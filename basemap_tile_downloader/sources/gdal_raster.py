# -*- coding: utf-8 -*-
"""Local raster (GDAL) source backend for the Basemap Tile Downloader.

Unlike the WMS/WMTS/XYZ backends there is nothing to fetch — the pixels already
live on disk (e.g. a GeoTIFF). This backend reuses the same tiling/mosaic
pipeline to export a local raster over the chosen extent: each tile is a
windowed read of the source at the chosen resolution (in the raster's own CRS),
then the shared mosaic step reprojects to the output CRS and optionally crops to
the exact extent.
"""

from qgis.core import (
    QgsProject, QgsRectangle, QgsGeometry, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsRasterLayer,
)

from ..engine import DownloaderError, TileFetchError
from ..tilemath import wms_grid_dims

try:
    from osgeo import gdal
except ImportError:
    gdal = None

SOURCE_NAME = "GeoTIFF"
LOCAL = True                   # a local windowed read, not a network download —
                               # drives the plugin's "read/export" vs "download" wording
INITIAL_DELAY_SEC = 0.0        # local reads: no server, so no throttling needed
CONCURRENCY = 4                # each fetch opens its own handle (safe for reads)


# ─────────────────────────────────────────────
# DETECTION / PARAMS
# ─────────────────────────────────────────────
def detect(layer):
    # File-backed rasters (GeoTIFF, JP2, …) use the "gdal" provider; the remote
    # tile sources all use "wms".
    return isinstance(layer, QgsRasterLayer) and layer.providerType() == "gdal"


def extract_params(layer):
    path = layer.source()
    if not path:
        raise DownloaderError("Could not determine the raster file path.")
    crs = layer.crs()
    ext = layer.extent()

    dp = layer.dataProvider()
    nodata = None
    try:
        if dp is not None and dp.sourceHasNoDataValue(1):
            nodata = dp.sourceNoDataValue(1)
    except Exception:
        nodata = None

    return {
        "path": path,
        "crs":  crs.authid() or crs.toWkt(),
        # Footprint (in the raster's own CRS) so we can skip tiles outside it.
        "src_bounds": (ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()),
        # Native pixel size — the dialog uses it to default the resolution.
        "native_res": layer.rasterUnitsPerPixelX(),
        "bands":  layer.bandCount(),
        "nodata": nodata,
    }


def native_crs(params, opts):
    return params["crs"]

def default_out_crs(params):
    return params["crs"]

def fingerprint_parts(params, opts):
    return [params["path"], params["crs"],
            opts.get("tile_pixels"), opts.get("resolution")]


def _preserve_nodata(params):
    """Nodata value to carry through for a single-band raster (e.g. a DTM), or
    None. Multi-band (RGB/RGBA) imagery is masked with an alpha band instead;
    for single-band data an alpha band would leave QGIS to compute its grey
    stretch over the fill pixels, changing how the result looks."""
    if params.get("bands", 1) >= 3:
        return None
    return params.get("nodata")


def mosaic_hints(params, opts):
    """Tell the shared mosaic step whether to add an alpha band (RGB) or preserve
    a nodata value (single-band)."""
    nd = _preserve_nodata(params)
    return {"add_alpha": nd is None, "nodata": nd}


# ─────────────────────────────────────────────
# TILE GRID
# ─────────────────────────────────────────────
def build_tile_grid(extent_geom, extent_crs, params, opts, logger):
    tile_pixels = int(opts.get("tile_pixels", 1024))
    # A local raster is exported at its own native pixel size (the dialog greys
    # out the resolution field), so use the source resolution rather than
    # opts["resolution"] — which the greyed spinbox may not represent exactly.
    resolution  = float(params.get("native_res") or opts.get("resolution", 0.5))

    req_crs = QgsCoordinateReferenceSystem(params["crs"])
    if not req_crs.isValid():
        raise DownloaderError(f"Raster CRS '{params['crs']}' is invalid.")

    ctx     = QgsProject.instance().transformContext()
    src_crs = QgsCoordinateReferenceSystem(extent_crs)
    region  = QgsGeometry(extent_geom)
    if src_crs != req_crs and region.transform(
            QgsCoordinateTransform(src_crs, req_crs, ctx)) != 0:
        raise DownloaderError("Could not reproject the extent to the raster CRS.")

    bb   = region.boundingBox()
    step = tile_pixels * resolution
    if step <= 0:
        raise DownloaderError("Tile size in map units is ≤ 0 – check resolution.")

    n_cols, n_rows = wms_grid_dims(bb.width(), bb.height(), step)
    logger.info("Extent bbox (raster CRS): %s", bb.toString())
    logger.info("Grid: %d×%d tiles, %.4f units/tile", n_cols, n_rows, step)

    sxmin, symin, sxmax, symax = params["src_bounds"]
    src_rect = QgsRectangle(sxmin, symin, sxmax, symax)

    tiles, tid, skipped = [], 0, 0
    for row in range(n_rows):
        for col in range(n_cols):
            xmin = bb.xMinimum() + col * step
            ymin = bb.yMinimum() + row * step
            xmax, ymax = xmin + step, ymin + step
            rect = QgsRectangle(xmin, ymin, xmax, ymax)
            if not rect.intersects(src_rect):
                skipped += 1
                continue        # outside the raster footprint → nothing to read
            if QgsGeometry.fromRect(rect).intersects(region):
                # gx/gy are the grid indices the engine uses for macro-cell order.
                tiles.append({"id": tid, "xmin": xmin, "ymin": ymin,
                              "xmax": xmax, "ymax": ymax, "gx": col, "gy": row})
                tid += 1

    logger.info("Kept %d tiles (%d skipped as outside the raster).", len(tiles), skipped)
    if not tiles:
        raise DownloaderError("The extent does not overlap the raster.")
    return tiles


# ─────────────────────────────────────────────
# FETCH  (windowed read of the local file)
# ─────────────────────────────────────────────
def fetch_one_tile(params, opts, tile, out_path, logger):
    if gdal is None:
        raise DownloaderError("GDAL bindings unavailable; cannot read the raster.")
    tile_pixels = int(opts.get("tile_pixels", 1024))
    crs = params["crs"]
    xmin, ymin, xmax, ymax = tile["xmin"], tile["ymin"], tile["xmax"], tile["ymax"]
    if tile["id"] == 0:
        logger.info("FIRST TILE bounds (%s): %s", crs, (xmin, ymin, xmax, ymax))

    warp_kwargs = dict(
        format="GTiff",
        srcSRS=crs, dstSRS=crs,
        outputBounds=[xmin, ymin, xmax, ymax],
        width=tile_pixels, height=tile_pixels,
        resampleAlg="near",
    )
    nd = _preserve_nodata(params)
    if nd is None:
        # RGB/multiband (or no source nodata): mask outside-source areas with alpha.
        warp_kwargs["dstAlpha"] = True
    else:
        # Single-band with nodata (e.g. a DTM): keep the nodata value instead of
        # adding alpha, so out-of-source fill isn't mistaken for real data.
        warp_kwargs["srcNodata"] = nd
        warp_kwargs["dstNodata"] = nd
    try:
        ds = gdal.Warp(out_path, params["path"], options=gdal.WarpOptions(**warp_kwargs))
    except Exception as e:
        raise TileFetchError(f"GDAL read failed: {e}")
    if ds is None:
        raise TileFetchError("GDAL produced no output for the tile.")
    ds = None
    return out_path

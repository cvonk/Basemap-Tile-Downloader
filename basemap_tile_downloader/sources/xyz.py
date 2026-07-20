# -*- coding: utf-8 -*-
"""XYZ source backend for the Basemap Tile Downloader ({z}/{x}/{y} in Web Mercator)."""

import urllib.parse

from qgis.core import (
    QgsProject, QgsRectangle, QgsGeometry, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsRasterLayer, QgsDataSourceUri,
)

from .. import engine
from ..engine import DownloaderError, TileFetchError
# Pure Web-Mercator math (QGIS-free, unit-tested in tests/test_tilemath.py).
# tile_resolution_m is re-exported here so callers can use xyz.tile_resolution_m.
from ..tilemath import (
    tile_resolution_m, tile_bounds_3857, xyz_url, tile_range,
)

SOURCE_NAME = "XYZ"
INITIAL_DELAY_SEC = 0.25       # tile servers usually tolerate a faster start

WEBMERC = "EPSG:3857"


# ─────────────────────────────────────────────
# DETECTION / PARAMS
# ─────────────────────────────────────────────
def detect(layer):
    if not isinstance(layer, QgsRasterLayer) or layer.providerType() != "wms":
        return False
    uri = QgsDataSourceUri(); uri.setEncodedUri(layer.source())
    return (uri.param("type") or "").lower() == "xyz"


def extract_params(layer):
    uri = QgsDataSourceUri(); uri.setEncodedUri(layer.source())
    template = urllib.parse.unquote((uri.param("url") or "").strip())
    if not template or "{z}" not in template:
        raise DownloaderError(
            "Could not extract a {z}/{x}/{y} URL template from the XYZ layer source.")

    def _int(v, default):
        try: return int(v)
        except (TypeError, ValueError): return default
    # zmin/zmax are the zoom range the layer advertises; the tile grid ignores
    # them (it uses the chosen `zoom`), but the dialog reads them to clamp the
    # zoom spinner to what the source actually serves.
    return {"template": template,
            "zmin": _int(uri.param("zmin"), 0),
            "zmax": _int(uri.param("zmax"), 22)}


def native_crs(params, opts):
    return WEBMERC

def default_out_crs(params):
    return WEBMERC

def fingerprint_parts(params, opts):
    return [params["template"], opts.get("zoom")]


# XYZ tiles have a global identity ({z}/{x}/{y} on a fixed template), so
# overlapping AOIs reuse each other's tiles from the shared cache.
SHAREABLE = True


def shared_signature(params, opts):
    """Identity of the tile source for the cross-job shared cache: the URL
    template, independent of extent and zoom (the zoom is in each tile's path)."""
    return "xyz\n" + params["template"]


def shared_rel_path(tile):
    """Path (under the source's shared dir) for this tile's global identity, or
    None if the tile lacks the expected keys (a resumed legacy queue)."""
    if not {"z", "x", "y"} <= tile.keys():
        return None
    return "{}/{}/{}.tif".format(tile["z"], tile["x"], tile["y"])


# ─────────────────────────────────────────────
# TILE GRID
# ─────────────────────────────────────────────
def build_tile_grid(extent_geom, extent_crs, params, opts, logger):
    zoom = int(opts.get("zoom", 18))
    web  = QgsCoordinateReferenceSystem(WEBMERC)
    ctx  = QgsProject.instance().transformContext()
    src  = QgsCoordinateReferenceSystem(extent_crs)
    region = QgsGeometry(extent_geom)
    if src != web and region.transform(QgsCoordinateTransform(src, web, ctx)) != 0:
        raise DownloaderError("Could not reproject the extent to EPSG:3857.")

    bb = region.boundingBox()
    xmin, xmax, ymin, ymax = tile_range(
        bb.xMinimum(), bb.yMinimum(), bb.xMaximum(), bb.yMaximum(), zoom)

    logger.debug("Extent bbox (EPSG:3857): %s", bb.toString())
    logger.info("Zoom %d → %.3f m/px; tiles x[%d..%d] y[%d..%d]",
                zoom, tile_resolution_m(zoom), xmin, xmax, ymin, ymax)

    tiles, tid = [], 0
    for ty in range(ymin, ymax + 1):
        for tx in range(xmin, xmax + 1):
            ulx, uly, lrx, lry = tile_bounds_3857(tx, ty, zoom)
            if QgsGeometry.fromRect(QgsRectangle(ulx, lry, lrx, uly)).intersects(region):
                tiles.append({"id": tid, "z": zoom, "x": tx, "y": ty})
                tid += 1

    logger.info("Kept %d tiles intersecting the extent.", len(tiles))
    if not tiles:
        raise DownloaderError("No tiles intersect the extent.")
    return tiles


# ─────────────────────────────────────────────
# FETCH
# ─────────────────────────────────────────────
def fetch_one_tile(params, opts, tile, out_path, logger, attempt=0):
    # `attempt` (retry cache-buster) is unused: an XYZ tile has a stable
    # {z}/{x}/{y} URL and no server-side error cache to bust.
    url = xyz_url(params["template"], tile["x"], tile["y"], tile["z"])
    # URLs are logged with credential-looking query values (?key=…) masked, so a
    # shared download.log can't leak an API key.
    logger.debug("GET tile %d (z%d/%d/%d): %s",
                 tile["id"], tile["z"], tile["x"], tile["y"],
                 engine.redact_url(url))
    if tile["id"] == 0:
        logger.info("FIRST TILE URL (paste into a browser to verify; any "
                    "credential masked): %s", engine.redact_url(url))

    status, headers, body, err, timed_out = engine.blocking_get(url)
    # Order matters: any HTTP status >= 400 ALSO sets `err`
    # (QgsBlockingNetworkRequest reports it as ServerExceptionError), so the
    # status-specific handling must run before the generic network-error raise —
    # otherwise the throttle/back-off (and Retry-After) paths are unreachable.
    if timed_out:
        raise TileFetchError("Request timed out.")
    if status in (404, 204):
        return None                       # missing tile → legitimate gap
    if status in (429, 403):
        # Some tile servers use 403 to signal rate-limiting / over-use, so treat
        # it as a throttle: back off and retry. A genuinely forbidden resource
        # still fails once the per-tile attempt cap is reached.
        raise TileFetchError(f"HTTP {status} (rate-limited?).",
                             retry_after=engine.parse_retry_after(headers.get("retry-after")),
                             is_throttle=True)
    if status in (500, 503):
        raise TileFetchError(f"HTTP {status}.",
                             retry_after=engine.parse_retry_after(headers.get("retry-after")),
                             is_throttle=True)
    if status and status >= 400:
        raise TileFetchError(f"HTTP {status}.")
    if err:                               # network-level failure (no HTTP status)
        raise TileFetchError(f"Network error: {err}")
    if not body:
        return None                       # empty 2xx body → treat as a gap

    bounds  = tile_bounds_3857(tile["x"], tile["y"], tile["z"])
    problem = engine.georeference(body, out_path, bounds, WEBMERC)
    if problem:
        raise TileFetchError(problem)
    return out_path

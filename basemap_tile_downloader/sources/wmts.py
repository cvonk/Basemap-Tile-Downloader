# -*- coding: utf-8 -*-
"""
WMTS source backend for the Basemap Tile Downloader.

Parses the WMTS GetCapabilities for the layer's TileMatrixSet (per-zoom
scale / origin / tile size / matrix extent), works out which tiles cover the
extent at a chosen matrix, and fetches them via RESTful ResourceURL templates or
KVP GetTile requests.
"""

import math, urllib.parse
# Used only for ET.ParseError; all untrusted XML from the server is parsed by the
# hardened safexml module (entities disabled), never by ElementTree.
import xml.etree.ElementTree as ET  # nosec B405

from qgis.core import (
    QgsProject, QgsRectangle, QgsGeometry, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsRasterLayer, QgsDataSourceUri,
)

from .. import engine, safexml
from ..engine import DownloaderError, TileFetchError

SOURCE_NAME = "WMTS"
INITIAL_DELAY_SEC = 0.5

STANDARD_PIXEL_SIZE_M  = 0.00028               # OGC "standardized rendering pixel size"
METERS_PER_UNIT_DEGREE = 111319.49079327358    # WMTS constant for degree-based CRS


# ─────────────────────────────────────────────
# DETECTION / PARAMS
# ─────────────────────────────────────────────
def detect(layer):
    if not isinstance(layer, QgsRasterLayer) or layer.providerType() != "wms":
        return False
    uri = QgsDataSourceUri(); uri.setEncodedUri(layer.source())
    if (uri.param("type") or "").lower() == "xyz":
        return False
    return bool(uri.param("tileMatrixSet"))


def _first_param(uri, name):
    v = uri.param(name)
    if v:
        return v
    vs = uri.params(name)
    return vs[0] if vs else ""


def extract_params(layer):
    uri = QgsDataSourceUri(); uri.setEncodedUri(layer.source())
    caps_url = (uri.param("url") or "").strip()
    layer_id = _first_param(uri, "layers")
    if not caps_url or not layer_id:
        raise DownloaderError("WMTS layer source is missing 'url' or 'layers'.")
    return {
        "caps_url":        caps_url,
        "layer":           layer_id,
        "style":           _first_param(uri, "styles"),
        "format":          uri.param("format") or "image/png",
        "tile_matrix_set": uri.param("tileMatrixSet"),
        "crs":             (uri.param("crs") or "").upper(),
        # resolved during prepare():
        "matrices": None, "tms_crs": None, "rest_template": None, "kvp_base": None,
    }


def native_crs(params, opts):
    # tms_crs after prepare(); before that, the layer's declared CRS.
    return params.get("tms_crs") or params.get("crs") or "EPSG:3857"

def default_out_crs(params):
    return native_crs(params, {})

def fingerprint_parts(params, opts):
    return [params["caps_url"], params["layer"], params["tile_matrix_set"],
            opts.get("zoom")]


# WMTS tiles have a global identity (matrix/col/row within a tile-matrix-set),
# so overlapping AOIs reuse each other's tiles from the shared cache.
SHAREABLE = True


def shared_signature(params, opts):
    """Identity of the tile source for the cross-job shared cache: the endpoint,
    layer, matrix set, style and format — independent of extent and zoom (the
    matrix index is in each tile's path)."""
    return "wmts\n" + "\n".join(str(params.get(k, "")) for k in
                                ("caps_url", "layer", "tile_matrix_set",
                                 "style", "format"))


def shared_rel_path(tile):
    """Path (under the source's shared dir) for this tile's global identity (the
    matrix index is stable across jobs for a given tile-matrix-set), or None if
    the tile lacks the expected keys (a resumed legacy queue)."""
    if not {"m", "col", "row"} <= tile.keys():
        return None
    return "{}/{}/{}.tif".format(tile["m"], tile["col"], tile["row"])


# ─────────────────────────────────────────────
# CAPABILITIES PARSING  (prepare hook)
# ─────────────────────────────────────────────
def _local(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def _first_text(el, name):
    for c in el.iter():
        if _local(c.tag) == name and c.text:
            return c.text.strip()
    return ""


def prepare(params, opts, logger):
    url = params["caps_url"]
    logger.info("WMTS GetCapabilities → %s", url)
    status, headers, body, err, timed_out = engine.blocking_get(url)
    if timed_out:
        raise DownloaderError("Timed out fetching WMTS capabilities.")
    if err:
        raise DownloaderError(f"Network error fetching WMTS capabilities: {err}")
    if status and status >= 400:
        raise DownloaderError(f"WMTS GetCapabilities returned HTTP {status}.")
    if not body:
        raise DownloaderError("WMTS GetCapabilities returned an empty body.")
    try:
        root = safexml.fromstring(body)
    except ET.ParseError as e:
        raise DownloaderError(f"Cannot parse WMTS capabilities XML: {e}")

    # Locate the TileMatrixSet *definition* (has TileMatrix children, unlike the
    # TileMatrixSetLink reference inside a Layer).
    tms_id = params["tile_matrix_set"]
    tms_el = None
    for el in root.iter():
        if _local(el.tag) == "TileMatrixSet" and \
                any(_local(c.tag) == "TileMatrix" for c in el) and \
                _first_text(el, "Identifier") == tms_id:
            tms_el = el
            break
    if tms_el is None:
        raise DownloaderError(f"TileMatrixSet '{tms_id}' not found in capabilities.")

    supported = _first_text(tms_el, "SupportedCRS") or params.get("crs") or "EPSG:3857"
    crs = QgsCoordinateReferenceSystem(supported)
    if not crs.isValid():
        crs = QgsCoordinateReferenceSystem(params.get("crs") or "EPSG:3857")
    tms_crs   = crs.authid() or "EPSG:3857"
    geographic = crs.isGeographic()
    mpu = METERS_PER_UNIT_DEGREE if geographic else 1.0

    matrices = []
    for tm in tms_el:
        if _local(tm.tag) != "TileMatrix":
            continue
        try:
            scale = float(_first_text(tm, "ScaleDenominator"))
            a, b  = (float(v) for v in _first_text(tm, "TopLeftCorner").split()[:2])
            tw = int(_first_text(tm, "TileWidth"));  th = int(_first_text(tm, "TileHeight"))
            mw = int(_first_text(tm, "MatrixWidth")); mh = int(_first_text(tm, "MatrixHeight"))
        except (ValueError, IndexError):
            continue
        # TopLeftCorner follows CRS axis order: geographic CRS give (lat, lon).
        topx, topy = (b, a) if geographic else (a, b)
        px = scale * STANDARD_PIXEL_SIZE_M / mpu
        matrices.append({"id": _first_text(tm, "Identifier"), "scale": scale,
                         "topx": topx, "topy": topy,
                         "tsx": tw * px, "tsy": th * px, "mw": mw, "mh": mh})
    if not matrices:
        raise DownloaderError("WMTS TileMatrixSet has no usable TileMatrix entries.")
    matrices.sort(key=lambda m: m["scale"], reverse=True)     # coarse → fine

    # RESTful GetTile template for the layer, if advertised.
    rest = None
    for lyr in root.iter():
        if _local(lyr.tag) == "Layer" and _first_text(lyr, "Identifier") == params["layer"]:
            for res in lyr:
                if _local(res.tag) == "ResourceURL" and res.get("resourceType") == "tile":
                    rest = res.get("template")
                    break
            break

    p = urllib.parse.urlparse(url)
    params["kvp_base"]      = urllib.parse.urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    params["rest_template"] = rest
    params["matrices"]      = matrices
    params["tms_crs"]       = tms_crs
    logger.info("WMTS: %d matrices, CRS=%s, %s GetTile",
                len(matrices), tms_crs, "RESTful" if rest else "KVP")


# ─────────────────────────────────────────────
# TILE GRID
# ─────────────────────────────────────────────
def build_tile_grid(extent_geom, extent_crs, params, opts, logger):
    matrices = params.get("matrices")
    if not matrices:
        raise DownloaderError("WMTS capabilities not prepared.")
    m   = max(0, min(len(matrices) - 1, int(opts.get("zoom", 0))))
    mat = matrices[m]

    tms    = QgsCoordinateReferenceSystem(params["tms_crs"])
    ctx    = QgsProject.instance().transformContext()
    src    = QgsCoordinateReferenceSystem(extent_crs)
    region = QgsGeometry(extent_geom)
    if src != tms and region.transform(QgsCoordinateTransform(src, tms, ctx)) != 0:
        raise DownloaderError("Could not reproject the extent to the tile-matrix-set CRS.")

    bb    = region.boundingBox()
    topx, topy, tsx, tsy = mat["topx"], mat["topy"], mat["tsx"], mat["tsy"]

    def _clampc(v): return max(0, min(mat["mw"] - 1, v))
    def _clampr(v): return max(0, min(mat["mh"] - 1, v))
    cmin = _clampc(int(math.floor((bb.xMinimum() - topx) / tsx)))
    cmax = _clampc(int(math.floor((bb.xMaximum() - topx) / tsx)))
    rmin = _clampr(int(math.floor((topy - bb.yMaximum()) / tsy)))
    rmax = _clampr(int(math.floor((topy - bb.yMinimum()) / tsy)))

    logger.info("WMTS matrix '%s' (%s): cols[%d..%d] rows[%d..%d]",
                mat["id"], params["tms_crs"], cmin, cmax, rmin, rmax)

    tiles, tid = [], 0
    for r in range(rmin, rmax + 1):
        for c in range(cmin, cmax + 1):
            ulx = topx + c * tsx; uly = topy - r * tsy
            if QgsGeometry.fromRect(
                    QgsRectangle(ulx, uly - tsy, ulx + tsx, uly)).intersects(region):
                tiles.append({"id": tid, "m": m, "col": c, "row": r})
                tid += 1

    logger.info("Kept %d tiles intersecting the extent.", len(tiles))
    if not tiles:
        raise DownloaderError("No tiles intersect the extent.")
    return tiles


# ─────────────────────────────────────────────
# FETCH
# ─────────────────────────────────────────────
def _tile_url(params, mat, row, col):
    if params.get("rest_template"):
        return (params["rest_template"]
                .replace("{TileMatrix}", str(mat["id"]))
                .replace("{TileRow}", str(row))
                .replace("{TileCol}", str(col))
                .replace("{Style}", params.get("style") or "default")
                .replace("{TileMatrixSet}", params["tile_matrix_set"]))
    q = {"SERVICE": "WMTS", "REQUEST": "GetTile", "VERSION": "1.0.0",
         "LAYER": params["layer"], "STYLE": params.get("style") or "",
         "FORMAT": params.get("format") or "image/png",
         "TILEMATRIXSET": params["tile_matrix_set"],
         "TILEMATRIX": str(mat["id"]), "TILEROW": str(row), "TILECOL": str(col)}
    base = params["kvp_base"]
    return base + ("&" if "?" in base else "?") + urllib.parse.urlencode(q)


def fetch_one_tile(params, opts, tile, out_path, logger):
    mat = params["matrices"][tile["m"]]
    url = _tile_url(params, mat, tile["row"], tile["col"])
    logger.debug("GET tile %d (m%s/%d/%d): %s",
                 tile["id"], mat["id"], tile["row"], tile["col"], url)
    if tile["id"] == 0:
        logger.info("FIRST TILE URL (paste into a browser to verify): %s", url)

    status, headers, body, err, timed_out = engine.blocking_get(url)
    if timed_out:
        raise TileFetchError("Request timed out.")
    if err and status not in (404, 204):
        raise TileFetchError(f"Network error: {err}")
    if status in (404, 204) or not body:
        return None
    if status in (429, 403):
        raise TileFetchError(f"HTTP {status} (rate-limited?).",
                             retry_after=engine.parse_retry_after(headers.get("retry-after")),
                             is_throttle=True)
    if status in (500, 503):
        raise TileFetchError(f"HTTP {status}.",
                             retry_after=engine.parse_retry_after(headers.get("retry-after")),
                             is_throttle=True)
    if status and status >= 400:
        raise TileFetchError(f"HTTP {status}.")

    ulx = mat["topx"] + tile["col"] * mat["tsx"]
    uly = mat["topy"] - tile["row"] * mat["tsy"]
    bounds  = (ulx, uly, ulx + mat["tsx"], uly - mat["tsy"])
    problem = engine.georeference(body, out_path, bounds, params["tms_crs"])
    if problem:
        raise TileFetchError(problem)
    return out_path

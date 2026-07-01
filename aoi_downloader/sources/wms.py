# -*- coding: utf-8 -*-
"""WMS source backend for the AOI Downloader (GetMap over an AOI bbox)."""

import urllib.parse
import xml.etree.ElementTree as ET

from qgis.core import (
    QgsProject, QgsRectangle, QgsGeometry, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsRasterLayer, QgsDataSourceUri,
)

from .. import engine
from ..engine import DownloaderError, TileFetchError
from ..tilemath import wms_grid_dims

SOURCE_NAME = "WMS"
INITIAL_DELAY_SEC = 1.0        # WMS servers are often stricter; start gently
CONCURRENCY = 2               # …and are less tolerant of many parallel connections

PREFERRED_FORMATS = [
    ["image/tiff", "geotiff", "image/geo+tiff", "application/x-geotiff"],
    ["image/png"],
]


# ─────────────────────────────────────────────
# DETECTION / PARAMS
# ─────────────────────────────────────────────
def detect(layer):
    if not isinstance(layer, QgsRasterLayer) or layer.providerType() != "wms":
        return False
    uri = QgsDataSourceUri(); uri.setEncodedUri(layer.source())
    if (uri.param("type") or "").lower() == "xyz":
        return False                                        # XYZ backend
    if uri.param("tileMatrixSet"):
        return False                                        # WMTS backend
    return True


def extract_params(layer):
    uri = QgsDataSourceUri(); uri.setEncodedUri(layer.source())

    base_url = (uri.param("url") or uri.param("URL") or "").strip()
    if not base_url:
        raw = urllib.parse.unquote(layer.source())
        for part in raw.split("&"):
            if part.lower().startswith("url="):
                base_url = part[4:]; break
    if not base_url:
        raise DownloaderError("Could not extract a base URL from the WMS layer source.")

    layers = uri.params("layers") or uri.params("LAYERS")
    if not layers:
        raise DownloaderError("WMS layer has no 'layers' parameter.")

    params = {
        "url":    base_url,
        "layers": layers,
        "styles": uri.params("styles") or uri.params("STYLES") or [""],
        "crs":    (uri.param("crs") or uri.param("CRS") or
                   uri.param("srs") or uri.param("SRS") or "").upper(),
        "format": uri.param("format") or uri.param("FORMAT") or "",
        "extra":  {},
    }
    if not params["crs"]:
        params["crs"] = layer.crs().authid()

    known = {"url","URL","layers","LAYERS","styles","STYLES",
             "crs","CRS","srs","SRS","format","FORMAT","dpiMode","type"}
    if hasattr(uri, "parameterKeys"):
        for key in uri.parameterKeys():
            if key not in known:
                params["extra"][key] = uri.param(key)
    return params


def native_crs(params, opts):
    return params["crs"]

def default_out_crs(params):
    return params["crs"]

def fingerprint_parts(params, opts):
    return [params["url"], ",".join(sorted(params["layers"])), params["crs"],
            opts.get("tile_pixels"), opts.get("resolution")]


# ─────────────────────────────────────────────
# GETCAPABILITIES + FORMAT NEGOTIATION  (prepare hook)
# ─────────────────────────────────────────────
def _cap_url(base_url):
    p = list(urllib.parse.urlparse(base_url))
    q = dict(urllib.parse.parse_qsl(p[4], keep_blank_values=True))
    lk = {k.lower(): k for k in q}
    q[lk.get("service", "SERVICE")] = "WMS"
    q[lk.get("request", "REQUEST")] = "GetCapabilities"
    if "version" not in lk:
        q["VERSION"] = "1.3.0"
    p[4] = urllib.parse.urlencode(q)
    return urllib.parse.urlunparse(p)


def _strip_ns(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def prepare(params, opts, logger):
    """Fetch GetCapabilities and pick the best advertised image format."""
    url = _cap_url(params["url"])
    logger.info("GetCapabilities → %s", url)
    status, headers, body, err, timed_out = engine.blocking_get(url)
    if timed_out:
        raise DownloaderError("Timed out fetching GetCapabilities.")
    if err:
        raise DownloaderError(f"Network error fetching GetCapabilities: {err}")
    if status and status >= 400:
        raise DownloaderError(f"GetCapabilities returned HTTP {status}.")
    if not body:
        raise DownloaderError("GetCapabilities returned an empty body.")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise DownloaderError(f"Cannot parse GetCapabilities XML: {e}")

    formats = []
    for elem in root.iter():
        if _strip_ns(elem.tag) == "GetMap":
            formats = [f.text.strip() for f in elem.iter()
                       if _strip_ns(f.tag) == "Format" and f.text]
            break
    logger.info("Advertised formats: %s", formats)

    chosen = None
    for group in PREFERRED_FORMATS:
        for fmt in formats:
            if any(tok in fmt.lower() for tok in group):
                chosen = fmt; break
        if chosen:
            break
    if not chosen:
        chosen = formats[0] if formats else "image/png"
        logger.warning("No preferred format matched; using %s", chosen)
    else:
        logger.info("Selected format: %s", chosen)
    params["format"] = chosen


# ─────────────────────────────────────────────
# TILE GRID
# ─────────────────────────────────────────────
def build_tile_grid(aoi_geom, aoi_crs, params, opts, logger):
    tile_pixels = int(opts.get("tile_pixels", 1024))
    resolution  = float(opts.get("resolution", 0.5))

    req_crs = QgsCoordinateReferenceSystem(params["crs"])
    if not req_crs.isValid():
        raise DownloaderError(f"Request CRS '{params['crs']}' is invalid.")

    ctx     = QgsProject.instance().transformContext()
    src_crs = QgsCoordinateReferenceSystem(aoi_crs)
    region  = QgsGeometry(aoi_geom)
    if src_crs != req_crs and region.transform(
            QgsCoordinateTransform(src_crs, req_crs, ctx)) != 0:
        raise DownloaderError("Could not reproject the extent to the request CRS.")

    bb    = region.boundingBox()
    step  = tile_pixels * resolution
    if step <= 0:
        raise DownloaderError("Tile size in map units is ≤ 0 – check resolution.")

    n_cols, n_rows = wms_grid_dims(bb.width(), bb.height(), step)
    logger.info("Extent bbox (req CRS): %s", bb.toString())
    logger.info("Grid: %d×%d tiles, %.2f map-units/tile", n_cols, n_rows, step)

    tiles, tid = [], 0
    for row in range(n_rows):
        for col in range(n_cols):
            xmin = bb.xMinimum() + col * step
            ymin = bb.yMinimum() + row * step
            xmax, ymax = xmin + step, ymin + step
            if QgsGeometry.fromRect(QgsRectangle(xmin, ymin, xmax, ymax)).intersects(region):
                tiles.append({"id": tid, "xmin": xmin, "ymin": ymin,
                              "xmax": xmax, "ymax": ymax})
                tid += 1

    logger.info("Kept %d/%d tiles intersecting the extent.", len(tiles), n_cols * n_rows)
    if not tiles:
        raise DownloaderError("No tiles intersect the extent.")
    return tiles


# ─────────────────────────────────────────────
# GETMAP URL + FETCH
# ─────────────────────────────────────────────
YX_CRS = {"EPSG:4326", "CRS:84", "EPSG:4258"}


def _getmap_url(params, opts, tile):
    tile_pixels = int(opts.get("tile_pixels", 1024))
    p  = list(urllib.parse.urlparse(params["url"]))
    q  = dict(urllib.parse.parse_qsl(p[4], keep_blank_values=True))
    lk = {k.lower(): k for k in q}

    def s(name, val):
        q[lk.get(name.lower(), name.upper())] = val

    version = q.get(lk.get("version", "VERSION"), "1.3.0")
    s("service", "WMS"); s("request", "GetMap"); s("version", version)
    s("layers", ",".join(params["layers"]))
    s("styles", ",".join(params["styles"]))
    s("format", params["format"])
    s("transparent", q.get(lk.get("transparent", "TRANSPARENT"), "TRUE"))
    s("width", str(tile_pixels)); s("height", str(tile_pixels))

    crs = params["crs"]
    use_yx = version.startswith("1.3") and crs.upper() in YX_CRS
    if version.startswith("1.3"):
        s("crs", crs)
    else:
        s("srs", crs)
    if use_yx:
        bbox = "{},{},{},{}".format(tile["ymin"], tile["xmin"], tile["ymax"], tile["xmax"])
    else:
        bbox = "{},{},{},{}".format(tile["xmin"], tile["ymin"], tile["xmax"], tile["ymax"])
    s("bbox", bbox)

    for k, v in (params.get("extra") or {}).items():
        if v is not None and k.lower() not in (
                "tilepixelratio", "contextualwmslegend", "featurecount", "dpimode"):
            s(k, v)

    p[4] = urllib.parse.urlencode(q)
    return urllib.parse.urlunparse(p)


def _is_xml_exception(body):
    head = body[:512].lstrip()
    if not head.startswith(b"<"):
        return False
    text = head.decode("utf-8", errors="ignore")
    return ("ServiceException" in text or "ExceptionReport" in text or
            ("<?xml" in text and b"<html" not in body[:64].lower()))


def _parse_exception(body):
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "Unparseable XML."
    msgs = [e.text.strip() for e in root.iter()
            if _strip_ns(e.tag) == "ServiceException" and e.text]
    return "; ".join(msgs) if msgs else ET.tostring(root, encoding="unicode")[:500]


def fetch_one_tile(params, opts, tile, out_path, logger):
    url = _getmap_url(params, opts, tile)
    logger.debug("GetMap tile %d: %s", tile["id"], url)
    if tile["id"] == 0:
        logger.info("FIRST TILE URL (paste into a browser to verify): %s", url)

    status, headers, body, err, timed_out = engine.blocking_get(url)
    if timed_out:
        raise TileFetchError("Request timed out.")
    if err:
        raise TileFetchError(f"Network error: {err}")
    if status == 429:
        raise TileFetchError("HTTP 429.",
                             retry_after=engine.parse_retry_after(headers.get("retry-after")),
                             is_throttle=True)
    if status in (500, 503):
        raise TileFetchError(f"HTTP {status}.",
                             retry_after=engine.parse_retry_after(headers.get("retry-after")),
                             is_throttle=True)
    if status and status >= 400:
        raise TileFetchError(f"HTTP {status}.")
    if _is_xml_exception(body):
        raise TileFetchError(f"WMS ServiceException: {_parse_exception(body)}")
    if not body:
        raise TileFetchError("Empty response body.")

    bounds = (tile["xmin"], tile["ymax"], tile["xmax"], tile["ymin"])   # ulx,uly,lrx,lry
    problem = engine.georeference(body, out_path, bounds, params["crs"], detect_empty=True)
    if problem == "EMPTY_TILE":
        return None
    if problem:
        raise TileFetchError(f"Invalid image: {problem}")
    return out_path

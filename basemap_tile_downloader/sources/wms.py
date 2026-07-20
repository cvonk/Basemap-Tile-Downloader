# -*- coding: utf-8 -*-
"""WMS source backend for the Basemap Tile Downloader (GetMap over an extent)."""

import math
import urllib.parse
# Used only for ET.ParseError / ET.tostring; all untrusted XML from the server is
# parsed by the hardened safexml module (entities disabled), never by ElementTree.
import xml.etree.ElementTree as ET  # nosec B405

from qgis.core import (
    QgsProject, QgsRectangle, QgsGeometry, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsRasterLayer, QgsDataSourceUri,
)

from .. import engine, safexml
from ..engine import DownloaderError, TileFetchError

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

    known = {"url", "URL", "layers", "LAYERS", "styles", "STYLES",
             "crs", "CRS", "srs", "SRS", "format", "FORMAT", "dpiMode", "type"}
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


# WMS GetMap tiles become reusable across jobs once the grid is anchored to the
# CRS origin (see build_tile_grid): the same (endpoint, layers, styles, CRS,
# format, resolution, tile size) yields identical tiles, addressed by global
# col/row. Unlike XYZ/WMTS, the resolution and tile size define the grid itself,
# so they belong in the signature rather than in each tile's path.
SHAREABLE = True


def shared_signature(params, opts):
    """Identity of a GetMap tile's content (everything but its position): the
    endpoint, layers, styles, CRS, negotiated format, tile size and resolution,
    plus any extra GetMap parameters that change the pixels."""
    parts = [params.get("url", ""),
             ",".join(params.get("layers") or []),
             ",".join(params.get("styles") or []),
             params.get("crs", ""),
             params.get("format", ""),
             str(int(opts.get("tile_pixels", 1024))),
             repr(float(opts.get("resolution", 0.5)))]
    extra = params.get("extra") or {}
    parts += ["{}={}".format(k, extra[k]) for k in sorted(extra)]
    return "wms\n" + "\n".join(parts)


def shared_rel_path(tile):
    """Path (under the source's shared dir) for this tile's global identity, or
    None if the tile predates the origin-anchored grid — a resumed pre-1.7.0 WMS
    queue has no col/row, so those tiles fall back to per-job storage instead of
    crashing (and are not re-downloaded)."""
    if "col" not in tile or "row" not in tile:
        return None
    return "{}/{}.tif".format(tile["col"], tile["row"])


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
    logger.info("GetCapabilities → %s", engine.redact_url(url))
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
        root = safexml.fromstring(body)
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
def build_tile_grid(extent_geom, extent_crs, params, opts, logger):
    tile_pixels = int(opts.get("tile_pixels", 1024))
    resolution  = float(opts.get("resolution", 0.5))

    req_crs = QgsCoordinateReferenceSystem(params["crs"])
    if not req_crs.isValid():
        raise DownloaderError(f"Request CRS '{params['crs']}' is invalid.")

    ctx     = QgsProject.instance().transformContext()
    src_crs = QgsCoordinateReferenceSystem(extent_crs)
    region  = QgsGeometry(extent_geom)
    if src_crs != req_crs and region.transform(
            QgsCoordinateTransform(src_crs, req_crs, ctx)) != 0:
        raise DownloaderError("Could not reproject the extent to the request CRS.")

    bb    = region.boundingBox()
    step  = tile_pixels * resolution
    if step <= 0:
        raise DownloaderError("Tile size in map units is ≤ 0 – check resolution.")

    # Anchor the grid to the CRS origin (0,0), not the extent's own corner, so
    # the same (CRS, resolution, tile size) yields identical tile boundaries for
    # any extent — letting overlapping AOIs reuse tiles from the shared cache.
    # Tiles are addressed by their global integer column/row. The exact-extent
    # crop still trims the final mosaic, so the output isn't enlarged.
    c0 = math.floor(bb.xMinimum() / step); c1 = math.floor(bb.xMaximum() / step)
    r0 = math.floor(bb.yMinimum() / step); r1 = math.floor(bb.yMaximum() / step)
    n_grid = (c1 - c0 + 1) * (r1 - r0 + 1)
    logger.debug("Extent bbox (req CRS): %s", bb.toString())
    logger.info("Grid: %d×%d tiles, %.2f map-units/tile (origin-anchored)",
                c1 - c0 + 1, r1 - r0 + 1, step)

    tiles, tid = [], 0
    for row in range(r0, r1 + 1):
        for col in range(c0, c1 + 1):
            xmin = col * step; ymin = row * step
            xmax, ymax = xmin + step, ymin + step
            if QgsGeometry.fromRect(QgsRectangle(xmin, ymin, xmax, ymax)).intersects(region):
                tiles.append({"id": tid, "col": col, "row": row,
                              "xmin": xmin, "ymin": ymin,
                              "xmax": xmax, "ymax": ymax})
                tid += 1

    logger.info("Kept %d/%d tiles intersecting the extent.", len(tiles), n_grid)
    if not tiles:
        raise DownloaderError("No tiles intersect the extent.")
    return tiles


# ─────────────────────────────────────────────
# GETMAP URL + FETCH
# ─────────────────────────────────────────────
YX_CRS = {"EPSG:4326", "CRS:84", "EPSG:4258"}


def _getmap_url(params, opts, tile, attempt=0):
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

    # Retry cache-buster. A WMS ServiceException comes back as HTTP 200 with an
    # error body, so a caching layer in front of the server (CDN/Varnish) can
    # cache it and serve that same error to every byte-identical retry — our
    # resume would then re-request the exact URL and never break through. On a
    # retry (attempt > 0) add a throwaway, per-attempt parameter so the request
    # key differs and the server actually re-renders. MapServer (and WMS in
    # general) ignores unknown params, so the pixels are unaffected. The first
    # attempt stays clean, so a genuinely-cached good tile is still reused.
    if attempt > 0:
        q["_btd_cb"] = str(attempt)

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
        root = safexml.fromstring(body)
    except ET.ParseError:
        return "Unparseable XML."
    msgs = [e.text.strip() for e in root.iter()
            if _strip_ns(e.tag) == "ServiceException" and e.text]
    return "; ".join(msgs) if msgs else ET.tostring(root, encoding="unicode")[:500]


def fetch_one_tile(params, opts, tile, out_path, logger, attempt=0):
    url = _getmap_url(params, opts, tile, attempt)
    # URLs are logged with credential-looking query values masked, so a shared
    # download.log can't leak an API key.
    logger.debug("GetMap tile %d: %s", tile["id"], engine.redact_url(url))
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
    if err:                               # network-level failure (no HTTP status)
        raise TileFetchError(f"Network error: {err}")
    if _is_xml_exception(body):
        # A ServiceException is usually the provider's server failing to draw
        # (e.g. it momentarily can't read its own data file) — often transient.
        # Flag it so the engine backs off and retries rather than failing fast.
        raise TileFetchError(f"WMS ServiceException: {_parse_exception(body)}",
                             is_server_error=True)
    if not body:
        raise TileFetchError("Empty response body.")

    bounds = (tile["xmin"], tile["ymax"], tile["xmax"], tile["ymin"])   # ulx,uly,lrx,lry
    # A fully-transparent tile is kept and mosaicked like any other tile (not
    # dropped), so the output covers the whole grid over the extent.
    problem = engine.georeference(body, out_path, bounds, params["crs"])
    if problem:
        raise TileFetchError(f"Invalid image: {problem}")
    return out_path

# -*- coding: utf-8 -*-
"""WCS source backend for the Basemap Tile Downloader (GetCoverage over an extent).

Speaks **WCS 1.0.0**, the only version whose GetCoverage maps directly onto the
plugin's grid model: a request is a bbox plus a pixel width/height in a chosen
CRS, exactly like WMS GetMap. (1.1.x wraps the raster in a multipart MIME
response and reorders axes per the CRS definition; 2.0.x replaces the bbox with
per-axis `subset=` parameters whose labels have to be discovered first, and
needs the Scaling extension to control the output size. Both are far more
fragile, and every WCS server that speaks them also speaks 1.0.0.)

Unlike WMS this backend learns the coverage's real geometry up front, from
DescribeCoverage: its native CRS, its native pixel size, and — importantly —
its exact footprint, so tiles that fall outside the published coverage are
never requested. Coverages are usually elevation data (single-band Float32 with
a nodata value), so the mosaic keeps that nodata rather than gaining an alpha
band.
"""

import math
import urllib.parse
import uuid
# Used only for ET.ParseError / ET.tostring; all untrusted XML from the server is
# parsed by the hardened safexml module (entities disabled), never by ElementTree.
import xml.etree.ElementTree as ET  # nosec B405

from qgis.core import (
    QgsProject, QgsRectangle, QgsGeometry, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsRasterLayer, QgsDataSourceUri,
)

from .. import engine, safexml
from ..engine import DownloaderError, TileFetchError

SOURCE_NAME = "WCS"
VERSION = "1.0.0"
INITIAL_DELAY_SEC = 1.0        # coverage servers are stricter than tile caches
CONCURRENCY = 2               # …and a float raster tile is megabytes, not kilobytes

# Preferred GetCoverage formats, best first. A coverage carries measured values
# (elevation, slope, …), so a lossless raster format is not merely nicer — JPEG
# would quantise the data. The names are WCS 1.0.0 format identifiers as
# advertised in DescribeCoverage; MIME spellings appear too, so match loosely.
PREFERRED_FORMATS = [
    ["geotiff"],                      # GeoTIFF / image/tiff;application=geotiff
    ["image/tiff", "tiff"],           # plain TIFF (still lossless)
    ["image/png", "png"],             # last resort: 8/16-bit, but lossless
]

# A cached tile may hold float pixels (a DTM), where TIFF predictor 2 (horizontal
# differencing over bytes) is the wrong transform — GDAL wants predictor 3 for
# floats. Rather than guess the band type per tile, compress without a predictor,
# as the local-raster backend does for the same reason.
TILE_CREATION_OPTIONS = ["COMPRESS=DEFLATE"]


# ─────────────────────────────────────────────
# DETECTION / PARAMS
# ─────────────────────────────────────────────
def detect(layer):
    # QGIS gives WCS layers their own provider, so this needs no disambiguation
    # against the "wms" provider that WMS/WMTS/XYZ all share.
    return isinstance(layer, QgsRasterLayer) and layer.providerType() == "wcs"


def extract_params(layer):
    uri = QgsDataSourceUri(); uri.setEncodedUri(layer.source())

    base_url = (uri.param("url") or uri.param("URL") or "").strip()
    if not base_url:
        raise DownloaderError("Could not extract a base URL from the WCS layer source.")

    coverage = (uri.param("identifier") or uri.param("coverage") or "").strip()
    if not coverage:
        raise DownloaderError("WCS layer has no 'identifier' (coverage name).")

    crs = (uri.param("crs") or uri.param("CRS") or "").upper()
    if not crs:
        crs = layer.crs().authid()

    # What QGIS already knows from having added the layer (it ran its own
    # DescribeCoverage). prepare() re-reads all of this from the server and wins
    # where they disagree; these are the no-network defaults the dialog uses to
    # pre-fill the resolution before a run starts.
    try:
        native_res = float(layer.rasterUnitsPerPixelX())
    except (TypeError, ValueError):
        native_res = None
    if not native_res or native_res <= 0 or not math.isfinite(native_res):
        native_res = None

    ext = layer.extent()
    try:
        bounds = (ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum())
        if not (bounds[0] < bounds[2] and bounds[1] < bounds[3]):
            bounds = None
    except AttributeError:
        bounds = None

    dp = layer.dataProvider()
    nodata = None
    try:
        if dp is not None and dp.sourceHasNoDataValue(1):
            nodata = dp.sourceNoDataValue(1)
    except Exception:
        nodata = None

    try:
        bands = int(layer.bandCount())
    except (TypeError, ValueError):
        bands = 1

    return {
        "url":        base_url,
        "coverage":   coverage,
        "crs":        crs,
        "format":     uri.param("format") or uri.param("FORMAT") or "",
        "native_res": native_res,
        # Coverage footprint in its own CRS — build_tile_grid skips tiles outside
        # it, so we never spend a request on ground the service doesn't publish.
        "src_bounds": bounds,
        "bands":      bands,
        "nodata":     nodata,
    }


def native_crs(params, opts):
    return params["crs"]

def default_out_crs(params):
    return params["crs"]

def fingerprint_parts(params, opts):
    return [params["url"], params["coverage"], params["crs"],
            opts.get("tile_pixels"), opts.get("resolution")]


# GetCoverage tiles become reusable across jobs once the grid is anchored to the
# CRS origin (see build_tile_grid), exactly as for WMS: the same (endpoint,
# coverage, CRS, format, resolution, tile size) yields identical tiles, addressed
# by global col/row.
SHAREABLE = True


def shared_signature(params, opts):
    """Identity of a GetCoverage tile's content (everything but its position)."""
    return "wcs\n" + "\n".join([
        params.get("url", ""),
        params.get("coverage", ""),
        params.get("crs", ""),
        params.get("format", ""),
        str(int(opts.get("tile_pixels", 1024))),
        repr(_grid_resolution(params, opts)),
    ])


def shared_rel_path(tile):
    """Path (under the source's shared dir) for this tile's global identity."""
    if "col" not in tile or "row" not in tile:
        return None
    return "{}/{}.tif".format(tile["col"], tile["row"])


def _preserve_nodata(params):
    """Nodata value to carry through for a single-band coverage (e.g. a DTM), or
    None. Multi-band imagery is masked with an alpha band instead; for single-band
    data an alpha band would leave QGIS to compute its grey stretch over the fill
    pixels, changing how the result looks."""
    if params.get("bands", 1) >= 3:
        return None
    return params.get("nodata")


def mosaic_hints(params, opts):
    """Tell the shared mosaic step whether to add an alpha band (RGB) or preserve
    a nodata value (single-band)."""
    nd = _preserve_nodata(params)
    return {"add_alpha": nd is None, "nodata": nd}


# ─────────────────────────────────────────────
# DESCRIBECOVERAGE  (prepare hook)
# ─────────────────────────────────────────────
def _strip_ns(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def _with_query(base_url, extra):
    """`base_url` with `extra` merged into its query string, preserving the case
    of any parameter the URL already carries (some services are picky)."""
    p  = list(urllib.parse.urlparse(base_url))
    q  = dict(urllib.parse.parse_qsl(p[4], keep_blank_values=True))
    lk = {k.lower(): k for k in q}
    for name, val in extra.items():
        q[lk.get(name.lower(), name)] = val
    p[4] = urllib.parse.urlencode(q)
    return urllib.parse.urlunparse(p)


def describe_url(base_url, coverage):
    return _with_query(base_url, {"SERVICE": "WCS", "VERSION": VERSION,
                                  "REQUEST": "DescribeCoverage",
                                  "COVERAGE": coverage})


def normalise_crs(srs_name):
    """An EPSG authid from whatever spelling a service used: 'EPSG:25832',
    'urn:ogc:def:crs:EPSG::25832', 'http://www.opengis.net/def/crs/EPSG/0/25832'.
    Anything unrecognised is returned stripped, for the caller to validate."""
    s = (srs_name or "").strip()
    if not s:
        return ""
    if s.upper().startswith("EPSG:"):
        return "EPSG:" + s.split(":")[-1]
    low = s.lower()
    if "epsg" in low:
        # Trailing numeric component of a URN or URI form is the code.
        code = s.rstrip("/").split("/")[-1].split(":")[-1]
        if code.isdigit():
            return "EPSG:" + code
    return s


def _pos_pairs(elem):
    """The <gml:pos> children of `elem` as (x, y) float pairs."""
    out = []
    for pos in elem:
        if _strip_ns(pos.tag) != "pos" or not pos.text:
            continue
        bits = pos.text.split()
        if len(bits) >= 2:
            try:
                out.append((float(bits[0]), float(bits[1])))
            except ValueError:
                pass
    return out


def parse_coverage_description(body, coverage):
    """Pull what we need out of a WCS 1.0.0 DescribeCoverage response:

        {"crs", "bounds", "native_res", "crss", "formats"}

    Every key may be absent if the service didn't publish it — prepare() treats
    them as refinements over what QGIS already told us, not as requirements.
    """
    try:
        root = safexml.fromstring(body)
    except ET.ParseError as e:
        raise DownloaderError(f"Cannot parse DescribeCoverage XML: {e}")

    offerings = [e for e in root.iter() if _strip_ns(e.tag) == "CoverageOffering"]
    if not offerings:
        raise DownloaderError(
            "DescribeCoverage returned no CoverageOffering — is "
            f"'{coverage}' a coverage on this service?")

    def offering_name(off):
        for child in off:
            if _strip_ns(child.tag) == "name" and child.text:
                return child.text.strip()
        return ""

    # Prefer the offering that actually names our coverage; a service asked for
    # one coverage normally returns exactly that, so the first is a fine fallback.
    chosen = next((o for o in offerings if offering_name(o) == coverage), offerings[0])

    info = {}
    for spatial in chosen.iter():
        if _strip_ns(spatial.tag) != "spatialDomain":
            continue
        for node in spatial:
            tag = _strip_ns(node.tag)
            if tag == "Envelope" and "bounds" not in info:
                pts = _pos_pairs(node)
                if len(pts) >= 2:
                    (x0, y0), (x1, y1) = pts[0], pts[1]
                    info["bounds"] = (min(x0, x1), min(y0, y1),
                                      max(x0, x1), max(y0, y1))
                srs = normalise_crs(node.get("srsName"))
                if srs:
                    info["crs"] = srs
            elif tag == "RectifiedGrid":
                srs = normalise_crs(node.get("srsName"))
                if srs and "crs" not in info:
                    info["crs"] = srs
                # Two offset vectors span one pixel: (res, 0) and (0, -res).
                # The pixel size is the length of the first non-degenerate one.
                for vec in node.iter():
                    if _strip_ns(vec.tag) != "offsetVector" or not vec.text:
                        continue
                    try:
                        comps = [abs(float(v)) for v in vec.text.split()]
                    except ValueError:
                        continue
                    step = max(comps) if comps else 0.0
                    if step > 0:
                        info["native_res"] = step
                        break
        break

    crss = []
    for node in chosen.iter():
        if _strip_ns(node.tag) in ("requestResponseCRSs", "requestCRSs",
                                   "supportedCRS", "nativeCRSs") and node.text:
            for tok in node.text.replace(",", " ").split():
                code = normalise_crs(tok)
                if code and code not in crss:
                    crss.append(code)
    if crss:
        info["crss"] = crss

    formats = [n.text.strip() for n in chosen.iter()
               if _strip_ns(n.tag) == "formats" and n.text and n.text.strip()]
    if formats:
        info["formats"] = formats
    return info


def _choose_format(formats, logger):
    for group in PREFERRED_FORMATS:
        for fmt in formats:
            if any(tok in fmt.lower() for tok in group):
                logger.info("Selected format: %s", fmt)
                return fmt
    chosen = formats[0]
    logger.warning("No preferred format matched %s; using %s", formats, chosen)
    return chosen


def prepare(params, opts, logger):
    """Read the coverage's geometry from the server before the run: native CRS,
    native pixel size, exact footprint, and the best lossless output format."""
    url = describe_url(params["url"], params["coverage"])
    logger.info("DescribeCoverage → %s", engine.redact_url(url))
    status, headers, body, err, timed_out = engine.blocking_get(url)
    if timed_out:
        raise DownloaderError("Timed out fetching DescribeCoverage.")
    if status and status >= 400:
        raise DownloaderError(f"DescribeCoverage returned HTTP {status}.")
    if err:
        raise DownloaderError(f"Network error fetching DescribeCoverage: {err}")
    if not body:
        raise DownloaderError("DescribeCoverage returned an empty body.")
    if _is_xml_exception(body) and b"CoverageOffering" not in body:
        raise DownloaderError(f"WCS DescribeCoverage failed: {_parse_exception(body)}")

    info = parse_coverage_description(body, params["coverage"])

    # CRS: keep the layer's if the service accepts it (the user may have picked a
    # reprojection QGIS offered), otherwise fall back to the coverage's native one.
    crss = info.get("crss") or []
    want = (params.get("crs") or "").upper()
    if crss and want and want not in [c.upper() for c in crss]:
        native = info.get("crs") or crss[0]
        logger.warning("Service does not offer %s for this coverage (offers %s); "
                       "requesting %s instead.", want, ", ".join(crss), native)
        params["crs"] = native
    elif not want:
        params["crs"] = info.get("crs") or (crss[0] if crss else "")
    if not params["crs"]:
        raise DownloaderError("Could not determine a request CRS for the coverage.")

    # The footprint and pixel size DescribeCoverage publishes are expressed in the
    # coverage's own CRS. We only adopt them when that is also the CRS we are
    # requesting in — otherwise the numbers would be compared against, and the
    # grid built from, coordinates in a different system (metres vs degrees, or a
    # different projection's origin). Better no footprint filter than a wrong one.
    same_crs = (info.get("crs") or "").upper() == (params["crs"] or "").upper()
    if info.get("bounds") and same_crs:
        params["src_bounds"] = info["bounds"]
        logger.info("Coverage footprint (%s): %s", params["crs"],
                    ", ".join(f"{v:.1f}" for v in info["bounds"]))
    elif info.get("bounds"):
        params["src_bounds"] = None
        logger.info("Coverage footprint is published in %s but we request %s; "
                    "not filtering tiles by footprint.",
                    info.get("crs"), params["crs"])
    if info.get("native_res") and same_crs:
        params["native_res"] = info["native_res"]
        logger.info("Coverage native resolution: %g units/px", info["native_res"])

    if info.get("formats"):
        params["format"] = _choose_format(info["formats"], logger)
    elif not params.get("format"):
        params["format"] = "GeoTIFF"
        logger.warning("Service advertised no formats; assuming GeoTIFF.")

    _probe_band_info(params, opts, logger)


def _probe_band_info(params, opts, logger):
    """Fetch one tiny coverage window to learn the band count and nodata value
    first-hand, so the mosaic step preserves a DTM's nodata instead of stretching
    an alpha band over it. Best-effort: on any problem we keep whatever QGIS
    reported for the layer. Doubles as an early check that the negotiated format
    actually comes back as a raster GDAL can open — better to fail here than
    after queueing thousands of tiles."""
    try:
        from osgeo import gdal
    except ImportError:
        return
    bounds = params.get("src_bounds")
    res = params.get("native_res") or 1.0
    if not bounds:
        return
    # A 16×16 window at the footprint's centre: a few kilobytes, and inside the
    # coverage wherever its shape, so the request can't miss.
    cx = (bounds[0] + bounds[2]) / 2.0
    cy = (bounds[1] + bounds[3]) / 2.0
    span = 16 * res
    probe = {"id": -1, "xmin": cx - span / 2, "ymin": cy - span / 2,
             "xmax": cx + span / 2, "ymax": cy + span / 2}
    url = _getcoverage_url(params, {"tile_pixels": 16}, probe)
    logger.debug("Probing band layout → %s", engine.redact_url(url))
    status, headers, body, err, timed_out = engine.blocking_get(url)
    if timed_out or err or not body or (status and status >= 400):
        logger.info("Band-layout probe skipped (%s); using the layer's own "
                    "band/nodata information.", err or f"HTTP {status}")
        return
    if _is_xml_exception(body):
        logger.info("Band-layout probe returned a ServiceException (%s); using "
                    "the layer's own band/nodata information.",
                    engine._first_line(_parse_exception(body)))
        return

    mem = f"/vsimem/wcs_probe_{uuid.uuid4().hex}"     # coverage names aren't path-safe
    ds = None
    try:
        gdal.FileFromMemBuffer(mem, body)
        ds = gdal.Open(mem)
        if ds is None:
            raise DownloaderError(
                "The coverage came back in a format GDAL cannot read "
                f"({params['format']}). Try a different format, or check the "
                "service's DescribeCoverage output.")
        params["bands"] = ds.RasterCount
        band = ds.GetRasterBand(1)
        nd = band.GetNoDataValue()
        if nd is not None:
            params["nodata"] = nd
        logger.info("Coverage returns %d band(s), %s, nodata=%s",
                    ds.RasterCount, gdal.GetDataTypeName(band.DataType),
                    params.get("nodata"))
    finally:
        ds = None
        try: gdal.Unlink(mem)
        except Exception: pass  # nosec B110


# ─────────────────────────────────────────────
# TILE GRID
# ─────────────────────────────────────────────
def _grid_resolution(params, opts):
    """Metres per pixel the grid is built at. The dialog lets a WCS run choose
    (the server resamples), defaulting to the coverage's native size; a run that
    never saw the dialog falls back to native, then to the shared default."""
    res = opts.get("resolution")
    try:
        res = float(res) if res else 0.0
    except (TypeError, ValueError):
        res = 0.0
    if res > 0:
        return res
    return float(params.get("native_res") or 0.5)


def build_tile_grid(extent_geom, extent_crs, params, opts, logger):
    tile_pixels = int(opts.get("tile_pixels", 1024))
    resolution  = _grid_resolution(params, opts)

    req_crs = QgsCoordinateReferenceSystem(params["crs"])
    if not req_crs.isValid():
        raise DownloaderError(f"Request CRS '{params['crs']}' is invalid.")

    ctx     = QgsProject.instance().transformContext()
    src_crs = QgsCoordinateReferenceSystem(extent_crs)
    region  = QgsGeometry(extent_geom)
    if src_crs != req_crs and region.transform(
            QgsCoordinateTransform(src_crs, req_crs, ctx)) != 0:
        raise DownloaderError("Could not reproject the extent to the request CRS.")

    bb   = region.boundingBox()
    step = tile_pixels * resolution
    if step <= 0:
        raise DownloaderError("Tile size in map units is ≤ 0 – check resolution.")

    # Anchor the grid to the CRS origin (0,0), not the extent's own corner, so the
    # same (CRS, resolution, tile size) yields identical tile boundaries for any
    # extent — letting overlapping AOIs reuse tiles from the shared cache. The
    # exact-extent crop still trims the final mosaic, so the output isn't enlarged.
    c0 = math.floor(bb.xMinimum() / step); c1 = math.floor(bb.xMaximum() / step)
    r0 = math.floor(bb.yMinimum() / step); r1 = math.floor(bb.yMaximum() / step)
    n_grid = (c1 - c0 + 1) * (r1 - r0 + 1)
    logger.debug("Extent bbox (req CRS): %s", bb.toString())
    logger.info("Grid: %d×%d tiles, %.2f map-units/tile (origin-anchored)",
                c1 - c0 + 1, r1 - r0 + 1, step)

    # DescribeCoverage told us the coverage's real footprint, so tiles beyond it
    # are dropped rather than requested — the service would only answer them with
    # a ServiceException or a nodata-filled tile.
    cov = params.get("src_bounds")
    cov_rect = QgsRectangle(cov[0], cov[1], cov[2], cov[3]) if cov else None

    tiles, tid, outside = [], 0, 0
    for row in range(r0, r1 + 1):
        for col in range(c0, c1 + 1):
            xmin = col * step; ymin = row * step
            xmax, ymax = xmin + step, ymin + step
            rect = QgsRectangle(xmin, ymin, xmax, ymax)
            if not QgsGeometry.fromRect(rect).intersects(region):
                continue
            if cov_rect is not None and not rect.intersects(cov_rect):
                outside += 1
                continue
            tiles.append({"id": tid, "col": col, "row": row,
                          "xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax})
            tid += 1

    logger.info("Kept %d/%d tiles intersecting the extent (%d outside the "
                "coverage footprint).", len(tiles), n_grid, outside)
    if not tiles:
        raise DownloaderError(
            "No tiles intersect both the extent and the coverage footprint — "
            "the service publishes no data for this area.")
    return tiles


# ─────────────────────────────────────────────
# GETCOVERAGE URL + FETCH
# ─────────────────────────────────────────────
def _getcoverage_url(params, opts, tile, attempt=0):
    tile_pixels = int(opts.get("tile_pixels", 1024))
    extra = {
        "SERVICE":  "WCS",
        "VERSION":  VERSION,
        "REQUEST":  "GetCoverage",
        "COVERAGE": params["coverage"],
        "CRS":      params["crs"],
        # WCS 1.0.0 fixes the BBOX axis order at (x, y) regardless of the CRS's
        # declared axis order, so — unlike WMS 1.3.0 — there is no swap to handle.
        "BBOX":     "{},{},{},{}".format(tile["xmin"], tile["ymin"],
                                         tile["xmax"], tile["ymax"]),
        "WIDTH":    str(tile_pixels),
        "HEIGHT":   str(tile_pixels),
        "FORMAT":   params["format"],
    }
    url = _with_query(params["url"], extra)
    # Retry cache-buster, as for WMS: a ServiceException arrives as HTTP 200 with
    # an error body, which a CDN in front of the service can cache and replay to
    # every byte-identical retry. Only retries carry it, so a genuinely cached
    # good tile is still reused.
    if attempt > 0:
        url += ("&" if urllib.parse.urlparse(url).query else "?") + f"_btd_cb={attempt}"
    return url


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
            if _strip_ns(e.tag) in ("ServiceException", "ExceptionText") and e.text]
    return "; ".join(msgs) if msgs else ET.tostring(root, encoding="unicode")[:500]


# A service's way of saying "that request is off the edge of my data". The grid
# already drops tiles outside the published footprint, so this only fires on a
# boundary tile — a genuine gap, not a failure, so it is recorded as an empty
# tile instead of burning the tile's retry budget.
_NO_DATA_PHRASES = (
    "does not intersect", "no intersection", "outside the coverage",
    "outside coverage", "envelope does not", "no data available",
)


def fetch_one_tile(params, opts, tile, out_path, logger, attempt=0):
    url = _getcoverage_url(params, opts, tile, attempt)
    logger.debug("GetCoverage tile %d: %s", tile["id"], engine.redact_url(url))
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
        detail = _parse_exception(body)
        low = detail.lower()
        if any(p in low for p in _NO_DATA_PHRASES):
            logger.debug("Tile %d is outside the coverage (%s); recording a gap.",
                         tile["id"], engine._first_line(detail))
            return None                   # legitimate gap, not an error
        # Otherwise it is usually the service failing to render (e.g. it can
        # momentarily not read its own store) — often transient, so flag it for
        # the back-pressure budget rather than failing the tile outright.
        raise TileFetchError(f"WCS ServiceException: {detail}", is_server_error=True)
    if not body:
        raise TileFetchError("Empty response body.")

    bounds = (tile["xmin"], tile["ymax"], tile["xmax"], tile["ymin"])   # ulx,uly,lrx,lry
    problem = engine.georeference(body, out_path, bounds, params["crs"],
                                  creation_options=TILE_CREATION_OPTIONS)
    if problem:
        raise TileFetchError(f"Invalid coverage response: {problem}")
    return out_path

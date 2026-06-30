# -*- coding: utf-8 -*-
"""
core.py – WMS AOI Downloader engine
===================================

Download engine for the "WMS AOI Downloader" QGIS plugin. Tiles a WMS GetMap
request over the bounding box of a polygon AOI, downloads with adaptive
throttling and a resumable SQLite queue, then mosaics the tiles into a
compressed, tiled GeoTIFF (with overviews) that is loaded into the project.

Usage:
  • Normal use: launch from the plugin's dialog
        Web ▸ WMS AOI Downloader…
    The dialog passes the chosen WMS layer, AOI polygon, tile size, resolution
    and output path to run().

  • Headless / console use (defaults to the configured layer names + config):
        from wms_aoi_downloader import core
        core.run()                       # or pass layers/params explicitly

Development workflow:
  Edit the source in this plugin folder, then re-sync to the QGIS plugins
  directory and reload the plugin:
        pwsh -File ..\sync.ps1           # mirrors source → plugins folder
        (then use Plugin Reloader, or restart QGIS)

Per-job working files go to  <project_dir>/wms_aoi_download/
  tiles/          – individual GetMap responses
  tiles.sqlite    – resumable work queue
  download.log    – full debug log
  mosaic.vrt      – intermediate VRT
The final GeoTIFF is written to the dialog's output path (or a temporary file);
with no path it defaults to  <project_dir>/wms_aoi_download/mosaic.tif.
"""

import os, io, json, math, sqlite3, logging, time, traceback, urllib.parse, hashlib
import xml.etree.ElementTree as ET
from datetime import datetime

from qgis.PyQt.QtCore  import QUrl, QTimer, QEventLoop
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply

from qgis.core import (
    Qgis, QgsProject, QgsTask, QgsApplication, QgsMessageLog,
    QgsRectangle, QgsGeometry, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsRasterLayer, QgsDataSourceUri,
    QgsBlockingNetworkRequest, QgsWkbTypes, QgsProcessingUtils,
)

try:
    from osgeo import gdal
    gdal.UseExceptions()
except ImportError:
    gdal = None

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
WMS_LAYER_NAME   = "Copertura regioni WMS"
AOI_LAYER_NAME   = "Area of Interest (EPSG:32632)"

TILE_PIXELS          = 1024    # pixel width/height of each GetMap request
TARGET_RESOLUTION    = 0.5     # map units per pixel in the WMS request CRS

MAX_ATTEMPTS_PER_TILE    = 6
INITIAL_DELAY_SEC        = 1.0
MIN_DELAY_SEC            = 0.05
MAX_DELAY_SEC            = 60.0
SPEEDUP_FACTOR           = 0.85
SLOWDOWN_FACTOR          = 2.0
SUCCESSES_BEFORE_SPEEDUP = 3
REQUEST_TIMEOUT_MS       = 60_000

PREFERRED_FORMATS = [
    ["image/tiff", "geotiff", "image/geo+tiff", "application/x-geotiff"],
    ["image/png"],
]

CLEANUP_TILES_AFTER_MOSAIC = False
WORK_SUBDIR_NAME = "wms_aoi_download"


# ─────────────────────────────────────────────
# JOB FINGERPRINT  (change detection)
# ─────────────────────────────────────────────
def _job_fingerprint(wms_params, tile_pixels, target_resolution, aoi_layer):
    """
    Return a short hex digest that uniquely identifies the combination of
    WMS source, tile geometry, and AOI features.  If anything changes the
    old work folder is invalid and must be wiped.
    """
    h = hashlib.sha256()
    h.update(wms_params["url"].encode())
    h.update(json.dumps(sorted(wms_params["layers"])).encode())
    h.update(wms_params["crs"].encode())
    h.update(str(tile_pixels).encode())
    h.update(str(target_resolution).encode())
    # Include AOI geometry so moving the polygon triggers a fresh download
    for feat in aoi_layer.getFeatures():
        g = feat.geometry()
        if not g.isNull():
            h.update(g.asWkt(precision=2).encode())
    return h.hexdigest()[:16]


# ─────────────────────────────────────────────
# EXCEPTIONS
# ─────────────────────────────────────────────
class DownloaderError(Exception):
    """Fatal, non-retryable."""

class TileFetchError(Exception):
    def __init__(self, message, retry_after=None, is_throttle=False):
        super().__init__(message)
        self.retry_after  = retry_after
        self.is_throttle  = is_throttle


# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
def _build_logger(work_dir):
    logger = logging.getLogger("wms_aoi_downloader")
    logger.setLevel(logging.DEBUG)
    logger.handlers = []

    log_path = os.path.join(work_dir, "download.log")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)

    class _QH(logging.Handler):
        def emit(self, record):
            lvl = {logging.DEBUG:    Qgis.Info,
                   logging.INFO:     Qgis.Info,
                   logging.WARNING:  Qgis.Warning,
                   logging.ERROR:    Qgis.Critical,
                   logging.CRITICAL: Qgis.Critical}.get(record.levelno, Qgis.Info)
            try:
                QgsMessageLog.logMessage(
                    self.format(record), "WMS AOI Downloader", lvl)
            except Exception:
                pass

    qh = _QH()
    qh.setLevel(logging.INFO)
    qh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(qh)
    logger.info("Log → %s", log_path)
    return logger


# ─────────────────────────────────────────────
# LAYER DISCOVERY  (must run on the main thread)
# ─────────────────────────────────────────────
def _find_layer(name):
    layers = QgsProject.instance().mapLayersByName(name)
    if not layers:
        raise DownloaderError(
            f'Layer "{name}" not found in the current project.\n'
            f'Available layers: '
            + ", ".join(f'"{n}"'
                        for n in sorted({l.name() for l in
                                         QgsProject.instance().mapLayers().values()})))
    return layers[0]


def discover_layers():
    """Called on the MAIN thread before the task is submitted."""
    wms_layer = _find_layer(WMS_LAYER_NAME)
    aoi_layer = _find_layer(AOI_LAYER_NAME)

    if not isinstance(wms_layer, QgsRasterLayer) or wms_layer.providerType() != "wms":
        raise DownloaderError(
            f'"{WMS_LAYER_NAME}" is not a WMS raster layer '
            f'(provider="{wms_layer.providerType()}").')

    geom_type = QgsWkbTypes.geometryType(aoi_layer.wkbType())
    if geom_type != QgsWkbTypes.PolygonGeometry:
        raise DownloaderError(
            f'"{AOI_LAYER_NAME}" is not a polygon layer '
            f'(geometry type = {QgsWkbTypes.displayString(aoi_layer.wkbType())}).')

    return wms_layer, aoi_layer


# ─────────────────────────────────────────────
# WMS PARAMETER EXTRACTION
# ─────────────────────────────────────────────
def extract_wms_params(wms_layer):
    uri = QgsDataSourceUri()
    uri.setEncodedUri(wms_layer.source())

    # 'url' can be stored under different keys depending on QGIS version
    base_url = (uri.param("url") or uri.param("URL") or "").strip()
    if not base_url:
        # Last resort: try to decode the raw source string
        raw = urllib.parse.unquote(wms_layer.source())
        for part in raw.split("&"):
            if part.lower().startswith("url="):
                base_url = part[4:]
                break

    if not base_url:
        raise DownloaderError(
            "Could not extract a base URL from the WMS layer source.\n"
            f"Raw source: {wms_layer.source()[:500]}")

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

    known = {"url","URL","layers","LAYERS","styles","STYLES",
             "crs","CRS","srs","SRS","format","FORMAT","dpiMode"}
    if hasattr(uri, "parameterKeys"):
        for key in uri.parameterKeys():
            if key not in known:
                params["extra"][key] = uri.param(key)

    return params


# ─────────────────────────────────────────────
# BLOCKING HTTP  (safe to call from a QgsTask worker thread)
# ─────────────────────────────────────────────
def _blocking_get(url, timeout_ms=REQUEST_TIMEOUT_MS):
    """
    Uses QgsBlockingNetworkRequest which is specifically designed for
    background-thread use.  Returns (status, headers_dict, body_bytes, error_str, timed_out).
    """
    req  = QgsBlockingNetworkRequest()
    qt_req = QNetworkRequest(QUrl(url))
    qt_req.setHeader(QNetworkRequest.UserAgentHeader, b"QGIS-WMS-AOI-Downloader/1.0")

    # QgsBlockingNetworkRequest.get() blocks the calling thread correctly.
    err_code = req.get(qt_req, forceRefresh=True)

    reply = req.reply()
    status  = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
    body    = bytes(reply.content())
    headers = {}
    for h in reply.rawHeaderList():
        key = bytes(h).decode("latin1").lower()
        headers[key] = bytes(reply.rawHeader(h)).decode("latin1")

    error_str  = None
    timed_out  = False

    if err_code == QgsBlockingNetworkRequest.NetworkError:
        error_str = reply.errorString()
        if "timeout" in (error_str or "").lower():
            timed_out = True
    elif err_code == QgsBlockingNetworkRequest.ServerExceptionError:
        error_str = reply.errorString()

    return status, headers, body, error_str, timed_out


# ─────────────────────────────────────────────
# GETCAPABILITIES
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


def fetch_capabilities(base_url, logger):
    url = _cap_url(base_url)
    logger.info("GetCapabilities → %s", url)
    status, headers, body, err, timed_out = _blocking_get(url)
    if timed_out:
        raise DownloaderError("Timed out fetching GetCapabilities.")
    if err:
        raise DownloaderError(f"Network error fetching GetCapabilities: {err}")
    if status and status >= 400:
        raise DownloaderError(f"GetCapabilities returned HTTP {status}.")
    if not body:
        raise DownloaderError("GetCapabilities returned an empty body.")
    try:
        return ET.fromstring(body)
    except ET.ParseError as e:
        raise DownloaderError(f"Cannot parse GetCapabilities XML: {e}\n"
                               f"First 500 bytes: {body[:500]}")


def _strip_ns(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def get_supported_formats(root):
    for elem in root.iter():
        if _strip_ns(elem.tag) == "GetMap":
            return [f.text.strip() for f in elem.iter()
                    if _strip_ns(f.tag) == "Format" and f.text]
    return []


def choose_format(available, logger):
    al = [f.lower() for f in available]
    for group in PREFERRED_FORMATS:
        for fmt in available:
            if any(tok in fmt.lower() for tok in group):
                logger.info("Selected format: %s", fmt)
                return fmt
    fallback = available[0] if available else "image/png"
    logger.warning("No preferred format matched; using %s", fallback)
    return fallback


# ─────────────────────────────────────────────
# TILE GRID
# ─────────────────────────────────────────────
def build_tile_grid(aoi_layer, request_crs_authid, logger,
                    tile_pixels=TILE_PIXELS, target_resolution=TARGET_RESOLUTION):
    req_crs = QgsCoordinateReferenceSystem(request_crs_authid)
    if not req_crs.isValid():
        raise DownloaderError(f"Request CRS '{request_crs_authid}' is invalid.")

    aoi_crs = aoi_layer.crs()
    ctx     = QgsProject.instance().transformContext()
    xform   = None if aoi_crs == req_crs else \
              QgsCoordinateTransform(aoi_crs, req_crs, ctx)

    geoms = []
    for feat in aoi_layer.getFeatures():
        g = QgsGeometry(feat.geometry())
        if g.isNull() or g.isEmpty():
            continue
        if xform:
            if g.transform(xform) != 0:
                logger.warning("Could not reproject feature id=%s; skipping.", feat.id())
                continue
        geoms.append(g)

    if not geoms:
        raise DownloaderError("AOI layer has no usable polygon geometries.")

    union = QgsGeometry.unaryUnion(geoms)
    bb    = union.boundingBox()
    step  = tile_pixels * target_resolution
    if step <= 0:
        raise DownloaderError("Tile size in map units is ≤ 0 – check target_resolution.")

    n_cols = max(1, math.ceil(bb.width()  / step))
    n_rows = max(1, math.ceil(bb.height() / step))
    logger.info("AOI bbox (req CRS): %s", bb.toString())
    logger.info("Grid: %d×%d tiles, %.2f map-units/tile", n_cols, n_rows, step)

    tiles, tid = [], 0
    for row in range(n_rows):
        for col in range(n_cols):
            xmin = bb.xMinimum() + col * step
            ymin = bb.yMinimum() + row * step
            xmax, ymax = xmin + step, ymin + step
            if QgsGeometry.fromRect(QgsRectangle(xmin, ymin, xmax, ymax)).intersects(union):
                tiles.append({"id": tid,
                               "xmin": xmin, "ymin": ymin,
                               "xmax": xmax, "ymax": ymax})
                tid += 1

    logger.info("Kept %d/%d tiles intersecting the AOI.", len(tiles), n_cols * n_rows)
    if not tiles:
        raise DownloaderError("No tiles intersect the AOI polygon.")
    return tiles


# ─────────────────────────────────────────────
# SQLITE WORK QUEUE
# ─────────────────────────────────────────────
class TileQueue:
    def __init__(self, db_path, logger):
        self.db_path = db_path
        self.logger  = logger
        self._c = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        self._c.execute("PRAGMA journal_mode=WAL;")
        self._c.execute("""
            CREATE TABLE IF NOT EXISTS tiles (
                id INTEGER PRIMARY KEY,
                xmin REAL, ymin REAL, xmax REAL, ymax REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                file_path TEXT, last_error TEXT, updated_at TEXT
            )""")
        self._c.execute("""
            CREATE TABLE IF NOT EXISTS job_meta (key TEXT PRIMARY KEY, value TEXT)""")

    def populate_if_empty(self, tiles, meta, work_dir=None):
        stored_fp = None
        try:
            row = self._c.execute(
                "SELECT value FROM job_meta WHERE key='fingerprint'").fetchone()
            if row:
                stored_fp = json.loads(row[0])
        except Exception:
            pass

        current_fp = meta.get("fingerprint")
        has_queue  = self._c.execute("SELECT COUNT(*) FROM tiles").fetchone()[0] > 0

        # Wipe whenever an existing queue's fingerprint is absent or differs
        # from the current job (TARGET_RESOLUTION / TILE_PIXELS / AOI changed,
        # or the queue predates fingerprinting). Treating a missing stored
        # fingerprint as a mismatch is what makes a resolution change actually
        # take effect instead of silently resuming the old tiles.
        if has_queue and current_fp and stored_fp != current_fp:
            self.logger.warning(
                "Job parameters have changed (fingerprint %s -> %s). "
                "Wiping old work queue and starting fresh.", stored_fp, current_fp)
            self.close()
            # Delete the specific work artifacts rather than rmtree(work_dir):
            # the live download.log lives in work_dir and is still open here
            # (locked on Windows), which makes a whole-folder rmtree unreliable.
            for name in ("tiles.sqlite", "tiles.sqlite-wal", "tiles.sqlite-shm",
                         "mosaic.vrt", "mosaic.tif"):
                try: os.remove(os.path.join(work_dir, name))
                except OSError: pass
            tiles_dir = os.path.join(work_dir, "tiles")
            if os.path.isdir(tiles_dir):
                import shutil
                shutil.rmtree(tiles_dir, ignore_errors=True)
            os.makedirs(tiles_dir, exist_ok=True)
            self._c = sqlite3.connect(
                os.path.join(work_dir, "tiles.sqlite"), timeout=30, isolation_level=None)
            self._c.execute("PRAGMA journal_mode=WAL;")
            self._c.execute("""
                CREATE TABLE IF NOT EXISTS tiles (
                    id INTEGER PRIMARY KEY,
                    xmin REAL, ymin REAL, xmax REAL, ymax REAL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    file_path TEXT, last_error TEXT, updated_at TEXT
                )""")
            self._c.execute("""
                CREATE TABLE IF NOT EXISTS job_meta (key TEXT PRIMARY KEY, value TEXT)""")
            has_queue = False

        if has_queue:
            self.logger.info("Resuming existing queue (fingerprint=%s).", stored_fp)
            return
        self._c.execute("BEGIN")
        self._c.executemany(
            "INSERT INTO tiles (id,xmin,ymin,xmax,ymax) VALUES (?,?,?,?,?)",
            [(t["id"],t["xmin"],t["ymin"],t["xmax"],t["ymax"]) for t in tiles])
        for k, v in meta.items():
            self._c.execute("INSERT INTO job_meta VALUES (?,?)", (k, json.dumps(v)))
        self._c.execute("COMMIT")
        self.logger.info("Queued %d tiles (fingerprint=%s).", len(tiles), current_fp)

    def pending_tiles(self):
        return self._c.execute(
            "SELECT id,xmin,ymin,xmax,ymax,attempts FROM tiles "
            "WHERE status='pending' ORDER BY id").fetchall()

    def total(self):
        return self._c.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]

    def counts(self):
        r = {"pending": 0, "done": 0, "failed": 0}
        for s, n in self._c.execute(
                "SELECT status,COUNT(*) FROM tiles GROUP BY status"):
            r[s] = n
        return r

    def mark_attempt(self, tid, attempts):
        self._c.execute(
            "UPDATE tiles SET attempts=?,updated_at=? WHERE id=?",
            (attempts, datetime.utcnow().isoformat(), tid))

    def mark_done(self, tid, path):
        self._c.execute(
            "UPDATE tiles SET status='done',file_path=?,last_error=NULL,updated_at=? WHERE id=?",
            (path, datetime.utcnow().isoformat(), tid))

    def mark_failed(self, tid, err):
        self._c.execute(
            "UPDATE tiles SET status='failed',last_error=?,updated_at=? WHERE id=?",
            (str(err)[:2000], datetime.utcnow().isoformat(), tid))

    def done_file_paths(self):
        return [r[0] for r in
                self._c.execute(
                    "SELECT file_path FROM tiles "
                    "WHERE status='done' AND file_path IS NOT NULL")
                if r[0] and os.path.exists(r[0])]

    def close(self):
        try: self._c.close()
        except Exception: pass


# ─────────────────────────────────────────────
# TILE FETCH + VALIDATION
# ─────────────────────────────────────────────
def _getmap_url(base_url, wms_params, fmt, tile, w, h):
    p  = list(urllib.parse.urlparse(base_url))
    q  = dict(urllib.parse.parse_qsl(p[4], keep_blank_values=True))
    lk = {k.lower(): k for k in q}

    def s(name, val):
        q[lk.get(name.lower(), name.upper())] = val

    version = q.get(lk.get("version", "VERSION"), "1.3.0")
    s("service", "WMS");  s("request", "GetMap"); s("version", version)
    s("layers",  ",".join(wms_params["layers"]))
    s("styles",  ",".join(wms_params["styles"]))
    s("format",  fmt)
    s("transparent", q.get(lk.get("transparent","TRANSPARENT"), "TRUE"))
    s("width",  str(w));  s("height", str(h))

    crs = wms_params["crs"]
    # WMS 1.3.0 axis order: geographic CRS (EPSG:4326 etc.) use Y,X (lat,lon).
    # Projected metric CRS like EPSG:32632 (UTM) always use X,Y (easting,northing).
    # Many servers including the Italian PCN geoportal expect X,Y regardless,
    # so we only flip to Y,X for the small set of known geographic CRS.
    YX_CRS = {"EPSG:4326", "CRS:84", "EPSG:4258"}
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

    for k, v in (wms_params.get("extra") or {}).items():
        if v is not None and k.lower() not in (
                "tilepixelratio","contextualwmslegend","featurecount","dpimode"):
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


def _validate_image(body):
    if not body or len(body) < 64:
        return "Body too small to be a valid image."
    if gdal is None:
        return None   # can't validate without GDAL; accept on size
    mem = f"/vsimem/tile_val_{id(body)}.tmp"
    gdal.FileFromMemBuffer(mem, body)
    try:
        ds = gdal.Open(mem)
        if ds is None:
            return "GDAL: cannot open as raster (corrupt?)."
        if ds.RasterCount == 0 or ds.RasterXSize == 0:
            return "Zero-size raster."

        # Empty-tile detection: only flag as empty when the alpha channel
        # (band 4) is entirely zero — i.e. the server returned a fully
        # transparent tile.  We do NOT use pixel-value uniformity as a
        # proxy because real orthophoto tiles can legitimately be all-black
        # (night/shadow) or all-white (clouds/snow).
        if ds.RasterCount >= 4:
            alpha = ds.GetRasterBand(4)
            # ComputeStatistics(approx_ok) – correct name in GDAL 2.x / 3.x
            try:
                stats = alpha.ComputeStatistics(True)   # [min, max, mean, std]
            except Exception:
                stats = alpha.GetStatistics(True, True)  # fallback
            if stats[1] == 0:   # max alpha == 0  →  fully transparent
                return "EMPTY_TILE"

        return None
    finally:
        try: gdal.Unlink(mem)
        except Exception: pass


def _parse_retry_after(value):
    if not value:
        return None
    try:
        return float(value.strip())
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value.strip())
        return max(0.0, (dt - datetime.utcnow().replace(tzinfo=dt.tzinfo)).total_seconds())
    except Exception:
        return None


def fetch_one_tile(wms_params, image_format, tile, out_path, logger,
                   tile_pixels=TILE_PIXELS):
    url = _getmap_url(wms_params["url"], wms_params, image_format,
                      tile, tile_pixels, tile_pixels)
    logger.debug("GetMap tile %d: %s", tile["id"], url)
    if tile["id"] == 0:
        logger.info("FIRST TILE URL (paste into browser to verify): %s", url)
    status, headers, body, err, timed_out = _blocking_get(url)

    if timed_out:
        raise TileFetchError("Request timed out.")
    if err:
        raise TileFetchError(f"Network error: {err}")
    if status == 429:
        ra = _parse_retry_after(headers.get("retry-after"))
        raise TileFetchError("HTTP 429.", retry_after=ra, is_throttle=True)
    if status in (500, 503):
        ra = _parse_retry_after(headers.get("retry-after"))
        raise TileFetchError(f"HTTP {status}.", retry_after=ra, is_throttle=True)
    if status and status >= 400:
        raise TileFetchError(f"HTTP {status}.")
    if _is_xml_exception(body):
        raise TileFetchError(f"WMS ServiceException: {_parse_exception(body)}")

    problem = _validate_image(body)
    if problem == "EMPTY_TILE":
        return None          # legitimate empty area – mark done, no file
    if problem:
        raise TileFetchError(f"Invalid image: {problem}")

    with open(out_path, "wb") as f:
        f.write(body)
    return out_path


# ─────────────────────────────────────────────
# ADAPTIVE THROTTLE
# ─────────────────────────────────────────────
class AdaptiveThrottle:
    def __init__(self, logger):
        self._d   = INITIAL_DELAY_SEC
        self._ok  = 0
        self._log = logger

    def wait(self, cancel_check=None):
        rem = self._d
        while rem > 0:
            if cancel_check and cancel_check():
                return
            time.sleep(min(0.1, rem))
            rem -= 0.1

    def on_success(self):
        self._ok += 1
        if self._ok >= SUCCESSES_BEFORE_SPEEDUP:
            new = max(MIN_DELAY_SEC, self._d * SPEEDUP_FACTOR)
            if new != self._d:
                self._log.debug("Throttle ↑ %.3f→%.3f", self._d, new)
            self._d, self._ok = new, 0

    def _slow(self, new, reason):
        self._ok = 0
        self._log.warning("Throttle ↓ %.3f→%.3f (%s)", self._d, new, reason)
        self._d = new

    def on_throttle(self, retry_after=None):
        new = min(MAX_DELAY_SEC,
                  max(self._d, retry_after) if retry_after else self._d * SLOWDOWN_FACTOR)
        self._slow(new, f"throttle retry_after={retry_after}")

    def on_timeout(self):
        self._slow(min(MAX_DELAY_SEC, self._d * SLOWDOWN_FACTOR), "timeout")


# ─────────────────────────────────────────────
# MOSAIC
# ─────────────────────────────────────────────
def build_mosaic(tile_paths, work_dir, logger, tif_path=None):
    if gdal is None:
        raise DownloaderError("GDAL Python bindings unavailable; cannot build mosaic.")
    if not tile_paths:
        raise DownloaderError("No downloaded tiles to mosaic.")

    vrt = os.path.join(work_dir, "mosaic.vrt")
    tif = tif_path or os.path.join(work_dir, "mosaic.tif")
    out_dir = os.path.dirname(tif)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    logger.info("BuildVRT from %d tiles → %s", len(tile_paths), vrt)
    ds = gdal.BuildVRT(vrt, tile_paths,
                       options=gdal.BuildVRTOptions(resampleAlg="nearest", addAlpha=True))
    if ds is None:
        raise DownloaderError("gdal.BuildVRT failed.")
    ds = None

    logger.info("Translating VRT → %s", tif)
    ds = gdal.Translate(tif, vrt,
                        options=gdal.TranslateOptions(
                            format="GTiff",
                            creationOptions=["COMPRESS=DEFLATE","PREDICTOR=2",
                                             "TILED=YES","BLOCKXSIZE=256","BLOCKYSIZE=256",
                                             "BIGTIFF=IF_SAFER"]))
    if ds is None:
        raise DownloaderError("gdal.Translate failed.")
    ds.BuildOverviews("AVERAGE", [2, 4, 8, 16])
    ds = None
    return vrt, tif


# ─────────────────────────────────────────────
# QGSTASK
# ─────────────────────────────────────────────
class WmsAoiDownloadTask(QgsTask):

    def __init__(self, wms_layer, aoi_layer, wms_params,
                 tile_pixels=TILE_PIXELS, target_resolution=TARGET_RESOLUTION,
                 output_path=None):
        super().__init__("WMS AOI download", QgsTask.CanCancel)
        # Store everything resolved on the main thread
        self._wms_params       = wms_params
        self._aoi_layer_id     = aoi_layer.id()
        self._req_crs          = wms_params["crs"] or wms_layer.crs().authid()
        self._tile_pixels      = int(tile_pixels)
        self._target_resolution = float(target_resolution)
        # None → default to <work_dir>/mosaic.tif (resolved in build_mosaic)
        self._output_path      = output_path or None

        project = QgsProject.instance()
        base_dir = (os.path.dirname(project.fileName())
                    if project.fileName()
                    else QgsApplication.qgisSettingsDirPath())
        self.work_dir = os.path.join(base_dir, WORK_SUBDIR_NAME)
        os.makedirs(os.path.join(self.work_dir, "tiles"), exist_ok=True)

        self.result_tif_path = None
        self.exception       = None
        self.logger          = _build_logger(self.work_dir)

    # ── worker thread ──────────────────────────────────────────────────────
    def run(self):
        try:
            self._run_impl()
            return True
        except DownloaderError as e:
            self.exception = e
            self.logger.error("Fatal: %s", e)
            return False
        except Exception as e:
            self.exception = e
            self.logger.error("Unexpected: %s\n%s", e, traceback.format_exc())
            return False

    def _run_impl(self):
        logger = self.logger
        logger.info("=== WMS AOI Download starting ===")

        # Re-resolve the AOI layer safely (read-only access, geometry iteration
        # is safe from a background thread in QGIS 3.x).
        aoi_layer = QgsProject.instance().mapLayer(self._aoi_layer_id)
        if aoi_layer is None:
            raise DownloaderError("AOI layer was removed from the project.")

        tiles = build_tile_grid(aoi_layer, self._req_crs, logger,
                                self._tile_pixels, self._target_resolution)

        fingerprint = _job_fingerprint(
            self._wms_params, self._tile_pixels, self._target_resolution, aoi_layer)
        logger.info("Job fingerprint: %s", fingerprint)

        # GetCapabilities
        caps  = fetch_capabilities(self._wms_params["url"], logger)
        fmts  = get_supported_formats(caps)
        logger.info("Advertised formats: %s", fmts)
        fmt   = choose_format(fmts, logger)

        ext = ".tif" if "tif" in fmt.lower() else ".png"

        # Queue
        db_path = os.path.join(self.work_dir, "tiles.sqlite")
        queue   = TileQueue(db_path, logger)
        try:
            queue.populate_if_empty(tiles, {
                "url": self._wms_params["url"],
                "layers": self._wms_params["layers"],
                "crs": self._req_crs, "format": fmt,
                "tile_pixels": self._tile_pixels, "resolution": self._target_resolution,
                "fingerprint": fingerprint,
            }, work_dir=self.work_dir)

            total = queue.total()
            logger.info("Queue: %s  (total=%d)", queue.counts(), total)

            throttle = AdaptiveThrottle(logger)
            logger.info("Sleeping %.1fs before first request…", INITIAL_DELAY_SEC)
            self._sleep(INITIAL_DELAY_SEC)

            processed = 0
            tiles_dir = os.path.join(self.work_dir, "tiles")

            while True:
                if self.isCanceled():
                    logger.warning("Cancelled. Queue checkpointed in %s", db_path)
                    return

                pending = queue.pending_tiles()
                if not pending:
                    break

                tid, xmin, ymin, xmax, ymax, attempts = pending[0]
                tile     = {"id": tid, "xmin": xmin, "ymin": ymin,
                            "xmax": xmax, "ymax": ymax}
                out_path = os.path.join(tiles_dir, f"tile_{tid:06d}{ext}")

                success, last_err = False, None

                while attempts < MAX_ATTEMPTS_PER_TILE:
                    if self.isCanceled():
                        return
                    attempts += 1
                    queue.mark_attempt(tid, attempts)

                    try:
                        throttle.wait(self.isCanceled)
                        if self.isCanceled():
                            return

                        result = fetch_one_tile(
                            self._wms_params, fmt, tile, out_path, logger,
                            self._tile_pixels)
                        throttle.on_success()
                        queue.mark_done(tid, result)
                        success = True
                        logger.info("Tile %d OK%s", tid,
                                    "" if result else " (empty/NoData)")
                        break

                    except TileFetchError as e:
                        last_err = e
                        if "timed out" in str(e).lower():
                            throttle.on_timeout()
                        elif e.is_throttle:
                            throttle.on_throttle(e.retry_after)
                        else:
                            logger.warning("Tile %d attempt %d: %s", tid, attempts, e)

                        if attempts < MAX_ATTEMPTS_PER_TILE:
                            backoff = min(MAX_DELAY_SEC,
                                          (2 ** (attempts - 1)) * max(throttle._d, 0.5))
                            logger.info("Tile %d retry in %.1fs (%d/%d)",
                                        tid, backoff, attempts+1, MAX_ATTEMPTS_PER_TILE)
                            self._sleep(backoff)

                if not success:
                    logger.error("Tile %d failed permanently: %s", tid, last_err)
                    queue.mark_failed(tid, last_err)

                processed += 1
                c = queue.counts()
                done_n = c["done"] + c["failed"]
                self.setProgress(100.0 * done_n / total if total else 100.0)
                if processed % 10 == 0:
                    logger.info("Checkpoint %d/%d (%s)", done_n, total, c)

            c = queue.counts()
            logger.info("All tiles resolved: %s", c)

            tile_paths = queue.done_file_paths()
            if not tile_paths:
                raise DownloaderError("No tiles downloaded; cannot build mosaic.")

            vrt_path, tif_path = build_mosaic(
                tile_paths, self.work_dir, logger, self._output_path)
            self.result_tif_path = tif_path

            if CLEANUP_TILES_AFTER_MOSAIC:
                for p in tile_paths:
                    try: os.remove(p)
                    except OSError: pass
                try: os.remove(vrt_path)
                except OSError: pass

            logger.info("=== Done. Mosaic → %s ===", tif_path)

        finally:
            queue.close()
            _release_logger()

    def _sleep(self, seconds):
        rem = float(seconds)
        while rem > 0:
            if self.isCanceled():
                return
            time.sleep(min(0.1, rem))
            rem -= 0.1


# ─────────────────────────────────────────────
# LOGGER CLEANUP
# ─────────────────────────────────────────────
def _release_logger():
    """Close and remove all handlers from the module logger so the log file
    is released and can be deleted/overwritten on the next run."""
    log = logging.getLogger("wms_aoi_downloader")
    for h in list(log.handlers):
        try:
            h.flush()
            h.close()
        except Exception:
            pass
        log.removeHandler(h)


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def run(wms_layer=None, aoi_layer=None, tile_pixels=None, target_resolution=None,
        output_path=None, temporary=False):
    """
    Start a download task.

    Plugin path: the dialog passes the resolved layers and config in directly.

    Console path (uses the default layer names + module config):
        import wms_aoi_downloader; wms_aoi_downloader.run()

    Output:
      temporary=True   → mosaic written to a unique file in the system temp dir.
      output_path=...  → mosaic written there.
      neither          → defaults to <work_dir>/mosaic.tif.
    """
    # Refuse to start a second task if one is already active so two launches
    # can't race on the same work_dir / tiles.sqlite.
    TASK_DESC = "WMS AOI download"
    for t in QgsApplication.taskManager().activeTasks():
        if t.description() == TASK_DESC:
            msg = ("A 'WMS AOI download' task is already running; not starting "
                   "another. Cancel it in the Task Manager first if you want to restart.")
            print(f"[WMS Downloader] {msg}")
            QgsMessageLog.logMessage(msg, "WMS AOI Downloader", Qgis.Warning)
            return t

    tile_pixels       = int(tile_pixels) if tile_pixels else TILE_PIXELS
    target_resolution = float(target_resolution) if target_resolution else TARGET_RESOLUTION

    if temporary:
        # Lands in QGIS's managed temp dir, which QGIS cleans up itself.
        output_path = QgsProcessingUtils.generateTempFilename(
            f"wms_aoi_mosaic_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tif")
    # else: output_path may be an explicit path, or None to default to
    # <work_dir>/mosaic.tif inside build_mosaic.

    # Layers are resolved on the MAIN thread before the task is submitted. The
    # plugin passes them in; the console path locates them by configured name.
    if wms_layer is None or aoi_layer is None:
        print("[WMS Downloader] Discovering layers…")
        try:
            wms_layer, aoi_layer = discover_layers()
        except DownloaderError as e:
            QgsMessageLog.logMessage(str(e), "WMS AOI Downloader", Qgis.Critical)
            print(f"[WMS Downloader] ERROR: {e}")
            return None

    try:
        wms_params = extract_wms_params(wms_layer)
    except DownloaderError as e:
        QgsMessageLog.logMessage(str(e), "WMS AOI Downloader", Qgis.Critical)
        print(f"[WMS Downloader] ERROR: {e}")
        return None

    print(f"[WMS Downloader] WMS URL  : {wms_params['url']}")
    print(f"[WMS Downloader] Layers   : {wms_params['layers']}")
    print(f"[WMS Downloader] CRS      : {wms_params['crs']}")
    print("[WMS Downloader] Submitting task…")

    task = WmsAoiDownloadTask(wms_layer, aoi_layer, wms_params,
                              tile_pixels, target_resolution, output_path)

    def _finished(success):
        _release_logger()   # always release the log file handle when the task ends
        if success and task.result_tif_path and os.path.exists(task.result_tif_path):
            layer_name = os.path.splitext(
                os.path.basename(task.result_tif_path))[0].replace("_", " ")
            lyr = QgsRasterLayer(task.result_tif_path, layer_name)
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
                print(f"[WMS Downloader] Mosaic loaded: {task.result_tif_path}")
                QgsMessageLog.logMessage(
                    f"Mosaic loaded: {task.result_tif_path}",
                    "WMS AOI Downloader", Qgis.Info)
            else:
                msg = f"Mosaic file invalid: {task.result_tif_path}"
                print(f"[WMS Downloader] WARNING: {msg}")
                QgsMessageLog.logMessage(msg, "WMS AOI Downloader", Qgis.Critical)
        elif not success:
            msg = str(task.exception) if task.exception else "Task failed."
            print(f"[WMS Downloader] FAILED: {msg}")
            QgsMessageLog.logMessage(f"Task failed: {msg}",
                                      "WMS AOI Downloader", Qgis.Critical)

    task.taskCompleted.connect(lambda: _finished(True))
    task.taskTerminated.connect(lambda: _finished(False))
    QgsApplication.taskManager().addTask(task)

    print("[WMS Downloader] Task queued. Watch the QGIS Task Manager panel,")
    print(f"                 the Messages log (tab 'WMS AOI Downloader'),")
    print(f"                 and: {os.path.join(task.work_dir, 'download.log')}")
    return task


# NOTE: as a plugin engine this module must NOT auto-run on import.
# The plugin calls core.run(...) from the dialog; console users call run().

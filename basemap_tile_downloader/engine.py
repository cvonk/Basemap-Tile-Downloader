# -*- coding: utf-8 -*-
"""
engine.py – shared download engine for the Basemap Tile Downloader plugin
================================================================

Source-agnostic machinery: blocking HTTP, adaptive throttle, a resumable SQLite
work queue, tile georeferencing, GDAL mosaicking, and the QgsTask that drives a
run. The WMS- and XYZ-specific bits live in sources/wms.py and sources/xyz.py;
this module dispatches to whichever one matches the chosen layer.

A "source" module exposes:
    SOURCE_NAME
    detect(layer) -> bool
    extract_params(layer) -> dict
    prepare(params, opts, logger) -> None        # optional (WMS caps/format)
    native_crs(params, opts) -> str
    default_out_crs(params) -> str
    build_tile_grid(extent_geom, extent_crs, params, opts, logger) -> list[dict]  # each has "id"
    fetch_one_tile(params, opts, tile, out_path, logger) -> path|None
    fingerprint_parts(params, opts) -> list
"""

import os, re, json, sqlite3, logging, time, traceback, hashlib, uuid, configparser
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone

from qgis.PyQt.QtCore   import QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkRequest

from qgis.core import (
    Qgis, QgsProject, QgsTask, QgsApplication, QgsMessageLog,
    QgsRasterLayer, QgsBlockingNetworkRequest,
    QgsGeometry, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
)

try:
    from osgeo import gdal
    gdal.UseExceptions()
except ImportError:
    gdal = None

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
MAX_ATTEMPTS_PER_TILE    = 6
# Throttle/timeout responses are back-pressure, not the tile's fault, so they
# get their own (larger) retry budget instead of burning the error budget above.
# Still bounded, so a server that returns 429/403 forever can't loop indefinitely.
MAX_BACKPRESSURE_RETRIES = 8       # bounded so a persistently-broken tile gives up
                                   # in minutes, not hours
# Run-level circuit breaker: the per-tile budget above bounds each tile, but a
# whole run can still grind for hours when a server refuses a large block of
# tiles (every request 429s / ServiceExceptions, throttle pinned at the cap, the
# same tiles requeued round-robin). If this many requests fail in a row with NO
# successful tile in between, treat the server as down for this run, stop, and
# build a partial mosaic from whatever downloaded (the rest stay 'pending', so a
# re-run resumes them). At the 30s back-off cap this trips after ~10 minutes.
MAX_CONSECUTIVE_BACKPRESSURE = 30
INITIAL_DELAY_SEC        = 0.5
MIN_DELAY_SEC            = 0.05
MAX_DELAY_SEC            = 30.0    # ceiling for our *adaptive* (guessed) backoff
RETRY_AFTER_MAX_SEC      = 300.0   # ceiling for a *server-directed* Retry-After wait
                                   # (honoured beyond the adaptive cap, but bounded so
                                   # an absurd value can't stall the run indefinitely)
SPEEDUP_FACTOR           = 0.85
SLOWDOWN_FACTOR          = 2.0
SUCCESSES_BEFORE_SPEEDUP = 3
REQUEST_TIMEOUT_MS       = 60_000
CONCURRENCY              = 4       # default parallel tile fetches; a source may
                                   # override via a CONCURRENCY attribute, and the
                                   # dialog lets the user set it per run

CLEANUP_TILES_AFTER_MOSAIC = False
WORK_SUBDIR_NAME = "__btdcache__"
LOG_TAB          = "Basemap Tile Downloader"
TASK_DESC        = "Basemap tile download"       # remote sources (WMS/WMTS/XYZ)
TASK_DESC_LOCAL  = "Basemap raster export"       # a local raster (GeoTIFF) read
TASK_DESCS       = (TASK_DESC, TASK_DESC_LOCAL)   # any of ours, for the run guard


def _plugin_version():
    """Version string from this plugin's metadata.txt (matches the released git
    tag). Empty string if it can't be read."""
    try:
        cp = configparser.ConfigParser(interpolation=None)   # tolerate % in values
        cp.read(os.path.join(os.path.dirname(__file__), "metadata.txt"),
                encoding="utf-8")
        return cp.get("general", "version", fallback="").strip()
    except Exception:
        return ""


def _first_line(s, limit=200):
    """First line of a (possibly multi-line) error message, truncated — used to
    keep the log readable when an error repeats across many tiles."""
    return next(iter((s or "").splitlines()), "")[:limit]


# ─────────────────────────────────────────────
# EXCEPTIONS
# ─────────────────────────────────────────────
class DownloaderError(Exception):
    """Fatal, non-retryable."""

class TileFetchError(Exception):
    def __init__(self, message, retry_after=None, is_throttle=False,
                 is_server_error=False):
        super().__init__(message)
        self.retry_after = retry_after
        # is_server_error: a transient server-side failure (e.g. a WMS
        # ServiceException about a file it momentarily can't read). Retried on
        # the back-pressure budget with backoff, not the per-tile error budget.
        self.is_server_error = is_server_error
        self.is_throttle = is_throttle


# ─────────────────────────────────────────────
# SOURCE DISPATCH  (late import to avoid a cycle)
# ─────────────────────────────────────────────
def _source_modules():
    from .sources import wms, xyz, wmts, gdal_raster
    return (xyz, wmts, wms, gdal_raster)


def source_for(layer):
    """Return the source backend module that handles `layer`, or None."""
    if not isinstance(layer, QgsRasterLayer):
        return None
    for mod in _source_modules():
        try:
            if mod.detect(layer):
                return mod
        except Exception:  # nosec B110
            pass
    return None


# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
def build_logger(work_dir):
    logger = logging.getLogger("basemap_tile_downloader")
    logger.setLevel(logging.DEBUG)
    logger.handlers = []

    log_path = os.path.join(work_dir, "download.log")
    # mode="w": start each run with a fresh log so it doesn't grow unbounded
    # across re-runs (the resumable queue lives in the SQLite DB, not the log).
    fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
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
                QgsMessageLog.logMessage(self.format(record), LOG_TAB, lvl)
            except Exception:  # nosec B110
                pass

    qh = _QH(); qh.setLevel(logging.INFO)
    qh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(qh)
    logger.info("Log → %s", log_path)
    return logger


def release_logger():
    log = logging.getLogger("basemap_tile_downloader")
    for h in list(log.handlers):
        try: h.flush(); h.close()
        except Exception: pass  # nosec B110
        log.removeHandler(h)


# ─────────────────────────────────────────────
# BLOCKING HTTP  (worker-thread safe)
# ─────────────────────────────────────────────
def blocking_get(url, timeout_ms=REQUEST_TIMEOUT_MS):
    req    = QgsBlockingNetworkRequest()
    qt_req = QNetworkRequest(QUrl(url))
    qt_req.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader,
                     b"QGIS-Basemap-Tile-Downloader/1.0")
    try:
        qt_req.setTransferTimeout(int(timeout_ms))    # Qt 5.15+ (QGIS 3.16+)
    except (AttributeError, TypeError):
        pass
    err_code = req.get(qt_req, forceRefresh=True)

    reply  = req.reply()
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    body   = bytes(reply.content())
    headers = {}
    for h in reply.rawHeaderList():
        headers[bytes(h).decode("latin1").lower()] = \
            bytes(reply.rawHeader(h)).decode("latin1")

    error_str, timed_out = None, False
    # QgsBlockingNetworkRequest reports a transfer timeout via its own
    # TimeoutError code; older builds fold it into NetworkError with a
    # "timeout" errorString. Handle both so a timed-out tile is retried
    # rather than being mistaken for an empty response (a permanent gap).
    timeout_code = getattr(QgsBlockingNetworkRequest, "TimeoutError", None)
    if timeout_code is not None and err_code == timeout_code:
        timed_out = True
        error_str = reply.errorString() or "Request timed out."
    elif err_code == QgsBlockingNetworkRequest.NetworkError:
        error_str = reply.errorString()
        low = (error_str or "").lower()
        if "timeout" in low or "timed out" in low:
            timed_out = True
    elif err_code == QgsBlockingNetworkRequest.ServerExceptionError:
        error_str = reply.errorString()
    return status, headers, body, error_str, timed_out


def parse_retry_after(value):
    if not value:
        return None
    try:
        return float(value.strip())
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value.strip())
        if dt is None:
            return None
        # Compare against "now" in the same awareness/zone as the parsed date,
        # so a non-GMT offset (or a naive date) yields the right delay.
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        return max(0.0, (dt - now).total_seconds())
    except Exception:
        return None


# ─────────────────────────────────────────────
# GEOREFERENCE A TILE  (used by every source)
# ─────────────────────────────────────────────
def georeference(body, out_tif, bounds, srs):
    """
    Write raw tile bytes to a GeoTIFF stamped with `bounds`
    (ulx, uly, lrx, lry) in CRS `srs`. Returns None on success, or an error
    string. A fully-transparent tile is written like any other (not dropped).
    """
    if gdal is None:
        raise DownloaderError("GDAL bindings unavailable; cannot georeference tiles.")
    mem = f"/vsimem/basemap_tile_{uuid.uuid4().hex}"     # unique per call (thread-safe)
    gdal.FileFromMemBuffer(mem, body)
    ds = None
    try:
        ds = gdal.Open(mem)
        if ds is None:
            return "GDAL: cannot open tile (not an image?)."
        if ds.RasterXSize == 0 or ds.RasterCount == 0:
            return "Zero-size raster."
        ulx, uly, lrx, lry = bounds
        gdal.Translate(out_tif, ds, options=gdal.TranslateOptions(
            format="GTiff", outputSRS=srs, outputBounds=[ulx, uly, lrx, lry]))
        return None
    finally:
        ds = None
        try: gdal.Unlink(mem)
        except Exception: pass  # nosec B110


# ─────────────────────────────────────────────
# ADAPTIVE THROTTLE
# ─────────────────────────────────────────────
class AdaptiveThrottle:
    def __init__(self, logger, initial_delay=INITIAL_DELAY_SEC, min_delay=0.0,
                 max_delay=MAX_DELAY_SEC):
        self._d, self._ok, self._log = max(initial_delay, min_delay), 0, logger
        self._extra = 0.0     # one-shot wait honouring a server-directed Retry-After
        self._min = max(0.0, float(min_delay))   # user floor on the pace (default 0)
        self._max = max(self._min, float(max_delay))   # ceiling for adaptive back-off

    def status(self):
        """Human-readable current pacing, for the log."""
        base = max(self._min, self._d)
        if self._extra:
            return f"{base:.2f}s + {self._extra:.1f}s one-shot"
        return f"{base:.2f}s"

    def _floor_note(self, new):
        """When the user's minimum delay is above the adaptive value, the real
        pace is the floor, not `new` — say so, so the Throttle line isn't misread
        as pacing below the configured minimum."""
        return f" (floor {self._min:.2f}s in effect)" if self._min > new else ""

    def wait(self, cancel_check=None):
        # Never pace faster than the user's minimum delay (default 0 = no floor).
        rem = max(self._min, self._d) + self._extra   # + any one-shot Retry-After
        self._extra = 0.0
        while rem > 0:
            if cancel_check and cancel_check():
                return
            time.sleep(min(0.1, rem)); rem -= 0.1

    def on_success(self):
        self._ok += 1
        if self._ok >= SUCCESSES_BEFORE_SPEEDUP:
            new = max(MIN_DELAY_SEC, self._d * SPEEDUP_FACTOR)
            if new != self._d:
                self._log.debug("Throttle ↑ %.3f→%.3f%s", self._d, new,
                                self._floor_note(new))
            self._d, self._ok = new, 0

    def _slow(self, new, reason):
        self._ok = 0
        self._log.warning("Throttle ↓ %.3f→%.3f (%s)%s", self._d, new, reason,
                          self._floor_note(new))
        self._d = new

    def on_throttle(self, retry_after=None, reason="rate-limit"):
        # Adaptive (guessed) backoff is capped at the (configurable) ceiling.
        new = min(self._max, self._d * SLOWDOWN_FACTOR)
        if retry_after and retry_after > 0:
            # The server told us exactly how long to wait: honour it as a bounded
            # one-shot pause. We only nudge the adaptive baseline up modestly, so
            # the run speeds back up promptly once the rate window has passed
            # instead of decaying down from the full Retry-After value.
            self._extra = max(self._extra, min(RETRY_AFTER_MAX_SEC, retry_after))
            self._ok = 0
            self._log.warning("Throttle ↓ %.3f→%.3f + one-shot %.1fs (server Retry-After)%s",
                              self._d, new, self._extra, self._floor_note(new))
            self._d = new
        else:
            self._slow(new, reason)

    def on_timeout(self):
        self._slow(min(self._max, self._d * SLOWDOWN_FACTOR), "timeout")


# ─────────────────────────────────────────────
# SQLITE WORK QUEUE  (generic: each tile stored as a JSON spec)
# ─────────────────────────────────────────────
_TILES_DDL = """
    CREATE TABLE IF NOT EXISTS tiles (
        id INTEGER PRIMARY KEY,
        spec TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        file_path TEXT, last_error TEXT, updated_at TEXT
    )"""
_META_DDL = "CREATE TABLE IF NOT EXISTS job_meta (key TEXT PRIMARY KEY, value TEXT)"


class TileQueue:
    def __init__(self, db_path, logger):
        self.db_path, self.logger = db_path, logger
        self.work_dir = os.path.dirname(db_path)
        self._c = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        self._c.execute("PRAGMA journal_mode=WAL;")
        self._c.execute(_TILES_DDL)
        self._c.execute(_META_DDL)

    def _resolve(self, p):
        """Absolute path for a stored file_path. New rows are stored relative to
        the job's work_dir (so the cache is relocatable); legacy rows are already
        absolute and are used as-is."""
        if not p:
            return p
        return os.path.normpath(p if os.path.isabs(p) else os.path.join(self.work_dir, p))

    def populate_if_empty(self, tiles, meta, work_dir=None):
        stored_fp = None
        try:
            row = self._c.execute(
                "SELECT value FROM job_meta WHERE key='fingerprint'").fetchone()
            if row:
                stored_fp = json.loads(row[0])
        except Exception:  # nosec B110
            pass

        current_fp = meta.get("fingerprint")
        has_queue  = self._c.execute("SELECT COUNT(*) FROM tiles").fetchone()[0] > 0

        if has_queue and current_fp and stored_fp != current_fp:
            self.logger.warning(
                "Job parameters changed (fingerprint %s -> %s). Wiping queue.",
                stored_fp, current_fp)
            self.close()
            for name in ("tiles.sqlite", "tiles.sqlite-wal", "tiles.sqlite-shm",
                         "mosaic.vrt", "mosaic.tif"):
                try: os.remove(os.path.join(work_dir, name))
                except OSError: pass
            tiles_dir = os.path.join(work_dir, "tiles")
            if os.path.isdir(tiles_dir):
                import shutil; shutil.rmtree(tiles_dir, ignore_errors=True)
            os.makedirs(tiles_dir, exist_ok=True)
            self._c = sqlite3.connect(
                os.path.join(work_dir, "tiles.sqlite"), timeout=30, isolation_level=None)
            self._c.execute("PRAGMA journal_mode=WAL;")
            self._c.execute(_TILES_DDL)
            self._c.execute(_META_DDL)
            has_queue = False

        if has_queue:
            # Re-queue any previously-failed tiles so a re-run recovers gaps left
            # by transient server errors (the earlier run's 'done' tiles are kept).
            requeued = self._c.execute(
                "UPDATE tiles SET status='pending', attempts=0, last_error=NULL "
                "WHERE status='failed'").rowcount
            # Also re-queue 'done' tiles whose cached file has gone missing (cache
            # cleared/moved, or a partial cleanup); otherwise the run would finish
            # with nothing to mosaic and fail with "No tiles downloaded". A NULL
            # file_path is an intentionally empty tile (a legitimate 404/204 gap),
            # NOT a lost file — leave those alone so a resume doesn't waste a
            # request (and a quota tile) re-confirming every known gap.
            missing = [(tid,) for tid, fp in self._c.execute(
                           "SELECT id, file_path FROM tiles WHERE status='done'")
                       if fp and not os.path.exists(self._resolve(fp))]
            if missing:
                self._c.executemany(
                    "UPDATE tiles SET status='pending', attempts=0, last_error=NULL "
                    "WHERE id=?", missing)
            if requeued or missing:
                self.logger.info(
                    "Resuming queue; re-queued %d failed and %d missing tile(s) for retry.",
                    requeued, len(missing))
            else:
                self.logger.info("Resuming existing queue (fingerprint=%s).", stored_fp)
            return
        self._c.execute("BEGIN")
        self._c.executemany(
            "INSERT INTO tiles (id,spec) VALUES (?,?)",
            [(t["id"], json.dumps(t)) for t in tiles])
        for k, v in meta.items():
            self._c.execute("INSERT INTO job_meta VALUES (?,?)", (k, json.dumps(v)))
        self._c.execute("COMMIT")
        self.logger.info("Queued %d tiles (fingerprint=%s).", len(tiles), current_fp)

    def pending_tiles(self):
        return self._c.execute(
            "SELECT id,spec,attempts FROM tiles WHERE status='pending' ORDER BY id"
        ).fetchall()

    def total(self):
        return self._c.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]

    def counts(self):
        r = {"pending": 0, "done": 0, "failed": 0}
        for s, n in self._c.execute("SELECT status,COUNT(*) FROM tiles GROUP BY status"):
            r[s] = n
        return r

    def mark_attempt(self, tid, attempts):
        self._c.execute("UPDATE tiles SET attempts=?,updated_at=? WHERE id=?",
                        (attempts, datetime.now(timezone.utc).isoformat(), tid))

    def mark_done(self, tid, path):
        # Store the path relative to work_dir (forward slashes) so the cache can
        # be moved/restored elsewhere and still resume; None for an empty tile.
        rel = os.path.relpath(path, self.work_dir).replace(os.sep, "/") if path else None
        self._c.execute(
            "UPDATE tiles SET status='done',file_path=?,last_error=NULL,updated_at=? WHERE id=?",
            (rel, datetime.now(timezone.utc).isoformat(), tid))

    def mark_failed(self, tid, err):
        self._c.execute("UPDATE tiles SET status='failed',last_error=?,updated_at=? WHERE id=?",
                        (str(err)[:2000], datetime.now(timezone.utc).isoformat(), tid))

    def done_file_paths(self):
        out = []
        for (p,) in self._c.execute(
                "SELECT file_path FROM tiles WHERE status='done' AND file_path IS NOT NULL"):
            ap = self._resolve(p)
            if ap and os.path.exists(ap):
                out.append(ap)
        return out

    def close(self):
        try: self._c.close()
        except Exception: pass  # nosec B110


# ─────────────────────────────────────────────
# FINGERPRINT
# ─────────────────────────────────────────────
def fingerprint(source, params, opts, extent_wkt, extent_crs):
    h = hashlib.sha256()
    h.update(source.SOURCE_NAME.encode())
    for part in source.fingerprint_parts(params, opts):
        h.update(str(part).encode())
    h.update((extent_crs or "").encode())
    h.update((extent_wkt or "").encode())
    return h.hexdigest()[:16]


def cache_key_for(output_path, temporary, fp):
    """Per-job cache-subdir name. Uses the sanitised output filename (so re-runs
    to the same file resume, and a different output doesn't clobber it); falls
    back to the job fingerprint `fp` for a temporary/unnamed output."""
    if output_path and not temporary:
        base = os.path.splitext(os.path.basename(output_path))[0]
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
        if safe:
            return safe
    return fp


def job_base_dir():
    """Where the `__btdcache__` folder lives: next to the saved project, else the
    QGIS settings dir."""
    project = QgsProject.instance()
    return (os.path.dirname(project.fileName())
            if project.fileName() else QgsApplication.qgisSettingsDirPath())


# A shared tile whose fetch returned "no tile here" (404/204) is recorded with a
# zero-byte sentinel, so an overlapping AOI doesn't re-request the known gap.
SHARED_EMPTY_SUFFIX = ".empty"


def shared_source_dir(work_dir, source, params, opts):
    """Directory of tiles reusable across jobs from the same source, beside the
    per-job dirs (…/__btdcache__/shared/<signature>/). Its tiles are keyed by a
    global identity ({z}/{x}/{y} for XYZ, matrix/col/row for WMTS, globally-
    snapped col/row for WMS), so overlapping AOIs reuse each other's tiles.
    Returns None for a source with no global tile identity (a local raster isn't
    fetched) — that keeps its per-job tiles. The signature takes `opts` because
    for WMS the resolution/tile size define the grid itself (unlike the zoom /
    matrix that XYZ / WMTS already carry in each tile's identity)."""
    if not getattr(source, "SHAREABLE", False):
        return None
    try:
        sig = hashlib.sha256(
            source.shared_signature(params, opts).encode()).hexdigest()[:16]
    except Exception:
        return None
    return os.path.join(os.path.dirname(work_dir), "shared", sig)


def has_resumable_cache(layer, extent, extent_crs, opts, output_path, temporary):
    """True if a work queue already exists for this exact job (same source,
    params, extent and output) with tiles in it — i.e. a run would *resume* it
    rather than start fresh. The dialog uses this to skip the "overwrite output
    file?" prompt when you are just continuing a job. Best-effort: any problem
    returns False, so the prompt still protects a genuinely new export."""
    try:
        source = source_for(layer)
        if source is None or extent is None or extent.isEmpty():
            return False
        params = source.extract_params(layer)
        extent_wkt = QgsGeometry.fromRect(extent).asWkt()
        fp = fingerprint(source, params, opts or {}, extent_wkt,
                         extent_crs or QgsProject.instance().crs().authid())
        ckey = cache_key_for(output_path, temporary, fp)
        db = os.path.join(job_base_dir(), WORK_SUBDIR_NAME, ckey, "tiles.sqlite")
        if not os.path.isfile(db):
            return False
        con = sqlite3.connect(db)
        try:
            row = con.execute(
                "SELECT value FROM job_meta WHERE key='fingerprint'").fetchone()
            stored = json.loads(row[0]) if row else None
            n = con.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        finally:
            con.close()
        # Same job (fingerprint matches) with a populated queue → a resume.
        return stored == fp and n > 0
    except Exception:
        return False


def migrate_flat_cache(work_dir, fp, logger):
    """One-time migration of a pre-1.4.18 *flat* cache. Old versions kept a single
    cache directly in <base>/__btdcache__/; now each job has its own subdir. If a
    flat cache exists and belongs to *this* job (its stored fingerprint matches
    `fp`), move it into `work_dir` so an interrupted job still resumes after the
    upgrade. A flat cache from a different job is left untouched. Returns True if
    a migration happened."""
    parent = os.path.dirname(work_dir)                  # the __btdcache__ root
    flat_db = os.path.join(parent, "tiles.sqlite")
    if not os.path.isfile(flat_db):
        return False
    if os.path.isfile(os.path.join(work_dir, "tiles.sqlite")):
        return False                                    # job already has its cache
    try:
        con = sqlite3.connect(flat_db)
        row = con.execute(
            "SELECT value FROM job_meta WHERE key='fingerprint'").fetchone()
        con.close()
        stored = json.loads(row[0]) if row else None
    except Exception:
        stored = None
    if stored != fp:
        return False                                    # different job — leave it

    import shutil
    logger.info("Migrating a pre-1.4.18 cache into this job's folder (%s).", work_dir)
    for name in ("tiles.sqlite", "tiles.sqlite-wal", "tiles.sqlite-shm",
                 "tiles", "mosaic.vrt", "mosaic.tif", "cutline.gpkg"):
        src = os.path.join(parent, name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(work_dir, name)
        try:
            if os.path.exists(dst):     # replace the empty dir/file created at init
                if os.path.isdir(dst):
                    shutil.rmtree(dst, ignore_errors=True)
                else:
                    os.remove(dst)
            shutil.move(src, dst)
        except OSError as e:
            logger.warning("Could not migrate %s: %s", name, e)

    # The moved DB still records tile paths at the OLD flat location; repoint them
    # (relative to the new work_dir) so already-downloaded tiles are reused, not
    # re-fetched — and so the migrated cache is itself relocatable afterwards.
    try:
        con = sqlite3.connect(os.path.join(work_dir, "tiles.sqlite"))
        rows = con.execute(
            "SELECT id, file_path FROM tiles WHERE file_path IS NOT NULL").fetchall()
        for tid, fpth in rows:
            if fpth:
                con.execute("UPDATE tiles SET file_path=? WHERE id=?",
                            ("tiles/" + os.path.basename(fpth), tid))
        con.commit()
        con.close()
    except Exception as e:
        logger.warning("Cache migrated, but repointing tile paths failed (%s); "
                       "some tiles may be re-downloaded.", e)
    return True


# ─────────────────────────────────────────────
# MOSAIC
# ─────────────────────────────────────────────
def build_mosaic(tile_paths, work_dir, logger, tif_path, native_crs, out_crs,
                 resample="bilinear", cutline=None, add_alpha=True, nodata=None):
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
    # RGB tiles get an alpha band for transparency; single-band data (e.g. a DTM)
    # instead carries its nodata value, so QGIS doesn't stretch over fill pixels.
    vrt_opts = dict(resampleAlg="nearest")
    if add_alpha:
        vrt_opts["addAlpha"] = True
    elif nodata is not None:
        vrt_opts["srcNodata"] = nodata
        vrt_opts["VRTNodata"] = nodata
    # gdal.UseExceptions() is on, so a GDAL failure raises rather than returning
    # None; wrap the calls so it surfaces as a clear DownloaderError (the run's
    # generic handler would otherwise report a raw, cryptic GDAL message).
    try:
        ds = gdal.BuildVRT(vrt, tile_paths, options=gdal.BuildVRTOptions(**vrt_opts))
    except Exception as e:
        raise DownloaderError(f"gdal.BuildVRT failed: {e}")
    ds = None

    creation = ["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES",
                "BLOCKXSIZE=256", "BLOCKYSIZE=256", "BIGTIFF=IF_SAFER"]

    reproject = (out_crs and native_crs and out_crs.upper() != native_crs.upper())
    warp_alg  = "near" if resample == "none" else resample
    # A cutline must be applied with Warp, so warp even when not reprojecting.
    try:
        if reproject or cutline:
            warp_kwargs = dict(format="GTiff", dstSRS=out_crs, resampleAlg=warp_alg,
                               creationOptions=creation, multithread=True)
            if not add_alpha and nodata is not None:
                warp_kwargs.update(srcNodata=nodata, dstNodata=nodata)
            if cutline:
                warp_kwargs.update(cutlineDSName=cutline, cropToCutline=True)
            logger.info("Warp VRT → %s (%s → %s, resample=%s%s)",
                        tif, native_crs, out_crs, warp_alg,
                        ", clip=extent" if cutline else "")
            ds = gdal.Warp(tif, vrt, options=gdal.WarpOptions(**warp_kwargs))
        else:
            logger.info("Translate VRT → %s (%s)", tif, native_crs)
            ds = gdal.Translate(tif, vrt, options=gdal.TranslateOptions(
                format="GTiff", creationOptions=creation))
    except Exception as e:
        raise DownloaderError(f"Mosaic creation failed: {e}")
    ds.BuildOverviews("AVERAGE", [2, 4, 8, 16])
    ds = None
    return vrt, tif


# ─────────────────────────────────────────────
# QGSTASK
# ─────────────────────────────────────────────
# Strong references to in-flight tasks. QGIS's task manager owns the C++ task,
# but that does NOT keep the Python wrapper alive — without a Python reference the
# subclass (and its overridden run()/finished()) can be garbage-collected before
# it runs, so the task silently does nothing. run() adds the task here; finished()
# removes it.
_ACTIVE_TASKS = set()


class BasemapTileDownloadTask(QgsTask):
    # Emitted (from the worker thread) when the fetch phase ends and the mosaic
    # build begins. Connected to a bound-method slot on this main-thread QObject,
    # so it is delivered as a queued signal on the main thread — letting the UI
    # flash a message while the progress bar already sits at 100%.
    mosaicStarted = pyqtSignal()
    # Emitted (worker thread, throttled) after each tile resolves, carrying
    # (resolved, total) so the UI can show a live per-tile counter. Queued to the
    # main thread like mosaicStarted.
    tileProgress = pyqtSignal(int, int)

    def __init__(self, source, layer, extent_wkt, extent_crs, params, opts,
                 native_crs, out_crs, output_path=None, resample="bilinear",
                 clip=False, concurrency=CONCURRENCY,
                 max_attempts=MAX_ATTEMPTS_PER_TILE, min_delay=0.0,
                 backoff_cap=MAX_DELAY_SEC, giveup_after=MAX_CONSECUTIVE_BACKPRESSURE,
                 cache_key=None, on_mosaic_start=None, on_finished=None,
                 on_tile_progress=None):
        # Silent: suppress QGIS's own task-finished/terminated notifications — the
        # plugin posts its own completion (and error) messages, so QGIS's generic
        # one is just noise, especially after a failure. (Silent needs QGIS 3.26+;
        # guarded in case it's absent.)
        flags = QgsTask.CanCancel
        silent = getattr(QgsTask, "Silent", None)
        if silent is not None:
            flags |= silent
        # User-facing wording: a local raster is "read/exported", remote tiles are
        # "downloaded" (internal names stay "fetch"/"tile"). This also names the
        # QGIS Task Manager entry.
        local = getattr(source, "LOCAL", False)
        super().__init__(TASK_DESC_LOCAL if local else TASK_DESC, flags)
        self._local   = local
        self._t_label = "raster export" if local else "tile download"
        self._t_ing   = "Reading" if local else "Fetching"
        self._t_past  = "read" if local else "downloaded"
        self._source       = source
        self._params       = params
        self._opts         = opts
        self._extent_wkt      = extent_wkt      # extent as a rectangle polygon (WKT)
        self._extent_crs      = extent_crs      # CRS authid of the extent
        self._native_crs   = native_crs
        self._out_crs      = out_crs or native_crs
        self._output_path  = output_path or None
        self._resample     = resample or "bilinear"
        self._clip         = bool(clip)
        self._concurrency  = max(1, int(concurrency))
        self._max_attempts = max(1, int(max_attempts))
        self._max_backpressure = MAX_BACKPRESSURE_RETRIES
        self._min_delay    = max(0.0, float(min_delay or 0.0))
        # Advanced (per-run) tuning: adaptive back-off ceiling, and the run-level
        # circuit-breaker threshold (0 = never give up, only the per-tile limit).
        self._backoff_cap  = max(self._min_delay, float(backoff_cap or MAX_DELAY_SEC))
        self._giveup_after = max(0, int(giveup_after))

        # Each job gets its own cache subdir (keyed by the output name, or the
        # job fingerprint for a temporary output), so downloading a different
        # source/extent no longer wipes an in-progress (e.g. rate-limited) job.
        self.work_dir = os.path.join(job_base_dir(), WORK_SUBDIR_NAME, cache_key or "default")
        os.makedirs(os.path.join(self.work_dir, "tiles"), exist_ok=True)

        self.result_tif_path = None
        self.exception       = None
        self.summary         = None      # {total, done, failed} once tiles resolve
        self.was_cancelled   = False     # set if the run was cancelled mid-way
        self.server_gave_up  = False     # set if the circuit breaker tripped (server down)
        self.logger          = build_logger(self.work_dir)

        # Connect on the main thread (where the task is built) to a bound method,
        # so emitting from the worker thread is auto-delivered as a queued signal.
        self._on_mosaic_start   = on_mosaic_start
        self._on_finished       = on_finished
        self._on_tile_progress  = on_tile_progress
        self.mosaicStarted.connect(self._handle_mosaic_started)
        self.tileProgress.connect(self._handle_tile_progress)

    def _handle_mosaic_started(self):
        # Runs on the main thread; best-effort UI callback (a message-bar flash).
        # Never let a UI hiccup disturb the run.
        if callable(self._on_mosaic_start):
            try:
                self._on_mosaic_start()
            except Exception:  # nosec B110
                pass

    def _handle_tile_progress(self, done, total):
        # Runs on the main thread; best-effort live per-tile counter update.
        if callable(self._on_tile_progress):
            try:
                self._on_tile_progress(done, total)
            except Exception:  # nosec B110
                pass

    def run(self):
        try:
            self._run_impl()
            return True
        except DownloaderError as e:
            self.exception = e; self.logger.error("Fatal: %s", e); return False
        except Exception as e:
            self.exception = e
            self.logger.error("Unexpected: %s\n%s", e, traceback.format_exc())
            return False

    def finished(self, result):
        """Main-thread completion hook. QGIS calls this after run() returns —
        for success, failure, or cancellation alike — so doing the UI work here
        (rather than from the manager's taskCompleted/taskTerminated signals)
        makes the completion message appear immediately, including after a cancel.
        `result` is what run() returned (True even on cancel: a partial mosaic is
        still built)."""
        _ACTIVE_TASKS.discard(self)     # release the strong ref kept in run()
        release_logger()
        loaded = False
        tif = self.result_tif_path
        # Load the mosaic whenever one was produced — including a partial mosaic
        # from a cancelled run — so the user can see the gaps.
        if tif and os.path.exists(tif):
            layer_name = os.path.splitext(os.path.basename(tif))[0].replace("_", " ")
            lyr = QgsRasterLayer(tif, layer_name)
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
                loaded = True
                print(f"[Basemap Tile Downloader] Mosaic loaded: {tif}")
            else:
                msg = f"Mosaic file invalid: {tif}"
                print(f"[Basemap Tile Downloader] WARNING: {msg}")
                QgsMessageLog.logMessage(msg, LOG_TAB, Qgis.Critical)
        elif not result and not self.was_cancelled:
            msg = str(self.exception) if self.exception else "Task failed."
            print(f"[Basemap Tile Downloader] FAILED: {msg}")
            QgsMessageLog.logMessage(f"Task failed: {msg}", LOG_TAB, Qgis.Critical)

        if callable(self._on_finished):
            try:
                self._on_finished({
                    "success":   bool(result),
                    "loaded":    loaded,
                    "cancelled": bool(self.was_cancelled),
                    "server_gave_up": bool(self.server_gave_up),
                    "local":     bool(getattr(self, "_local", False)),
                    "tif":       tif,
                    "summary":   self.summary or {},
                    "error":     (str(self.exception)
                                  if (not result and self.exception) else None),
                })
            except Exception:  # nosec B110
                pass

    def _run_impl(self):
        logger = self.logger
        ver = _plugin_version()
        logger.info("=== Basemap %s%s (%s) starting ===",
                    self._t_label, f" v{ver}" if ver else "", self._source.SOURCE_NAME)
        if gdal is None:
            raise DownloaderError("GDAL bindings unavailable; cannot run.")

        extent_geom = QgsGeometry.fromWkt(self._extent_wkt or "")
        if extent_geom.isNull() or extent_geom.isEmpty():
            raise DownloaderError("No valid extent to download.")

        prepare = getattr(self._source, "prepare", None)
        if callable(prepare):
            prepare(self._params, self._opts, logger)
            # A backend may resolve its native CRS during prepare (e.g. WMTS
            # reads it from the capabilities), so refresh it now.
            self._native_crs = self._source.native_crs(self._params, self._opts)

        tiles = self._source.build_tile_grid(
            extent_geom, self._extent_crs, self._params, self._opts, logger)
        fp = fingerprint(self._source, self._params, self._opts,
                         self._extent_wkt, self._extent_crs)
        logger.info("Job fingerprint: %s", fp)

        db_path = os.path.join(self.work_dir, "tiles.sqlite")
        migrate_flat_cache(self.work_dir, fp, logger)   # one-time pre-1.4.18 move
        queue   = TileQueue(db_path, logger)
        try:
            queue.populate_if_empty(tiles, {
                "source": self._source.SOURCE_NAME, "native_crs": self._native_crs,
                "out_crs": self._out_crs, "fingerprint": fp,
            }, work_dir=self.work_dir)

            total = queue.total()
            logger.info("Queue: %s  (total=%d)", queue.counts(), total)

            initial_delay = getattr(self._source, "INITIAL_DELAY_SEC", INITIAL_DELAY_SEC)
            throttle = AdaptiveThrottle(logger, initial_delay, self._min_delay,
                                        max_delay=self._backoff_cap)
            if self._min_delay:
                logger.info("Minimum delay between requests: %.2fs", self._min_delay)
            self._sleep(initial_delay)

            tiles_dir = os.path.join(self.work_dir, "tiles")
            # In-memory work queue seeded from the DB; retries are re-appended.
            # All DB and throttle state is touched only here (this thread); the
            # pool workers just fetch + georeference and return a result.
            # Each work item is [tid, tile, attempts, backpressure]: `attempts`
            # counts genuine errors (persisted in the DB, survives a resume);
            # `backpressure` counts throttle/timeout retries (in-memory, per run).
            pending = deque([tid, json.loads(spec), attempts, 0]
                            for tid, spec, attempts in queue.pending_tiles())
            in_flight = {}          # future -> [tid, tile, attempts, backpressure]
            processed = 0
            # Track resolved counts in memory (seeded from any resumed run's
            # already-'done' tiles) instead of re-querying SQLite per tile.
            start = queue.counts()
            done_count   = start.get("done", 0)
            failed_count = start.get("failed", 0)

            # Cross-job shared cache: XYZ/WMTS tiles have a global identity, so a
            # tile another (overlapping) AOI already fetched can be reused here —
            # no request, no provider-quota hit. Freshly fetched tiles are
            # published to this dir below; a known-empty tile leaves a sentinel so
            # it isn't re-requested. WMS/local sources return None (kept per-job).
            shared_dir = shared_source_dir(self.work_dir, self._source,
                                           self._params, self._opts)
            if shared_dir and pending:
                reused, kept = 0, deque()
                for item in pending:
                    tid, tile = item[0], item[1]
                    rel = self._source.shared_rel_path(tile)
                    if rel is None:            # legacy tile: fetch it per-job
                        kept.append(item)
                        continue
                    sp = os.path.join(shared_dir, rel)
                    if os.path.exists(sp):
                        queue.mark_done(tid, sp); reused += 1
                    elif os.path.exists(sp + SHARED_EMPTY_SUFFIX):
                        queue.mark_done(tid, None); reused += 1
                    else:
                        kept.append(item)
                if reused:
                    pending = kept
                    done_count += reused
                    logger.info("Reused %d tile(s) from the shared cache; "
                                "%d left to fetch.", reused, len(pending))

            # Collapse repeated error messages: log a given error in full the
            # first time, then just a one-line "(repeat ×N)" — a broken provider
            # can otherwise spam thousands of identical multi-line exceptions.
            err_seen = {}
            # Circuit breaker: back-pressure failures in a row with no success.
            # Reset by any successful tile; trips at MAX_CONSECUTIVE_BACKPRESSURE.
            consecutive_bp = 0
            # Throttle for the live per-tile counter emitted to the UI.
            last_prog_emit = 0.0
            logger.info("%s with concurrency=%d (back-off cap %.0fs, give up "
                        "after %s consecutive failures)", self._t_ing, self._concurrency,
                        self._backoff_cap,
                        self._giveup_after if self._giveup_after else "never")

            with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
                while pending or in_flight:
                    # Fill the pool (pacing each dispatch with the throttle), but
                    # stop dispatching new tiles once cancelled / circuit-broken.
                    # Already-running fetches are still drained below — the pool
                    # waits for them on shutdown anyway, so we record their results
                    # instead of discarding and re-fetching them next run.
                    while pending and len(in_flight) < self._concurrency \
                            and not self.isCanceled() and not self.server_gave_up:
                        throttle.wait(self.isCanceled)
                        if self.isCanceled():
                            break
                        tid, tile, attempts, backpressure = pending.popleft()
                        out_path = os.path.join(tiles_dir, f"tile_{tid:06d}.tif")
                        fut = pool.submit(self._fetch_worker, tile, out_path)
                        in_flight[fut] = [tid, tile, attempts, backpressure]

                    if not in_flight:
                        break

                    done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                    for fut in done:
                        tid, tile, attempts, backpressure = in_flight.pop(fut)
                        outcome, path, retry_after, err = fut.result()

                        if outcome in ("ok", "empty"):
                            throttle.on_success()
                            store_path = path
                            rel = (self._source.shared_rel_path(tile)
                                   if shared_dir is not None else None)
                            if rel is not None:
                                # Publish to the shared cache so overlapping AOIs
                                # reuse this tile. Move (atomic on the same
                                # filesystem) rather than copy; a known-empty tile
                                # leaves a zero-byte sentinel. (rel is None for a
                                # legacy tile without col/row — kept per-job.)
                                sp = os.path.join(shared_dir, rel)
                                try:
                                    os.makedirs(os.path.dirname(sp), exist_ok=True)
                                    if outcome == "ok" and path:
                                        os.replace(path, sp)
                                        store_path = sp
                                    elif outcome == "empty":
                                        open(sp + SHARED_EMPTY_SUFFIX, "a").close()
                                except OSError:
                                    store_path = path   # fall back to per-job file
                            queue.mark_done(tid, store_path)
                            done_count += 1
                            consecutive_bp = 0     # a success resets the circuit breaker
                            # DEBUG (file log only): one line per tile would flood
                            # the QGIS Message Log panel; the periodic Checkpoint
                            # line below is the panel's progress indicator.
                            logger.debug("Tile %d OK%s", tid,
                                         "" if outcome == "ok" else " (empty/missing)")
                        elif outcome in ("throttle", "timeout", "server_error"):
                            # Back-pressure: slow the whole run down and retry the
                            # tile without spending its error budget — the tile is
                            # fine, the server is rate-limiting us (throttle/
                            # timeout) or transiently failing to serve it
                            # (server_error, e.g. a WMS ServiceException). The
                            # global throttle self-regulates: it backs off on these
                            # and speeds up again as tiles succeed.
                            if outcome == "timeout":
                                throttle.on_timeout()
                            elif outcome == "throttle":
                                throttle.on_throttle(retry_after, reason="rate-limit")
                            else:                       # server_error
                                throttle.on_throttle(reason="server error")
                            backpressure += 1
                            consecutive_bp += 1     # feeds the run-level circuit breaker
                            ra = f", server asked {retry_after:.0f}s" if retry_after else ""
                            seen = err_seen.get(err, 0) + 1
                            err_seen[err] = seen
                            if backpressure <= self._max_backpressure:
                                pending.append([tid, tile, attempts, backpressure])
                                if seen == 1:
                                    logger.info(
                                        "Tile %d back-pressure (%s: %s%s) — backing "
                                        "off, now pacing at %s; requeued (retry %d/%d).",
                                        tid, outcome, err or "-", ra, throttle.status(),
                                        backpressure, self._max_backpressure)
                                else:
                                    logger.info(
                                        "Tile %d back-pressure (%s, repeat ×%d) — "
                                        "pacing at %s; requeued (retry %d/%d).",
                                        tid, outcome, seen, throttle.status(),
                                        backpressure, self._max_backpressure)
                            else:
                                logger.error(
                                    "Tile %d gave up after %d %s retries: %s",
                                    tid, backpressure, outcome, _first_line(err))
                                queue.mark_failed(tid, err)
                                failed_count += 1
                        else:
                            attempts += 1
                            queue.mark_attempt(tid, attempts)
                            seen = err_seen.get(err, 0) + 1
                            err_seen[err] = seen
                            if seen == 1:
                                logger.warning("Tile %d attempt %d: %s", tid, attempts, err)
                            else:
                                logger.warning("Tile %d attempt %d: %s (repeat ×%d)",
                                               tid, attempts, _first_line(err), seen)
                            if attempts < self._max_attempts:
                                pending.append([tid, tile, attempts, backpressure])
                            else:
                                logger.error("Tile %d failed permanently: %s",
                                             tid, _first_line(err))
                                queue.mark_failed(tid, err)
                                failed_count += 1

                        processed += 1
                        done_n = done_count + failed_count
                        self.setProgress(100.0 * done_n / total if total else 100.0)
                        # Live per-tile counter in the message bar. Throttled so a
                        # fast source can't flood the UI event loop; the final tile
                        # always emits so the count lands exactly on the total.
                        now = time.monotonic()
                        if done_n >= total or now - last_prog_emit >= 0.15:
                            self.tileProgress.emit(done_n, total)
                            last_prog_emit = now

                        # Circuit breaker: server has refused every request for a
                        # long stretch — stop grinding and build what we have.
                        # A threshold of 0 disables it (only the per-tile limit).
                        if self._giveup_after and consecutive_bp >= self._giveup_after:
                            self.server_gave_up = True
                            logger.error(
                                "Server persistently failing: %d requests in a row "
                                "with no success (pacing at %s). Giving up on the "
                                "remaining tiles and building a partial mosaic from "
                                "the %d downloaded so far; re-run later to fill the "
                                "gaps.", consecutive_bp, throttle.status(), done_count)

            # The fetch phase is over (drained, cancelled, or circuit-broken).
            # Bump progress to 100% now so the task bar doesn't sit frozen at the
            # last fetch percentage (e.g. 93%) through the mosaic-build step,
            # which reports no progress of its own and can take a while.
            self.setProgress(100.0)

            # Always mosaic what we have — even on cancel — so the gaps show which
            # tiles are missing. Only bail if there is literally nothing to build.
            cancelled = self.isCanceled()
            self.was_cancelled = cancelled

            final_counts = queue.counts()
            n_done   = final_counts.get("done", 0)
            n_failed = final_counts.get("failed", 0)
            n_pending = final_counts.get("pending", 0)
            self.summary = {"total": total, "done": n_done, "failed": n_failed,
                            "cancelled": cancelled,
                            "server_gave_up": self.server_gave_up}
            if cancelled:
                logger.warning(
                    "Cancelled at %d/%d tiles — building a partial mosaic from what "
                    "%s so far (queue checkpointed in %s; re-run to continue).",
                    n_done, total, self._t_past, db_path)
            elif self.server_gave_up:
                logger.warning(
                    "Stopped early (server unavailable): %d of %d tiles downloaded, "
                    "%d still pending. Building a partial mosaic; re-run later to "
                    "fetch the rest (queue checkpointed in %s).",
                    n_done, total, n_pending, db_path)
            elif n_failed:
                logger.warning(
                    "Queue drained: %d of %d tiles %s, %d failed after "
                    "exhausting their retries. The mosaic will have gaps there; "
                    "re-run with the same settings to retry only the failed tiles.",
                    n_done, total, self._t_past, n_failed)
            else:
                logger.info("Queue drained: all %d tiles %s.", total, self._t_past)
            # Tally of distinct errors seen this run (most frequent first) so a
            # provider outage is one readable summary, not thousands of lines.
            for msg, n in sorted(err_seen.items(), key=lambda kv: -kv[1]):
                logger.info("Error seen %d×: %s", n, _first_line(msg))

            tile_paths = queue.done_file_paths()
            if not tile_paths:
                if cancelled:
                    logger.warning("Cancelled before any tile %s; nothing to mosaic.",
                                   self._t_past)
                    return
                raise DownloaderError(f"No tiles {self._t_past}; cannot build mosaic.")

            # Fetch phase is done and there are tiles to mosaic; the progress bar
            # is already pinned at 100%. Tell the UI the (progress-less) mosaic
            # build is starting so it can reassure the user it's not stuck.
            self.mosaicStarted.emit()

            cutline = self._build_cutline(extent_geom, logger) if self._clip else None
            # A source may ask to preserve a nodata value (single-band) instead of
            # adding an alpha band (RGB); default is add-alpha, as before.
            hints = {}
            get_hints = getattr(self._source, "mosaic_hints", None)
            if callable(get_hints):
                hints = get_hints(self._params, self._opts) or {}
            vrt_path, tif_path = build_mosaic(
                tile_paths, self.work_dir, logger, self._output_path,
                self._native_crs, self._out_crs, self._resample, cutline,
                add_alpha=hints.get("add_alpha", True), nodata=hints.get("nodata"))
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
            release_logger()

    def _build_cutline(self, extent_geom, logger):
        """Write the extent polygon (reprojected to the output CRS) to a
        GeoPackage for use as a gdal.Warp cutline. Returns the path, or None."""
        try:
            from osgeo import ogr, osr
            target = QgsCoordinateReferenceSystem(self._out_crs)
            src    = QgsCoordinateReferenceSystem(self._extent_crs)
            ctx    = QgsProject.instance().transformContext()
            g = QgsGeometry(extent_geom)
            if src != target and g.transform(QgsCoordinateTransform(src, target, ctx)) != 0:
                return None
            wkt = g.asWkt()

            path = os.path.join(self.work_dir, "cutline.gpkg")
            if os.path.exists(path):
                try: os.remove(path)
                except OSError: pass
            srs = osr.SpatialReference(); srs.SetFromUserInput(self._out_crs)
            ds  = ogr.GetDriverByName("GPKG").CreateDataSource(path)
            lyr = ds.CreateLayer("cutline", srs, ogr.wkbMultiPolygon)
            geom = ogr.CreateGeometryFromWkt(wkt)
            if geom.GetGeometryName() == "POLYGON":
                geom = ogr.ForceToMultiPolygon(geom)
            f = ogr.Feature(lyr.GetLayerDefn()); f.SetGeometry(geom)
            lyr.CreateFeature(f)
            f = lyr = ds = None
            logger.info("Cutline written → %s", path)
            return path
        except Exception as e:
            logger.warning("Could not build cutline; skipping clip: %s", e)
            return None

    def _fetch_worker(self, tile, out_path):
        """Runs in a pool thread: one fetch attempt, no DB/throttle access.
        Returns (outcome, path, retry_after, error) where outcome is one of
        'ok' | 'empty' | 'throttle' | 'timeout' | 'server_error' | 'error'."""
        try:
            path = self._source.fetch_one_tile(
                self._params, self._opts, tile, out_path, self.logger)
            return ("ok" if path else "empty", path, None, None)
        except TileFetchError as e:
            msg = str(e)
            if "timed out" in msg.lower():
                return ("timeout", None, None, msg)
            if e.is_throttle:
                return ("throttle", None, e.retry_after, msg)
            if e.is_server_error:
                return ("server_error", None, None, msg)
            return ("error", None, None, msg)
        except Exception as e:                      # unexpected → treat as error
            return ("error", None, None, str(e))

    def _sleep(self, seconds):
        rem = float(seconds)
        while rem > 0:
            if self.isCanceled():
                return
            time.sleep(min(0.1, rem)); rem -= 0.1


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def run(layer=None, extent=None, extent_crs=None, opts=None, out_crs=None,
        output_path=None, temporary=False, resample="bilinear", clip=False,
        concurrency=None, max_attempts=None, min_delay=None,
        backoff_cap=None, giveup_after=None,
        on_finished=None, on_mosaic_start=None, on_tile_progress=None):
    """
    Start a download task. The source backend (WMS / XYZ) is auto-detected from
    `layer`. `opts` is the source-specific settings dict
    (WMS: {tile_pixels, resolution}; XYZ: {zoom}).

    `on_finished`, if given, is called on the main thread when the task ends
    with a result dict: {success, loaded, tif, summary, error}. The plugin uses
    it to post a completion message to the QGIS message bar.

    `on_mosaic_start`, if given, is called (no args) on the main thread when the
    fetch phase ends and the mosaic build begins — the progress bar is at 100%
    by then, so the plugin uses it to flash a "building mosaic" message.

    Each job caches under its own subdir (keyed by output name), so a different
    job no longer wipes an in-progress one; an interrupted run resumes on re-run.
    """
    for t in QgsApplication.taskManager().activeTasks():
        if t.description() in TASK_DESCS:
            msg = ("A Basemap Tile Downloader task is already running; not starting "
                   "another. Cancel it in the Task Manager first to restart.")
            print(f"[Basemap Tile Downloader] {msg}")
            QgsMessageLog.logMessage(msg, LOG_TAB, Qgis.Warning)
            return t

    source = source_for(layer)
    if source is None:
        msg = "Selected layer is not a recognised WMS/WMTS/XYZ or local raster (GeoTIFF) layer."
        QgsMessageLog.logMessage(msg, LOG_TAB, Qgis.Critical)
        print(f"[Basemap Tile Downloader] ERROR: {msg}")
        return None

    if extent is None or extent.isEmpty():
        msg = "No extent to download."
        QgsMessageLog.logMessage(msg, LOG_TAB, Qgis.Critical)
        print(f"[Basemap Tile Downloader] ERROR: {msg}")
        return None
    extent_crs = extent_crs or QgsProject.instance().crs().authid()
    extent_wkt = QgsGeometry.fromRect(extent).asWkt()

    try:
        params = source.extract_params(layer)
    except DownloaderError as e:
        QgsMessageLog.logMessage(str(e), LOG_TAB, Qgis.Critical)
        print(f"[Basemap Tile Downloader] ERROR: {e}")
        return None

    opts = opts or {}
    native = source.native_crs(params, opts)
    out_crs = out_crs or source.default_out_crs(params)
    resample = resample or "bilinear"
    if resample == "none":
        out_crs = native            # no reprojection → no resampling

    if temporary:
        from qgis.core import QgsProcessingUtils
        output_path = QgsProcessingUtils.generateTempFilename(
            f"basemap_mosaic_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tif")

    print(f"[Basemap Tile Downloader] Source : {source.SOURCE_NAME}")
    print(f"[Basemap Tile Downloader] Native : {native}   Output CRS: {out_crs}")

    conc     = int(concurrency) if concurrency else getattr(source, "CONCURRENCY", CONCURRENCY)
    attempts = int(max_attempts) if max_attempts else MAX_ATTEMPTS_PER_TILE
    mind     = float(min_delay) if min_delay else 0.0
    cap      = float(backoff_cap) if backoff_cap else MAX_DELAY_SEC
    # giveup_after=0 means "never give up", so distinguish it from None (not set).
    giveup   = MAX_CONSECUTIVE_BACKPRESSURE if giveup_after is None else int(giveup_after)
    # Per-job cache subdir so a different output/extent doesn't wipe this job.
    fp = fingerprint(source, params, opts, extent_wkt, extent_crs)
    ckey = cache_key_for(output_path, temporary, fp)
    task = BasemapTileDownloadTask(source, layer, extent_wkt, extent_crs, params, opts,
                                   native, out_crs, output_path, resample, clip,
                                   conc, attempts, mind, cap, giveup,
                                   cache_key=ckey, on_mosaic_start=on_mosaic_start,
                                   on_finished=on_finished, on_tile_progress=on_tile_progress)
    # Completion is handled by the task's finished() hook (main thread, prompt for
    # success/failure/cancel) rather than the manager's taskCompleted/
    # taskTerminated signals, whose terminated delivery lagged a cancel by an
    # event-loop turn (the message only appeared on the next run). Keep a strong
    # Python reference until finished() runs, or the task is GC'd before it starts.
    _ACTIVE_TASKS.add(task)
    QgsApplication.taskManager().addTask(task)
    print("[Basemap Tile Downloader] Task queued. Watch the Task Manager panel and "
          f"{os.path.join(task.work_dir, 'download.log')}")
    return task

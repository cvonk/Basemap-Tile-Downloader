# -*- coding: utf-8 -*-
"""
engine.py – shared download engine for the AOI Downloader plugin
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
    build_tile_grid(aoi_layer, params, opts, logger) -> list[dict]   # each has "id"
    fetch_one_tile(params, opts, tile, out_path, logger) -> path|None
    fingerprint_parts(params, opts) -> list
"""

import os, json, sqlite3, logging, time, traceback, hashlib
from datetime import datetime

from qgis.PyQt.QtCore   import QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from qgis.core import (
    Qgis, QgsProject, QgsTask, QgsApplication, QgsMessageLog,
    QgsRasterLayer, QgsBlockingNetworkRequest,
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
INITIAL_DELAY_SEC        = 0.5
MIN_DELAY_SEC            = 0.05
MAX_DELAY_SEC            = 60.0
SPEEDUP_FACTOR           = 0.85
SLOWDOWN_FACTOR          = 2.0
SUCCESSES_BEFORE_SPEEDUP = 3
REQUEST_TIMEOUT_MS       = 60_000

CLEANUP_TILES_AFTER_MOSAIC = False
WORK_SUBDIR_NAME = "aoi_download"
LOG_TAB          = "AOI Downloader"
TASK_DESC        = "AOI download"


# ─────────────────────────────────────────────
# EXCEPTIONS
# ─────────────────────────────────────────────
class DownloaderError(Exception):
    """Fatal, non-retryable."""

class TileFetchError(Exception):
    def __init__(self, message, retry_after=None, is_throttle=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.is_throttle = is_throttle


# ─────────────────────────────────────────────
# SOURCE DISPATCH  (late import to avoid a cycle)
# ─────────────────────────────────────────────
def _source_modules():
    from .sources import wms, xyz
    return (wms, xyz)


def source_for(layer):
    """Return the source backend module that handles `layer`, or None."""
    if not isinstance(layer, QgsRasterLayer):
        return None
    for mod in _source_modules():
        try:
            if mod.detect(layer):
                return mod
        except Exception:
            pass
    return None


# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
def build_logger(work_dir):
    logger = logging.getLogger("aoi_downloader")
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
                QgsMessageLog.logMessage(self.format(record), LOG_TAB, lvl)
            except Exception:
                pass

    qh = _QH(); qh.setLevel(logging.INFO)
    qh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(qh)
    logger.info("Log → %s", log_path)
    return logger


def release_logger():
    log = logging.getLogger("aoi_downloader")
    for h in list(log.handlers):
        try: h.flush(); h.close()
        except Exception: pass
        log.removeHandler(h)


# ─────────────────────────────────────────────
# BLOCKING HTTP  (worker-thread safe)
# ─────────────────────────────────────────────
def blocking_get(url, timeout_ms=REQUEST_TIMEOUT_MS):
    req    = QgsBlockingNetworkRequest()
    qt_req = QNetworkRequest(QUrl(url))
    qt_req.setHeader(QNetworkRequest.UserAgentHeader, b"QGIS-AOI-Downloader/1.0")
    err_code = req.get(qt_req, forceRefresh=True)

    reply  = req.reply()
    status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
    body   = bytes(reply.content())
    headers = {}
    for h in reply.rawHeaderList():
        headers[bytes(h).decode("latin1").lower()] = \
            bytes(reply.rawHeader(h)).decode("latin1")

    error_str, timed_out = None, False
    if err_code == QgsBlockingNetworkRequest.NetworkError:
        error_str = reply.errorString()
        if "timeout" in (error_str or "").lower():
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
        return max(0.0, (dt - datetime.utcnow().replace(tzinfo=dt.tzinfo)).total_seconds())
    except Exception:
        return None


# ─────────────────────────────────────────────
# GEOREFERENCE A TILE  (used by every source)
# ─────────────────────────────────────────────
def georeference(body, out_tif, bounds, srs, detect_empty=False):
    """
    Write raw tile bytes to a GeoTIFF stamped with `bounds`
    (ulx, uly, lrx, lry) in CRS `srs`. Returns None on success, "EMPTY_TILE"
    if detect_empty and the alpha band is entirely zero, or an error string.
    """
    if gdal is None:
        raise DownloaderError("GDAL bindings unavailable; cannot georeference tiles.")
    mem = f"/vsimem/aoi_tile_{id(body)}"
    gdal.FileFromMemBuffer(mem, body)
    ds = None
    try:
        ds = gdal.Open(mem)
        if ds is None:
            return "GDAL: cannot open tile (not an image?)."
        if ds.RasterXSize == 0 or ds.RasterCount == 0:
            return "Zero-size raster."
        if detect_empty and ds.RasterCount >= 4:
            alpha = ds.GetRasterBand(4)
            try:
                stats = alpha.ComputeStatistics(True)
            except Exception:
                stats = alpha.GetStatistics(True, True)
            if stats[1] == 0:
                return "EMPTY_TILE"
        ulx, uly, lrx, lry = bounds
        gdal.Translate(out_tif, ds, options=gdal.TranslateOptions(
            format="GTiff", outputSRS=srs, outputBounds=[ulx, uly, lrx, lry]))
        return None
    finally:
        ds = None
        try: gdal.Unlink(mem)
        except Exception: pass


# ─────────────────────────────────────────────
# ADAPTIVE THROTTLE
# ─────────────────────────────────────────────
class AdaptiveThrottle:
    def __init__(self, logger):
        self._d, self._ok, self._log = INITIAL_DELAY_SEC, 0, logger

    def wait(self, cancel_check=None):
        rem = self._d
        while rem > 0:
            if cancel_check and cancel_check():
                return
            time.sleep(min(0.1, rem)); rem -= 0.1

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
        self._c = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        self._c.execute("PRAGMA journal_mode=WAL;")
        self._c.execute(_TILES_DDL)
        self._c.execute(_META_DDL)

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
                        (attempts, datetime.utcnow().isoformat(), tid))

    def mark_done(self, tid, path):
        self._c.execute(
            "UPDATE tiles SET status='done',file_path=?,last_error=NULL,updated_at=? WHERE id=?",
            (path, datetime.utcnow().isoformat(), tid))

    def mark_failed(self, tid, err):
        self._c.execute("UPDATE tiles SET status='failed',last_error=?,updated_at=? WHERE id=?",
                        (str(err)[:2000], datetime.utcnow().isoformat(), tid))

    def done_file_paths(self):
        return [r[0] for r in self._c.execute(
                    "SELECT file_path FROM tiles WHERE status='done' AND file_path IS NOT NULL")
                if r[0] and os.path.exists(r[0])]

    def close(self):
        try: self._c.close()
        except Exception: pass


# ─────────────────────────────────────────────
# FINGERPRINT
# ─────────────────────────────────────────────
def fingerprint(source, params, opts, aoi_layer):
    h = hashlib.sha256()
    h.update(source.SOURCE_NAME.encode())
    for part in source.fingerprint_parts(params, opts):
        h.update(str(part).encode())
    for feat in aoi_layer.getFeatures():
        g = feat.geometry()
        if not g.isNull():
            h.update(g.asWkt(precision=2).encode())
    return h.hexdigest()[:16]


# ─────────────────────────────────────────────
# MOSAIC
# ─────────────────────────────────────────────
def build_mosaic(tile_paths, work_dir, logger, tif_path, native_crs, out_crs):
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

    creation = ["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES",
                "BLOCKXSIZE=256", "BLOCKYSIZE=256", "BIGTIFF=IF_SAFER"]

    reproject = (out_crs and native_crs and out_crs.upper() != native_crs.upper())
    if reproject:
        logger.info("Warp VRT → %s (%s → %s)", tif, native_crs, out_crs)
        ds = gdal.Warp(tif, vrt, options=gdal.WarpOptions(
            format="GTiff", dstSRS=out_crs, resampleAlg="bilinear",
            creationOptions=creation, multithread=True))
    else:
        logger.info("Translate VRT → %s (%s)", tif, native_crs)
        ds = gdal.Translate(tif, vrt, options=gdal.TranslateOptions(
            format="GTiff", creationOptions=creation))
    if ds is None:
        raise DownloaderError("Mosaic creation failed.")
    ds.BuildOverviews("AVERAGE", [2, 4, 8, 16])
    ds = None
    return vrt, tif


# ─────────────────────────────────────────────
# QGSTASK
# ─────────────────────────────────────────────
class AoiDownloadTask(QgsTask):
    def __init__(self, source, layer, aoi_layer, params, opts,
                 native_crs, out_crs, output_path=None):
        super().__init__(TASK_DESC, QgsTask.CanCancel)
        self._source       = source
        self._params       = params
        self._opts         = opts
        self._aoi_layer_id = aoi_layer.id()
        self._native_crs   = native_crs
        self._out_crs      = out_crs or native_crs
        self._output_path  = output_path or None

        project = QgsProject.instance()
        base_dir = (os.path.dirname(project.fileName())
                    if project.fileName() else QgsApplication.qgisSettingsDirPath())
        self.work_dir = os.path.join(base_dir, WORK_SUBDIR_NAME)
        os.makedirs(os.path.join(self.work_dir, "tiles"), exist_ok=True)

        self.result_tif_path = None
        self.exception       = None
        self.logger          = build_logger(self.work_dir)

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

    def _run_impl(self):
        logger = self.logger
        logger.info("=== AOI Download (%s) starting ===", self._source.SOURCE_NAME)
        if gdal is None:
            raise DownloaderError("GDAL bindings unavailable; cannot run.")

        aoi_layer = QgsProject.instance().mapLayer(self._aoi_layer_id)
        if aoi_layer is None:
            raise DownloaderError("AOI layer was removed from the project.")

        prepare = getattr(self._source, "prepare", None)
        if callable(prepare):
            prepare(self._params, self._opts, logger)

        tiles = self._source.build_tile_grid(aoi_layer, self._params, self._opts, logger)
        fp = fingerprint(self._source, self._params, self._opts, aoi_layer)
        logger.info("Job fingerprint: %s", fp)

        db_path = os.path.join(self.work_dir, "tiles.sqlite")
        queue   = TileQueue(db_path, logger)
        try:
            queue.populate_if_empty(tiles, {
                "source": self._source.SOURCE_NAME, "native_crs": self._native_crs,
                "out_crs": self._out_crs, "fingerprint": fp,
            }, work_dir=self.work_dir)

            total = queue.total()
            logger.info("Queue: %s  (total=%d)", queue.counts(), total)

            throttle = AdaptiveThrottle(logger)
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

                tid, spec, attempts = pending[0]
                tile = json.loads(spec)
                out_path = os.path.join(tiles_dir, f"tile_{tid:06d}.tif")
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
                        result = self._source.fetch_one_tile(
                            self._params, self._opts, tile, out_path, logger)
                        throttle.on_success()
                        queue.mark_done(tid, result)
                        success = True
                        logger.info("Tile %d OK%s", tid, "" if result else " (empty/missing)")
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
                                        tid, backoff, attempts + 1, MAX_ATTEMPTS_PER_TILE)
                            self._sleep(backoff)

                if not success:
                    logger.error("Tile %d failed permanently: %s", tid, last_err)
                    queue.mark_failed(tid, last_err)

                processed += 1
                c = queue.counts()
                done_n = c["done"] + c["failed"]
                self.setProgress(100.0 * done_n / total if total else 100.0)
                if processed % 25 == 0:
                    logger.info("Checkpoint %d/%d (%s)", done_n, total, c)

            logger.info("All tiles resolved: %s", queue.counts())
            tile_paths = queue.done_file_paths()
            if not tile_paths:
                raise DownloaderError("No tiles downloaded; cannot build mosaic.")

            vrt_path, tif_path = build_mosaic(
                tile_paths, self.work_dir, logger, self._output_path,
                self._native_crs, self._out_crs)
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

    def _sleep(self, seconds):
        rem = float(seconds)
        while rem > 0:
            if self.isCanceled():
                return
            time.sleep(min(0.1, rem)); rem -= 0.1


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def run(layer=None, aoi_layer=None, opts=None, out_crs=None,
        output_path=None, temporary=False):
    """
    Start a download task. The source backend (WMS / XYZ) is auto-detected from
    `layer`. `opts` is the source-specific settings dict
    (WMS: {tile_pixels, resolution}; XYZ: {zoom}).
    """
    for t in QgsApplication.taskManager().activeTasks():
        if t.description() == TASK_DESC:
            msg = ("An 'AOI download' task is already running; not starting "
                   "another. Cancel it in the Task Manager first to restart.")
            print(f"[AOI Downloader] {msg}")
            QgsMessageLog.logMessage(msg, LOG_TAB, Qgis.Warning)
            return t

    source = source_for(layer)
    if source is None:
        msg = "Selected layer is not a recognised WMS or XYZ tile layer."
        QgsMessageLog.logMessage(msg, LOG_TAB, Qgis.Critical)
        print(f"[AOI Downloader] ERROR: {msg}")
        return None

    try:
        params = source.extract_params(layer)
    except DownloaderError as e:
        QgsMessageLog.logMessage(str(e), LOG_TAB, Qgis.Critical)
        print(f"[AOI Downloader] ERROR: {e}")
        return None

    opts = opts or {}
    native = source.native_crs(params, opts)
    out_crs = out_crs or source.default_out_crs(params)

    if temporary:
        from qgis.core import QgsProcessingUtils
        output_path = QgsProcessingUtils.generateTempFilename(
            f"aoi_mosaic_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tif")

    print(f"[AOI Downloader] Source : {source.SOURCE_NAME}")
    print(f"[AOI Downloader] Native : {native}   Output CRS: {out_crs}")

    task = AoiDownloadTask(source, layer, aoi_layer, params, opts,
                           native, out_crs, output_path)

    def _finished(success):
        release_logger()
        if success and task.result_tif_path and os.path.exists(task.result_tif_path):
            layer_name = os.path.splitext(
                os.path.basename(task.result_tif_path))[0].replace("_", " ")
            lyr = QgsRasterLayer(task.result_tif_path, layer_name)
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
                print(f"[AOI Downloader] Mosaic loaded: {task.result_tif_path}")
            else:
                msg = f"Mosaic file invalid: {task.result_tif_path}"
                print(f"[AOI Downloader] WARNING: {msg}")
                QgsMessageLog.logMessage(msg, LOG_TAB, Qgis.Critical)
        elif not success:
            msg = str(task.exception) if task.exception else "Task failed."
            print(f"[AOI Downloader] FAILED: {msg}")
            QgsMessageLog.logMessage(f"Task failed: {msg}", LOG_TAB, Qgis.Critical)

    task.taskCompleted.connect(lambda: _finished(True))
    task.taskTerminated.connect(lambda: _finished(False))
    QgsApplication.taskManager().addTask(task)
    print("[AOI Downloader] Task queued. Watch the Task Manager panel and "
          f"{os.path.join(task.work_dir, 'download.log')}")
    return task

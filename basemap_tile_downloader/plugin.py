# -*- coding: utf-8 -*-
"""
Basemap Tile Downloader – plugin glue.

Adds a Raster-menu entry (+ toolbar button), shows the source-aware dialog, then
hands off to engine.run() which auto-detects the source backend (WMS / WMTS /
XYZ / local raster).
"""
import os
import time

from qgis.PyQt.QtWidgets import QAction, QDockWidget, QLabel
from qgis.PyQt.QtGui import QIcon
from qgis.core import Qgis, QgsMessageLog

from .dialog import BasemapTileDialog
from . import engine

MENU_TITLE = "Basemap Tile Downloader"


def _format_duration(seconds):
    """Compact human duration for the live ETA: '45s', '3m 20s', '1h 05m'."""
    secs = max(0, int(round(seconds)))
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m {secs:02d}s" if secs else f"{mins}m"
    hrs, mins = divmod(mins, 60)
    return f"{hrs}h {mins:02d}m"


class BasemapTileDownloaderPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self._icon_path = os.path.join(os.path.dirname(__file__), "icon.svg")
        # Live per-tile counter shown in the message bar during a run.
        self._progress_item = None
        self._progress_label = None
        self._progress_verb = "Downloading"
        self._progress_start = 0.0
        self._progress_done0 = 0
        # Persistent "building mosaic…" widget shown in the canvas message bar
        # while the (progress-less) mosaic build runs.
        self._mosaic_item = None

    def initGui(self):
        self.action = QAction(
            QIcon(self._icon_path), "Basemap Tile Downloader…", self.iface.mainWindow())
        self.action.triggered.connect(self.show_dialog)
        self.iface.addToolBarIcon(self.action)
        # Raster menu: it exports a raster, so it lives with the raster tools.
        self.iface.addPluginToRasterMenu(MENU_TITLE, self.action)

    def unload(self):
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginRasterMenu(MENU_TITLE, self.action)
        self.action = None

    def show_dialog(self):
        # One run at a time: tell the user up front rather than letting them fill
        # in the dialog only to have engine.run() refuse the task afterwards.
        if engine.active_task() is not None:
            self.iface.messageBar().pushWarning(
                MENU_TITLE, "A download/export is already running — cancel it in "
                            "the Task Manager first to start another.")
            return

        dlg = BasemapTileDialog(self.iface.mapCanvas(), self.iface.mainWindow())
        if not dlg.exec():
            return

        # values() keys match engine.run()'s keyword arguments (see dialog).
        values = dlg.values()
        layer, extent = values["layer"], values["extent"]
        if layer is None or engine.source_for(layer) is None:
            self.iface.messageBar().pushWarning(
                MENU_TITLE, "Select a recognised WMS / WMTS / XYZ or local raster (GeoTIFF) layer.")
            return
        if extent is None or extent.isEmpty():
            self.iface.messageBar().pushWarning(
                MENU_TITLE, "Set an extent to render.")
            return

        try:
            # Re-check just before starting: a run may have been started (e.g.
            # from the Python console) while the dialog was open. engine.run()
            # would refuse it and return the running task, and the "started"
            # message below would then be a lie.
            if engine.active_task() is not None:
                self.iface.messageBar().pushWarning(
                    MENU_TITLE, "A download/export is already running — cancel it "
                                "in the Task Manager first to start another.")
                return
            local = getattr(engine.source_for(layer), "LOCAL", False)
            self._progress_verb = "Reading" if local else "Downloading"
            self._clear_progress()          # drop any counter left from a prior run
            task = engine.run(**values,
                              on_finished=self._on_run_finished,
                              on_mosaic_start=self._on_mosaic_start,
                              on_tile_progress=self._on_tile_progress)
            if task is None:    # refused (bad layer params, …) — details in the log
                self.iface.messageBar().pushCritical(
                    MENU_TITLE, "Could not start — see the Log Messages panel "
                                "(Basemap Tile Downloader tab) for details.")
                return
            self._raise_log_panel()
            # A local raster is read/exported, not downloaded.
            started = "Export" if local else "Download"
            self.iface.messageBar().pushInfo(
                MENU_TITLE,
                f"{started} started — progress in the Task Manager; live log in the "
                "Log Messages panel (Basemap Tile Downloader tab).")
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "Basemap Tile Downloader", Qgis.MessageLevel.Critical)
            self.iface.messageBar().pushCritical(MENU_TITLE, str(e))

    def _raise_log_panel(self):
        """Show/raise QGIS's Log Messages panel so the live run log is visible.
        The dock's objectName ('MessageLog') is stable across UI languages."""
        try:
            dock = self.iface.mainWindow().findChild(QDockWidget, "MessageLog")
            if dock is None:
                return
            if hasattr(dock, "setUserVisible"):     # QgsDockWidget: show + raise tab
                dock.setUserVisible(True)
            else:
                dock.setVisible(True)
                dock.raise_()
        except Exception:  # nosec B110
            pass

    def _on_tile_progress(self, done, total):
        """Live per-tile counter in the message bar (runs on the main thread).
        Updates a single widget in place, so it never adds log lines. Best-effort
        — a UI hiccup must not disturb the run."""
        try:
            if self._progress_label is None:
                self._progress_label = QLabel()
                self._progress_item = self.iface.messageBar().pushWidget(
                    self._progress_label, Qgis.MessageLevel.Info)
                self._progress_start = time.monotonic()
                # Baseline: tiles already resolved when this run's counter first
                # appeared (resumed and shared-cache tiles cost ~no time now). The
                # pace is measured only over tiles fetched *after* this point, so
                # the ETA reflects the real download rate — including the whole
                # count would dilute the pace toward zero and under-estimate it.
                self._progress_done0 = done
            pct = int(100 * done / total) if total else 100
            txt = f"{self._progress_verb} tiles… {done:,} / {total:,} ({pct}%)"
            elapsed = time.monotonic() - self._progress_start
            fetched = done - self._progress_done0
            remaining = (total - done) if total else 0
            if fetched > 0 and remaining > 0:
                eta = remaining * (elapsed / fetched)
                txt += f" · ~{_format_duration(eta)} left"
            self._progress_label.setText(txt)
        except Exception:  # nosec B110
            pass

    def _clear_progress(self):
        """Remove the live per-tile counter and the mosaic-building notice, if
        present."""
        bar = self.iface.messageBar()
        for item in (self._progress_item, self._mosaic_item):
            if item is not None:
                try:
                    bar.popWidget(item)
                except Exception:  # nosec B110
                    pass
        self._progress_item = None
        self._progress_label = None
        self._mosaic_item = None

    def _on_mosaic_start(self):
        """Show a canvas message-bar notice when the fetch phase ends and the
        mosaic build begins (runs on the main thread). The progress bar is already
        at 100% here, but the mosaic step reports no progress and can take a while
        — this reassures the user it isn't stuck. Pushed as a persistent widget
        (not a self-dismissing message) so it stays for the whole build, and is
        cleared in _on_run_finished."""
        self._clear_progress()          # fetch done — retire the per-tile counter
        label = QLabel("All tiles ready — building the GeoTIFF mosaic "
                       "(this can take a moment)…")
        self._mosaic_item = self.iface.messageBar().pushWidget(label, Qgis.MessageLevel.Info)

    def _on_run_finished(self, result):
        """Post a completion summary to the message bar (runs on the main thread).
        The mosaic is built only when every tile is present, so a loaded result is
        always complete; an incomplete run defers the mosaic and asks for a re-run."""
        self._clear_progress()
        bar = self.iface.messageBar()
        s = result.get("summary") or {}
        total, done = s.get("total", 0), s.get("done", 0)
        failed = s.get("failed", 0)
        missing = max(0, total - done)      # failed + not-yet-fetched (cancelled)
        cancelled = result.get("cancelled")
        server_gave_up = result.get("server_gave_up")
        past = "read" if result.get("local") else "downloaded"

        if result.get("loaded"):
            if missing > 0:     # "build partial" produced a mosaic with gaps
                bar.pushMessage(
                    MENU_TITLE,
                    f"Partial mosaic loaded — {done} of {total} tiles "
                    f"({missing} missing, left as gaps). Re-run with "
                    "“Build mosaic even if some tiles are missing” off to fill them.",
                    level=Qgis.MessageLevel.Warning)
            else:
                bar.pushMessage(
                    MENU_TITLE, f"Mosaic loaded — {done} tiles.", level=Qgis.MessageLevel.Success)
        elif result.get("error"):
            bar.pushCritical(MENU_TITLE, result.get("error"))
        elif missing == 0:                  # complete, but every tile was empty
            bar.pushInfo(MENU_TITLE, f"All {total} tiles are empty — no data to mosaic.")
        else:
            # Incomplete: the mosaic is deferred until every tile is present.
            reason = ("Cancelled" if cancelled else
                      "Server unavailable — stopped early" if server_gave_up else
                      f"{failed} tile(s) failed" if failed else "Incomplete")
            bar.pushWarning(
                MENU_TITLE,
                f"{reason} — {done} of {total} tiles {past} ({missing} to go). "
                f"Re-run to continue; the mosaic is built once all tiles are ready.")

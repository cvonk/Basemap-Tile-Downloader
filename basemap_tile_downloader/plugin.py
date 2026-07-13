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
        dlg = BasemapTileDialog(self.iface.mapCanvas(), self.iface.mainWindow())
        if not dlg.exec():
            return

        (layer, extent, extent_crs, opts, out_crs, output_path, temporary,
         resample, clip, concurrency, max_attempts, min_delay,
         backoff_cap, giveup_after) = dlg.values()
        if layer is None or engine.source_for(layer) is None:
            self.iface.messageBar().pushWarning(
                MENU_TITLE, "Select a recognised WMS / WMTS / XYZ or local raster (GeoTIFF) layer.")
            return
        if extent is None or extent.isEmpty():
            self.iface.messageBar().pushWarning(
                MENU_TITLE, "Set an extent to render.")
            return

        try:
            local = getattr(engine.source_for(layer), "LOCAL", False)
            self._progress_verb = "Reading" if local else "Downloading"
            self._clear_progress()          # drop any counter left from a prior run
            engine.run(layer=layer, extent=extent, extent_crs=extent_crs, opts=opts,
                       out_crs=out_crs, output_path=output_path, temporary=temporary,
                       resample=resample, clip=clip, concurrency=concurrency,
                       max_attempts=max_attempts, min_delay=min_delay,
                       backoff_cap=backoff_cap, giveup_after=giveup_after,
                       on_finished=self._on_run_finished,
                       on_mosaic_start=self._on_mosaic_start,
                       on_tile_progress=self._on_tile_progress)
            self._raise_log_panel()
            # A local raster is read/exported, not downloaded.
            started = "Export" if local else "Download"
            self.iface.messageBar().pushInfo(
                MENU_TITLE,
                f"{started} started — progress in the Task Manager; live log in the "
                "Log Messages panel (Basemap Tile Downloader tab).")
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "Basemap Tile Downloader", Qgis.Critical)
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
                    self._progress_label, Qgis.Info)
                self._progress_start = time.monotonic()
            pct = int(100 * done / total) if total else 100
            txt = f"{self._progress_verb} tiles… {done:,} / {total:,} ({pct}%)"
            elapsed = time.monotonic() - self._progress_start
            if done:
                txt += f" · ~{elapsed / done:.1f}s/tile"
            self._progress_label.setText(txt)
        except Exception:  # nosec B110
            pass

    def _clear_progress(self):
        """Remove the live per-tile counter widget, if present."""
        if self._progress_item is not None:
            try:
                self.iface.messageBar().popWidget(self._progress_item)
            except Exception:  # nosec B110
                pass
        self._progress_item = None
        self._progress_label = None

    def _on_mosaic_start(self):
        """Flash a note when the fetch phase ends and the mosaic build begins
        (runs on the main thread). The progress bar is already at 100% here, but
        the mosaic step reports no progress and can take a while — this reassures
        the user it isn't stuck."""
        self._clear_progress()          # fetch done — retire the per-tile counter
        self.iface.messageBar().pushInfo(
            MENU_TITLE, "All tiles ready — building the GeoTIFF mosaic "
                        "(this can take a moment)…")

    def _on_run_finished(self, result):
        """Post a completion summary to the message bar (runs on the main thread)."""
        self._clear_progress()          # safety net (e.g. cancel before any mosaic)
        bar = self.iface.messageBar()
        s = result.get("summary") or {}
        total, done = s.get("total", 0), s.get("done", 0)
        missing = max(0, total - done)      # failed + not-yet-fetched (cancelled)
        cancelled = result.get("cancelled")
        server_gave_up = result.get("server_gave_up")
        local = result.get("local")     # local raster: read/export, not download

        if result.get("loaded"):
            if cancelled:
                bar.pushWarning(
                    MENU_TITLE,
                    f"Cancelled — partial mosaic loaded ({done} of {total} tiles, "
                    f"{missing} missing). Re-run to fill the gaps.")
            elif server_gave_up:
                bar.pushWarning(
                    MENU_TITLE,
                    f"Server unavailable — stopped early; partial mosaic loaded "
                    f"({done} of {total} tiles, {missing} missing). Re-run later to "
                    f"fill the gaps.")
            elif missing:
                bar.pushWarning(
                    MENU_TITLE,
                    f"Mosaic loaded — {done} of {total} tiles ({missing} missing) — "
                    f"see download.log.")
            else:
                bar.pushMessage(
                    MENU_TITLE, f"Mosaic loaded — {done} tiles.", level=Qgis.Success)
        elif cancelled:
            bar.pushInfo(MENU_TITLE,
                         f"Cancelled before any tiles were {'read' if local else 'downloaded'}.")
        else:
            bar.pushCritical(MENU_TITLE,
                             result.get("error") or f"{'Export' if local else 'Download'} failed.")

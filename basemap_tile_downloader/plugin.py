# -*- coding: utf-8 -*-
"""
Basemap Tile Downloader – plugin glue.

Adds a Raster-menu entry (+ toolbar button), shows the source-aware dialog, then
hands off to engine.run() which auto-detects the source backend (WMS / WMTS /
XYZ / local raster).
"""
import os

from qgis.PyQt.QtWidgets import QAction
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
         resample, clip, concurrency, max_attempts, min_delay) = dlg.values()
        if layer is None or engine.source_for(layer) is None:
            self.iface.messageBar().pushWarning(
                MENU_TITLE, "Select a recognised WMS / WMTS / XYZ or local raster (GeoTIFF) layer.")
            return
        if extent is None or extent.isEmpty():
            self.iface.messageBar().pushWarning(
                MENU_TITLE, "Set an extent to render.")
            return

        try:
            engine.run(layer=layer, extent=extent, extent_crs=extent_crs, opts=opts,
                       out_crs=out_crs, output_path=output_path, temporary=temporary,
                       resample=resample, clip=clip, concurrency=concurrency,
                       max_attempts=max_attempts, min_delay=min_delay,
                       on_finished=self._on_run_finished)
            self.iface.messageBar().pushInfo(
                MENU_TITLE, "Download started — watch the Task Manager panel.")
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "Basemap Tile Downloader", Qgis.Critical)
            self.iface.messageBar().pushCritical(MENU_TITLE, str(e))

    def _on_run_finished(self, result):
        """Post a completion summary to the message bar (runs on the main thread)."""
        bar = self.iface.messageBar()
        s = result.get("summary") or {}
        total, done = s.get("total", 0), s.get("done", 0)
        missing = max(0, total - done)      # failed + not-yet-fetched (cancelled)
        cancelled = result.get("cancelled")

        if result.get("loaded"):
            if cancelled:
                bar.pushWarning(
                    MENU_TITLE,
                    f"Cancelled — partial mosaic loaded ({done} of {total} tiles, "
                    f"{missing} missing). Re-run to fill the gaps.")
            elif missing:
                bar.pushWarning(
                    MENU_TITLE,
                    f"Mosaic loaded — {done} of {total} tiles ({missing} missing) — "
                    f"see download.log.")
            else:
                bar.pushMessage(
                    MENU_TITLE, f"Mosaic loaded — {done} tiles.", level=Qgis.Success)
        elif cancelled:
            bar.pushInfo(MENU_TITLE, "Cancelled before any tiles were downloaded.")
        else:
            bar.pushCritical(MENU_TITLE, result.get("error") or "Download failed.")

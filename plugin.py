# -*- coding: utf-8 -*-
"""
WMS AOI Downloader – plugin glue.

Adds a menu entry (+ toolbar button), shows a small dialog to pick the WMS and
AOI layers and the tile/resolution settings, then hands off to the download
engine in core.py.
"""
import os

from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.core import Qgis, QgsMessageLog

from .dialog import WmsAoiDialog
from . import core

# Where the entry appears in the main menu bar:
#   "web"     -> Web menu (convention for WMS / web-service tools)
#   "plugins" -> Plugins menu
MENU = "web"

MENU_TITLE = "WMS AOI Downloader"


class WmsAoiDownloaderPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self._icon_path = os.path.join(os.path.dirname(__file__), "icon.svg")

    # ── QGIS lifecycle ──────────────────────────────────────────────────────
    def initGui(self):
        self.action = QAction(
            QIcon(self._icon_path), "WMS AOI Downloader…", self.iface.mainWindow())
        self.action.triggered.connect(self.show_dialog)
        self.iface.addToolBarIcon(self.action)
        if MENU == "web":
            self.iface.addPluginToWebMenu(MENU_TITLE, self.action)
        else:
            self.iface.addPluginToMenu(MENU_TITLE, self.action)

    def unload(self):
        self.iface.removeToolBarIcon(self.action)
        if MENU == "web":
            self.iface.removePluginWebMenu(MENU_TITLE, self.action)
        else:
            self.iface.removePluginMenu(MENU_TITLE, self.action)
        self.action = None

    # ── action ──────────────────────────────────────────────────────────────
    def show_dialog(self):
        dlg = WmsAoiDialog(self.iface.mainWindow())
        if not dlg.exec():
            return

        wms_layer, aoi_layer, tile_pixels, target_resolution, output_path, temporary = \
            dlg.values()
        if wms_layer is None or aoi_layer is None:
            self.iface.messageBar().pushWarning(
                MENU_TITLE, "Select both a WMS raster layer and an AOI polygon layer.")
            return
        if not temporary and not output_path:
            self.iface.messageBar().pushWarning(
                MENU_TITLE, "Choose an output file, or select 'Temporary file'.")
            return

        try:
            core.run(wms_layer=wms_layer, aoi_layer=aoi_layer,
                     tile_pixels=tile_pixels, target_resolution=target_resolution,
                     output_path=output_path, temporary=temporary)
            self.iface.messageBar().pushInfo(
                MENU_TITLE, "Download started — watch the Task Manager panel.")
        except Exception as e:  # surface engine errors in the GUI, not just the log
            QgsMessageLog.logMessage(str(e), "WMS AOI Downloader", Qgis.Critical)
            self.iface.messageBar().pushCritical(MENU_TITLE, str(e))

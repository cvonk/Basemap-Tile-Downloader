# -*- coding: utf-8 -*-
"""
AOI Downloader – plugin glue.

Adds a menu entry (+ toolbar button), shows the source-aware dialog, then hands
off to engine.run() which auto-detects the WMS/XYZ backend.
"""
import os

from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.core import Qgis, QgsMessageLog

from .dialog import AoiDialog
from . import engine

# "web" -> Web menu (convention for web-service tools); "plugins" -> Plugins menu
MENU = "web"
MENU_TITLE = "AOI Downloader"


class AoiDownloaderPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self._icon_path = os.path.join(os.path.dirname(__file__), "icon.svg")

    def initGui(self):
        self.action = QAction(
            QIcon(self._icon_path), "AOI Downloader…", self.iface.mainWindow())
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

    def show_dialog(self):
        dlg = AoiDialog(self.iface.mainWindow())
        if not dlg.exec():
            return

        layer, aoi_layer, opts, out_crs, output_path, temporary = dlg.values()
        if layer is None or aoi_layer is None:
            self.iface.messageBar().pushWarning(
                MENU_TITLE, "Select both a WMS/XYZ source layer and an AOI polygon layer.")
            return
        if engine.source_for(layer) is None:
            self.iface.messageBar().pushWarning(
                MENU_TITLE, "The selected layer is not a recognised WMS or XYZ tile layer.")
            return
        if not temporary and not output_path:
            self.iface.messageBar().pushWarning(
                MENU_TITLE, "Choose an output file, or select 'Temporary file'.")
            return

        try:
            engine.run(layer=layer, aoi_layer=aoi_layer, opts=opts,
                       out_crs=out_crs, output_path=output_path, temporary=temporary)
            self.iface.messageBar().pushInfo(
                MENU_TITLE, "Download started — watch the Task Manager panel.")
        except Exception as e:
            QgsMessageLog.logMessage(str(e), "AOI Downloader", Qgis.Critical)
            self.iface.messageBar().pushCritical(MENU_TITLE, str(e))

# -*- coding: utf-8 -*-
"""WMS AOI Downloader – QGIS plugin entry point."""


def classFactory(iface):
    from .plugin import WmsAoiDownloaderPlugin
    return WmsAoiDownloaderPlugin(iface)

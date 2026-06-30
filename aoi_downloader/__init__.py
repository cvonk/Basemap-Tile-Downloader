# -*- coding: utf-8 -*-
"""AOI Downloader – QGIS plugin entry point (WMS + XYZ)."""


def classFactory(iface):
    from .plugin import AoiDownloaderPlugin
    return AoiDownloaderPlugin(iface)

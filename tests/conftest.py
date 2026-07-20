# -*- coding: utf-8 -*-
"""Install minimal qgis.* stub modules when QGIS is not importable, so the
fetch-classification tests can import engine.py and the source backends with a
plain Python (the modules only *reference* QGIS classes at import time; the
tests stub the network and GDAL calls). Under a real QGIS Python the genuine
modules are used and nothing is stubbed."""

import sys
import types


def _install_qgis_stubs():
    try:
        import qgis.core  # noqa: F401  (real QGIS available — nothing to do)
        return
    except ImportError:
        pass

    class _Stub:
        """Placeholder for any QGIS class the modules name but tests never use."""
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            return _Stub()

    qgis_m = types.ModuleType("qgis")
    pyqt   = types.ModuleType("qgis.PyQt")
    qtcore = types.ModuleType("qgis.PyQt.QtCore")
    qtnet  = types.ModuleType("qgis.PyQt.QtNetwork")
    core   = types.ModuleType("qgis.core")
    gui    = types.ModuleType("qgis.gui")

    qtcore.QUrl = _Stub
    qtcore.pyqtSignal = lambda *a, **k: _Stub()
    qtnet.QNetworkRequest = _Stub

    class QgsTask:              # engine subclasses it at import time
        class Flag:             # scoped enum, as the real (Qt6-safe) code uses
            CanCancel = 0x2

    for name in ("Qgis", "QgsProject", "QgsApplication", "QgsMessageLog",
                 "QgsRasterLayer", "QgsBlockingNetworkRequest", "QgsGeometry",
                 "QgsCoordinateReferenceSystem", "QgsCoordinateTransform",
                 "QgsRectangle", "QgsDataSourceUri", "QgsVectorLayer",
                 "QgsSettings", "QgsMapLayerProxyModel"):
        setattr(core, name, _Stub)
    core.QgsTask = QgsTask

    qgis_m.PyQt, pyqt.QtCore, pyqt.QtNetwork = pyqt, qtcore, qtnet
    qgis_m.core, qgis_m.gui = core, gui
    sys.modules.update({
        "qgis": qgis_m, "qgis.PyQt": pyqt, "qgis.PyQt.QtCore": qtcore,
        "qgis.PyQt.QtNetwork": qtnet, "qgis.core": core, "qgis.gui": gui,
    })


_install_qgis_stubs()

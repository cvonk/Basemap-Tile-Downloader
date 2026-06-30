# -*- coding: utf-8 -*-
"""WMS AOI Downloader – parameter dialog (built in code, no .ui file needed)."""

from qgis.PyQt.QtWidgets import (
    QDialog, QFormLayout, QVBoxLayout, QDialogButtonBox,
    QSpinBox, QDoubleSpinBox, QLabel, QComboBox,
)
from qgis.core import (
    QgsProject, QgsMapLayerProxyModel, QgsRasterLayer, QgsSettings,
)
from qgis.gui import QgsMapLayerComboBox, QgsFileWidget

from . import core

SETTINGS_GROUP = "wms_aoi_downloader"


class WmsAoiDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("WMS AOI Downloader")
        self.setMinimumWidth(480)

        form = QFormLayout()

        # WMS combo: raster layers, then exclude every non-WMS raster so only
        # actual WMS layers remain selectable.
        self.wms_combo = QgsMapLayerComboBox()
        self.wms_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.wms_combo.setAllowEmptyLayer(True)
        self._restrict_to_wms()
        form.addRow("WMS raster layer:", self.wms_combo)

        self.aoi_combo = QgsMapLayerComboBox()
        self.aoi_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        form.addRow("AOI polygon layer:", self.aoi_combo)

        self.tile_spin = QSpinBox()
        self.tile_spin.setRange(256, 8192)
        self.tile_spin.setSingleStep(256)
        form.addRow("Tile size (px):", self.tile_spin)

        self.res_spin = QDoubleSpinBox()
        self.res_spin.setDecimals(3)
        self.res_spin.setRange(0.001, 1000.0)
        self.res_spin.setSingleStep(0.1)
        form.addRow("Resolution (map units/px):", self.res_spin)

        self.out_mode = QComboBox()
        self.out_mode.addItem("Save to file…", "file")
        self.out_mode.addItem("Temporary file", "temp")
        self.out_mode.currentIndexChanged.connect(self._sync_out_widgets)
        form.addRow("Output:", self.out_mode)

        self.out_file = QgsFileWidget()
        self.out_file.setStorageMode(QgsFileWidget.SaveFile)
        self.out_file.setFilter("GeoTIFF (*.tif *.tiff)")
        form.addRow("Output file:", self.out_file)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        note = QLabel("Changing the resolution or tile size discards the old "
                      "queue and starts a fresh download.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(buttons)

        self._restore_state()
        self._sync_out_widgets()

    # ── filtering ─────────────────────────────────────────────────────────────
    def _restrict_to_wms(self):
        excepted = [l for l in QgsProject.instance().mapLayers().values()
                    if isinstance(l, QgsRasterLayer) and l.providerType() != "wms"]
        self.wms_combo.setExceptedLayerList(excepted)

    def _sync_out_widgets(self):
        self.out_file.setEnabled(self.out_mode.currentData() == "file")

    # ── settings persistence ──────────────────────────────────────────────────
    def _restore_state(self):
        s, g = QgsSettings(), SETTINGS_GROUP
        self.tile_spin.setValue(int(s.value(f"{g}/tile_pixels", core.TILE_PIXELS)))
        self.res_spin.setValue(float(s.value(f"{g}/resolution", core.TARGET_RESOLUTION)))

        idx = self.out_mode.findData(s.value(f"{g}/output_mode", "file"))
        if idx >= 0:
            self.out_mode.setCurrentIndex(idx)
        self.out_file.setFilePath(s.value(f"{g}/output_path", "") or "")

        proj = QgsProject.instance()
        self._restore_layer(self.wms_combo, s.value(f"{g}/wms_layer_id", ""),
                            core.WMS_LAYER_NAME, proj)
        self._restore_layer(self.aoi_combo, s.value(f"{g}/aoi_layer_id", ""),
                            core.AOI_LAYER_NAME, proj)

    @staticmethod
    def _restore_layer(combo, stored_id, default_name, proj):
        """Select the previously-used layer if it still exists, else fall back
        to the historical default layer name."""
        if stored_id and proj.mapLayer(stored_id):
            combo.setLayer(proj.mapLayer(stored_id))
            return
        for lyr in proj.mapLayersByName(default_name):
            combo.setLayer(lyr)
            return

    def _save_state(self):
        s, g = QgsSettings(), SETTINGS_GROUP
        s.setValue(f"{g}/tile_pixels", self.tile_spin.value())
        s.setValue(f"{g}/resolution", self.res_spin.value())
        s.setValue(f"{g}/output_mode", self.out_mode.currentData())
        s.setValue(f"{g}/output_path", self.out_file.filePath())
        wl, al = self.wms_combo.currentLayer(), self.aoi_combo.currentLayer()
        s.setValue(f"{g}/wms_layer_id", wl.id() if wl else "")
        s.setValue(f"{g}/aoi_layer_id", al.id() if al else "")

    def accept(self):
        self._save_state()
        super().accept()

    # ── result ────────────────────────────────────────────────────────────────
    def values(self):
        temporary = self.out_mode.currentData() == "temp"
        out_path = None if temporary else (self.out_file.filePath() or None)
        return (self.wms_combo.currentLayer(),
                self.aoi_combo.currentLayer(),
                self.tile_spin.value(),
                self.res_spin.value(),
                out_path,
                temporary)

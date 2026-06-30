# -*- coding: utf-8 -*-
"""
AOI Downloader – parameter dialog.

One layer combo (WMS + XYZ tile layers). The source type is auto-detected from
the chosen layer, and the relevant parameter fields are shown: tile size +
resolution for WMS, zoom level for XYZ.
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QFormLayout, QVBoxLayout, QDialogButtonBox,
    QSpinBox, QDoubleSpinBox, QComboBox, QLabel,
)
from qgis.core import (
    QgsProject, QgsMapLayerProxyModel, QgsRasterLayer, QgsSettings,
    QgsCoordinateReferenceSystem,
)
from qgis.gui import QgsMapLayerComboBox, QgsFileWidget, QgsProjectionSelectionWidget

from . import engine
from .sources import xyz

SETTINGS_GROUP = "aoi_downloader"

DEFAULT_TILE_PIXELS = 1024
DEFAULT_RESOLUTION  = 0.5
DEFAULT_ZOOM        = 18


class AoiDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AOI Downloader")
        self.setMinimumWidth(500)
        self._last_source = None

        form = QFormLayout()

        # One combo for both source types: raster layers minus anything that
        # isn't a recognised WMS/XYZ source.
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.layer_combo.setAllowEmptyLayer(True)
        self._restrict_to_sources()
        self.layer_combo.layerChanged.connect(self._on_layer_changed)
        form.addRow("Source layer (WMS/XYZ):", self.layer_combo)

        self.aoi_combo = QgsMapLayerComboBox()
        self.aoi_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        form.addRow("AOI polygon layer:", self.aoi_combo)

        # WMS-only rows ------------------------------------------------------
        self.tile_lbl  = QLabel("Tile size (px):")
        self.tile_spin = QSpinBox(); self.tile_spin.setRange(256, 8192)
        self.tile_spin.setSingleStep(256)
        form.addRow(self.tile_lbl, self.tile_spin)

        self.res_lbl  = QLabel("Resolution (units/px):")
        self.res_spin = QDoubleSpinBox(); self.res_spin.setDecimals(3)
        self.res_spin.setRange(0.001, 1000.0); self.res_spin.setSingleStep(0.1)
        form.addRow(self.res_lbl, self.res_spin)

        # XYZ-only rows ------------------------------------------------------
        self.zoom_lbl  = QLabel("Zoom level:")
        self.zoom_spin = QSpinBox(); self.zoom_spin.setRange(0, 22)
        self.zoom_spin.valueChanged.connect(self._update_zoom_label)
        form.addRow(self.zoom_lbl, self.zoom_spin)

        self.zoom_res_lbl  = QLabel("")
        self.zoom_res_info = QLabel("")
        form.addRow(self.zoom_res_lbl, self.zoom_res_info)

        # Common -------------------------------------------------------------
        self.crs_widget = QgsProjectionSelectionWidget()
        form.addRow("Output CRS:", self.crs_widget)

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
        note = QLabel("WMS is requested at the chosen resolution/CRS; XYZ is "
                      "fetched in EPSG:3857 at the chosen zoom and reprojected to "
                      "the output CRS. Changing the parameters or AOI starts a "
                      "fresh download.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(buttons)

        self._restore_state()
        self._sync_out_widgets()
        self._on_layer_changed()

    # ── filtering / visibility ────────────────────────────────────────────────
    def _restrict_to_sources(self):
        excepted = [l for l in QgsProject.instance().mapLayers().values()
                    if isinstance(l, QgsRasterLayer) and engine.source_for(l) is None]
        self.layer_combo.setExceptedLayerList(excepted)

    def _current_source_name(self):
        layer = self.layer_combo.currentLayer()
        src = engine.source_for(layer) if layer else None
        return src.SOURCE_NAME if src else None

    def _set_row_visible(self, label, field, visible):
        label.setVisible(visible); field.setVisible(visible)

    def _on_layer_changed(self, *args):
        name = self._current_source_name()
        is_wms, is_xyz = (name == "WMS"), (name == "XYZ")
        self._set_row_visible(self.tile_lbl, self.tile_spin, is_wms)
        self._set_row_visible(self.res_lbl,  self.res_spin,  is_wms)
        self._set_row_visible(self.zoom_lbl, self.zoom_spin, is_xyz)
        self._set_row_visible(self.zoom_res_lbl, self.zoom_res_info, is_xyz)
        self._update_zoom_label()

        # On a source-type change, default the output CRS to that source's native.
        if name and name != self._last_source:
            layer = self.layer_combo.currentLayer()
            src = engine.source_for(layer)
            try:
                params = src.extract_params(layer)
                self.crs_widget.setCrs(
                    QgsCoordinateReferenceSystem(src.default_out_crs(params)))
            except Exception:
                pass
        self._last_source = name

    def _update_zoom_label(self, *args):
        self.zoom_res_info.setText(
            f"≈ {xyz.tile_resolution_m(self.zoom_spin.value()):.3f} m/px at the equator")

    def _sync_out_widgets(self, *args):
        self.out_file.setEnabled(self.out_mode.currentData() == "file")

    # ── settings persistence ──────────────────────────────────────────────────
    def _restore_state(self):
        s, g = QgsSettings(), SETTINGS_GROUP
        self.tile_spin.setValue(int(s.value(f"{g}/wms_tile_pixels", DEFAULT_TILE_PIXELS)))
        self.res_spin.setValue(float(s.value(f"{g}/wms_resolution", DEFAULT_RESOLUTION)))
        self.zoom_spin.setValue(int(s.value(f"{g}/xyz_zoom", DEFAULT_ZOOM)))

        idx = self.out_mode.findData(s.value(f"{g}/output_mode", "file"))
        if idx >= 0:
            self.out_mode.setCurrentIndex(idx)
        self.out_file.setFilePath(s.value(f"{g}/output_path", "") or "")

        # Set the layers first (this fires _on_layer_changed, which may default
        # the output CRS to the source's native CRS)…
        proj = QgsProject.instance()
        lid = s.value(f"{g}/layer_id", "")
        if lid and proj.mapLayer(lid):
            self.layer_combo.setLayer(proj.mapLayer(lid))
        aid = s.value(f"{g}/aoi_layer_id", "")
        if aid and proj.mapLayer(aid):
            self.aoi_combo.setLayer(proj.mapLayer(aid))
        else:
            for lyr in proj.mapLayersByName("Area of Interest (EPSG:32632)"):
                self.aoi_combo.setLayer(lyr); break

        # …then restore the remembered output CRS so it wins over the default,
        # and pin _last_source so the final refresh won't clobber it again.
        out_crs = s.value(f"{g}/out_crs", "")
        if out_crs:
            self.crs_widget.setCrs(QgsCoordinateReferenceSystem(out_crs))
        self._last_source = self._current_source_name()

    def _save_state(self):
        s, g = QgsSettings(), SETTINGS_GROUP
        s.setValue(f"{g}/wms_tile_pixels", self.tile_spin.value())
        s.setValue(f"{g}/wms_resolution", self.res_spin.value())
        s.setValue(f"{g}/xyz_zoom", self.zoom_spin.value())
        if self.crs_widget.crs().isValid():
            s.setValue(f"{g}/out_crs", self.crs_widget.crs().authid())
        s.setValue(f"{g}/output_mode", self.out_mode.currentData())
        s.setValue(f"{g}/output_path", self.out_file.filePath())
        ly, al = self.layer_combo.currentLayer(), self.aoi_combo.currentLayer()
        s.setValue(f"{g}/layer_id", ly.id() if ly else "")
        s.setValue(f"{g}/aoi_layer_id", al.id() if al else "")

    def accept(self):
        self._save_state()
        super().accept()

    # ── result ────────────────────────────────────────────────────────────────
    def values(self):
        layer = self.layer_combo.currentLayer()
        aoi   = self.aoi_combo.currentLayer()
        name  = self._current_source_name()
        if name == "WMS":
            opts = {"tile_pixels": self.tile_spin.value(),
                    "resolution":  self.res_spin.value()}
        elif name == "XYZ":
            opts = {"zoom": self.zoom_spin.value()}
        else:
            opts = {}
        crs = self.crs_widget.crs()
        out_crs = crs.authid() if crs.isValid() else None
        temporary = self.out_mode.currentData() == "temp"
        out_path = None if temporary else (self.out_file.filePath() or None)
        return (layer, aoi, opts, out_crs, out_path, temporary)

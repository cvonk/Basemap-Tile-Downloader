# -*- coding: utf-8 -*-
"""
AOI Downloader – parameter dialog.

One layer combo (WMS + XYZ tile layers). The source type is auto-detected from
the chosen layer, and the relevant parameter fields are shown: tile size +
resolution for WMS, zoom level for XYZ.
"""

import math

from qgis.PyQt.QtWidgets import (
    QDialog, QFormLayout, QVBoxLayout, QHBoxLayout, QDialogButtonBox,
    QSpinBox, QDoubleSpinBox, QLabel, QWidget, QLineEdit, QToolButton,
    QMenu, QFileDialog, QMessageBox, QComboBox, QCheckBox,
)
from qgis.core import (
    QgsProject, QgsMapLayerProxyModel, QgsRasterLayer, QgsSettings,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
)
from qgis.gui import QgsMapLayerComboBox, QgsProjectionSelectionWidget

from . import engine, tilemath

SETTINGS_GROUP = "aoi_downloader"

DEFAULT_TILE_PIXELS = 1024
DEFAULT_RESOLUTION  = 0.5
DEFAULT_ZOOM        = 18

# Ask for confirmation above this estimated tile count.
WARN_TILE_COUNT = 5000


class OutputDestinationWidget(QWidget):
    """A single output control like QGIS's own dialogs: a path line-edit plus a
    "…" dropdown offering 'Save to File…' / 'Save to Temporary File'. An empty
    field means a temporary file (shown as the placeholder)."""

    def __init__(self, parent=None, file_filter="GeoTIFF (*.tif *.tiff)"):
        super().__init__(parent)
        self._filter = file_filter

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self.edit = QLineEdit()
        self.edit.setPlaceholderText("[Save to temporary file]")
        self.edit.setClearButtonEnabled(True)

        self.btn = QToolButton()
        self.btn.setText("…")
        self.btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.btn)
        menu.addAction("Save to File…").triggered.connect(self._choose_file)
        menu.addAction("Save to Temporary File").triggered.connect(self._set_temporary)
        self.btn.setMenu(menu)

        lay.addWidget(self.edit)
        lay.addWidget(self.btn)

    def _choose_file(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Output GeoTIFF", self.edit.text().strip(), self._filter)
        if path:
            self.edit.setText(path)

    def _set_temporary(self):
        self.edit.clear()

    # public API -------------------------------------------------------------
    def is_temporary(self):
        return not self.edit.text().strip()

    def file_path(self):
        return None if self.is_temporary() else self.edit.text().strip()

    def set_file_path(self, path):
        self.edit.setText(path or "")


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
        self.aoi_combo.layerChanged.connect(self._update_estimate)
        self.aoi_combo.layerChanged.connect(self._update_zoom_label)
        form.addRow("AOI polygon layer:", self.aoi_combo)

        # WMS-only rows ------------------------------------------------------
        self.tile_lbl  = QLabel("Tile size (px):")
        self.tile_spin = QSpinBox(); self.tile_spin.setRange(256, 8192)
        self.tile_spin.setSingleStep(256)
        self.tile_spin.valueChanged.connect(self._update_estimate)
        form.addRow(self.tile_lbl, self.tile_spin)

        self.res_lbl  = QLabel("Resolution (units/px):")
        self.res_spin = QDoubleSpinBox(); self.res_spin.setDecimals(3)
        self.res_spin.setRange(0.001, 1000.0); self.res_spin.setSingleStep(0.1)
        self.res_spin.valueChanged.connect(self._update_estimate)
        form.addRow(self.res_lbl, self.res_spin)

        # XYZ-only rows ------------------------------------------------------
        self.zoom_lbl  = QLabel("Zoom level:")
        self.zoom_spin = QSpinBox(); self.zoom_spin.setRange(0, 22)
        self.zoom_spin.valueChanged.connect(self._update_zoom_label)
        self.zoom_spin.valueChanged.connect(self._update_estimate)
        form.addRow(self.zoom_lbl, self.zoom_spin)

        self.zoom_res_lbl  = QLabel("")
        self.zoom_res_info = QLabel("")
        form.addRow(self.zoom_res_lbl, self.zoom_res_info)

        # Estimated tile count (updates live) --------------------------------
        self.estimate_lbl = QLabel("")
        form.addRow("", self.estimate_lbl)

        # Common -------------------------------------------------------------
        self.crs_widget = QgsProjectionSelectionWidget()
        form.addRow("Output CRS:", self.crs_widget)

        self.resample_combo = QComboBox()
        self.resample_combo.addItem("Bilinear", "bilinear")
        self.resample_combo.addItem("Nearest neighbour", "near")
        self.resample_combo.addItem("Cubic", "cubic")
        self.resample_combo.addItem("None (keep native CRS, no reprojection)", "none")
        self.resample_combo.currentIndexChanged.connect(self._sync_resample)
        form.addRow("Reproject sampling:", self.resample_combo)

        self.clip_check = QCheckBox("Clip output to the AOI polygon")
        form.addRow("", self.clip_check)

        self.out_widget = OutputDestinationWidget()
        form.addRow("Output:", self.out_widget)

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
        self._on_layer_changed()
        self._update_estimate()
        self._sync_resample()

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
        is_wms  = (name == "WMS")
        is_zoom = name in ("XYZ", "WMTS")       # both address tiles by zoom level
        self._set_row_visible(self.tile_lbl, self.tile_spin, is_wms)
        self._set_row_visible(self.res_lbl,  self.res_spin,  is_wms)
        self._set_row_visible(self.zoom_lbl, self.zoom_spin, is_zoom)
        # The m/px note only applies to XYZ's fixed Web-Mercator grid.
        self._set_row_visible(self.zoom_res_lbl, self.zoom_res_info, name == "XYZ")
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
        self._update_estimate()

    def _aoi_center_lat(self):
        """Latitude (°) of the AOI's bounding-box centre, or None."""
        aoi = self.aoi_combo.currentLayer()
        if aoi is None:
            return None
        bb = self._aoi_bbox_in(aoi, QgsCoordinateReferenceSystem("EPSG:4326"))
        return None if bb is None else bb.center().y()

    def _update_zoom_label(self, *args):
        z = self.zoom_spin.value()
        lat = self._aoi_center_lat()
        if lat is None:
            self.zoom_res_info.setText(
                f"≈ {tilemath.tile_resolution_m(z):.3f} m/px at the equator")
        else:
            self.zoom_res_info.setText(
                f"≈ {tilemath.tile_resolution_m_at_lat(z, lat):.3f} m/px "
                f"at the AOI (~{lat:.1f}°)")

    # ── tile-count estimate ───────────────────────────────────────────────────
    def _aoi_bbox_in(self, aoi, target_crs):
        """The AOI layer's extent, reprojected to target_crs. None on failure."""
        try:
            ext = aoi.extent()
            if aoi.crs() == target_crs:
                return ext
            xform = QgsCoordinateTransform(
                aoi.crs(), target_crs, QgsProject.instance().transformContext())
            return xform.transformBoundingBox(ext)
        except Exception:
            return None

    def _estimate_tiles(self):
        """Upper-bound tile count over the AOI bounding box (no polygon
        intersection), or None if it can't be computed yet."""
        layer = self.layer_combo.currentLayer()
        aoi   = self.aoi_combo.currentLayer()
        name  = self._current_source_name()
        if layer is None or aoi is None or name is None:
            return None
        try:
            if name == "XYZ":
                bb = self._aoi_bbox_in(aoi, QgsCoordinateReferenceSystem("EPSG:3857"))
                if bb is None:
                    return None
                xmin, xmax, ymin, ymax = tilemath.tile_range(
                    bb.xMinimum(), bb.yMinimum(), bb.xMaximum(), bb.yMaximum(),
                    self.zoom_spin.value())
                return (xmax - xmin + 1) * (ymax - ymin + 1)
            if name == "WMS":
                params = engine.source_for(layer).extract_params(layer)   # no network
                bb = self._aoi_bbox_in(aoi, QgsCoordinateReferenceSystem(params["crs"]))
                if bb is None:
                    return None
                step = self.tile_spin.value() * self.res_spin.value()
                if step <= 0:
                    return None
                return (max(1, math.ceil(bb.width() / step)) *
                        max(1, math.ceil(bb.height() / step)))
        except Exception:
            return None
        return None

    def _update_estimate(self, *args):
        n = self._estimate_tiles()
        self.estimate_lbl.setText(
            "" if n is None else f"≈ {n:,} tiles (bounding-box estimate)")

    def _sync_resample(self, *args):
        # "None" keeps the native CRS, so the output-CRS picker is irrelevant.
        self.crs_widget.setEnabled(self.resample_combo.currentData() != "none")

    # ── settings persistence ──────────────────────────────────────────────────
    def _restore_state(self):
        s, g = QgsSettings(), SETTINGS_GROUP
        self.tile_spin.setValue(int(s.value(f"{g}/wms_tile_pixels", DEFAULT_TILE_PIXELS)))
        self.res_spin.setValue(float(s.value(f"{g}/wms_resolution", DEFAULT_RESOLUTION)))
        self.zoom_spin.setValue(int(s.value(f"{g}/xyz_zoom", DEFAULT_ZOOM)))

        # Empty path (or remembered temp mode) → temporary file.
        if s.value(f"{g}/output_mode", "file") == "temp":
            self.out_widget.set_file_path("")
        else:
            self.out_widget.set_file_path(s.value(f"{g}/output_path", "") or "")

        r = self.resample_combo.findData(s.value(f"{g}/resample", "bilinear"))
        if r >= 0:
            self.resample_combo.setCurrentIndex(r)
        self.clip_check.setChecked(s.value(f"{g}/clip", False, type=bool))

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
        s.setValue(f"{g}/output_mode", "temp" if self.out_widget.is_temporary() else "file")
        s.setValue(f"{g}/output_path", self.out_widget.file_path() or "")
        s.setValue(f"{g}/resample", self.resample_combo.currentData())
        s.setValue(f"{g}/clip", self.clip_check.isChecked())
        ly, al = self.layer_combo.currentLayer(), self.aoi_combo.currentLayer()
        s.setValue(f"{g}/layer_id", ly.id() if ly else "")
        s.setValue(f"{g}/aoi_layer_id", al.id() if al else "")

    def accept(self):
        n = self._estimate_tiles()
        if n and n > WARN_TILE_COUNT:
            reply = QMessageBox.question(
                self, "Large download",
                f"This will download roughly {n:,} tiles, which may be slow and "
                f"put load on the server.\n\n"
                f"Bulk-downloading tiles may violate the provider's Terms of "
                f"Service (e.g. Google, Bing, Esri). Make sure your intended use "
                f"is permitted before continuing.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return          # keep the dialog open
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
        elif name in ("XYZ", "WMTS"):
            opts = {"zoom": self.zoom_spin.value()}
        else:
            opts = {}
        crs = self.crs_widget.crs()
        out_crs = crs.authid() if crs.isValid() else None
        temporary = self.out_widget.is_temporary()
        out_path = self.out_widget.file_path()
        resample = self.resample_combo.currentData()
        clip = self.clip_check.isChecked()
        return (layer, aoi, opts, out_crs, out_path, temporary, resample, clip)

# -*- coding: utf-8 -*-
"""
Basemap Tile Downloader – parameter dialog.

One layer combo (WMS/WMTS/XYZ tile layers plus local GDAL rasters such as
GeoTIFF). The source type is auto-detected from the chosen layer, and the
relevant parameter fields are shown: tile size + resolution for WMS and local
rasters, zoom level for XYZ/WMTS.
"""

import configparser
import math
import os
import subprocess

from qgis.PyQt.QtWidgets import (
    QDialog, QFormLayout, QVBoxLayout, QHBoxLayout, QDialogButtonBox,
    QSpinBox, QDoubleSpinBox, QLabel, QWidget, QLineEdit, QToolButton,
    QMenu, QFileDialog, QMessageBox, QComboBox, QCheckBox, QPushButton,
)
from qgis.core import (
    QgsProject, QgsMapLayerProxyModel, QgsRasterLayer, QgsSettings, QgsRectangle,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
)
from qgis.gui import (
    QgsMapLayerComboBox, QgsProjectionSelectionWidget, QgsCollapsibleGroupBox,
    QgsExtentWidget,
)

from . import engine, tilemath

SETTINGS_GROUP = "basemap_tile_downloader"

DEFAULT_TILE_PIXELS = 1024
DEFAULT_RESOLUTION  = 0.5
DEFAULT_ZOOM         = 18
DEFAULT_CONCURRENCY  = 4
DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_MIN_DELAY    = 0.0
DEFAULT_MAX_TILES    = 0     # per-run tile budget; 0 = no limit
DEFAULT_REST_SECS    = 0     # rest after each macro-cell; 0 = off
# Sourced from the engine so the dialog's defaults can't drift from the real ones.
DEFAULT_BACKOFF_CAP  = engine.MAX_DELAY_SEC                # s; adaptive back-off ceiling
DEFAULT_GIVEUP_AFTER = engine.MAX_CONSECUTIVE_BACKPRESSURE  # consecutive fails → give up

# Ask for confirmation above this estimated tile count.
WARN_TILE_COUNT = 5000

_PLUGIN_DIR = os.path.dirname(__file__)


def _plugin_version():
    """Version string from metadata.txt (bundled with the plugin) — matches the
    released git tag. Empty string if it can't be read."""
    try:
        cp = configparser.ConfigParser(interpolation=None)   # tolerate % in values
        cp.read(os.path.join(_PLUGIN_DIR, "metadata.txt"), encoding="utf-8")
        return cp.get("general", "version", fallback="").strip()
    except Exception:
        return ""


def _git_short_hash():
    """Short commit hash if the plugin is running from a git checkout (dev), else
    "". An installed plugin has no .git, so this is normally empty."""
    try:
        out = subprocess.run(
            ["git", "-C", _PLUGIN_DIR, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _window_title():
    ver  = _plugin_version()
    sha  = _git_short_hash()
    tag  = " ".join(p for p in (f"v{ver}" if ver else "", f"({sha})" if sha else "") if p)
    return f"Basemap Tile Downloader — {tag}" if tag else "Basemap Tile Downloader"


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


class BasemapTileDialog(QDialog):
    def __init__(self, canvas=None, parent=None):
        super().__init__(parent)
        self._canvas = canvas
        self.setWindowTitle(_window_title())
        self.setMinimumWidth(500)
        self._last_source = None

        form = QFormLayout()

        # One combo for every source type: raster layers minus anything that
        # isn't a recognised WMS/WMTS/XYZ or local (GDAL) raster.
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.layer_combo.setAllowEmptyLayer(True)
        self._restrict_to_sources()
        self.layer_combo.layerChanged.connect(self._on_layer_changed)
        form.addRow("Source layer (WMS/WMTS/XYZ/GeoTIFF):", self.layer_combo)

        # Extent selector like QGIS's "Convert Map to Raster" dialog: a dropdown
        # (Calculate from Layer / Use Current Map Canvas Extent / …) plus the
        # extent coordinates. Use the default expanded style, which lays the four
        # coordinates out in separate, clearly-labelled fields. The condensed
        # style packs them onto one comma-separated line that is unreadable in
        # locales using a comma decimal separator.
        self.extent_widget = QgsExtentWidget(None, QgsExtentWidget.ExpandedStyle)
        if self._canvas is not None:
            # drawOnCanvasOption=False hides the "Draw on Canvas" button — it
            # doesn't work usefully from this modal dialog. "Map Canvas Extent"
            # and the other extent options stay available.
            self.extent_widget.setMapCanvas(self._canvas, False)
        self.extent_widget.setOutputCrs(QgsProject.instance().crs())
        self.extent_widget.extentChanged.connect(self._update_estimate)
        self.extent_widget.extentChanged.connect(self._update_zoom_label)

        # Wrap the extent selector in a collapsible group (open by default) so it
        # can be folded away once the extent is set.
        extent_group = QgsCollapsibleGroupBox("Extent to render")
        extent_group.setCollapsed(False)
        extent_layout = QVBoxLayout(extent_group)
        extent_layout.setContentsMargins(6, 6, 6, 6)
        extent_layout.addWidget(self.extent_widget)
        form.addRow(extent_group)

        self.clip_check = QCheckBox("Crop output to the exact extent")
        form.addRow("", self.clip_check)

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

        self.out_widget = OutputDestinationWidget()
        form.addRow("Output:", self.out_widget)

        # Advanced options — created here, placed in a collapsible group below.
        self.conc_spin = QSpinBox()
        self.conc_spin.setRange(1, 16)
        self.conc_spin.setToolTip(
            "Number of tiles fetched in parallel. Lower it (1–2) for strict "
            "servers that reject or throttle many simultaneous connections.")
        self.attempts_spin = QSpinBox()
        self.attempts_spin.setRange(1, 20)
        self.attempts_spin.setToolTip(
            "How many times a tile is retried before it is marked failed.")
        self.min_delay_spin = QDoubleSpinBox()
        self.min_delay_spin.setRange(0.0, 60.0)
        self.min_delay_spin.setDecimals(1)
        self.min_delay_spin.setSingleStep(0.5)
        self.min_delay_spin.setSuffix(" s")
        self.min_delay_spin.setToolTip(
            "Floor on the pace: never send requests closer together than this. "
            "0 = no floor (the adaptive throttle decides). Raise it (e.g. 2 s) to "
            "pin a known-good rate for a strict server.")
        self.backoff_cap_spin = QDoubleSpinBox()
        self.backoff_cap_spin.setRange(1.0, 300.0)
        self.backoff_cap_spin.setDecimals(0)
        self.backoff_cap_spin.setSingleStep(5.0)
        self.backoff_cap_spin.setSuffix(" s")
        self.backoff_cap_spin.setToolTip(
            "Longest the adaptive throttle will wait between requests when a "
            "server is throttling or erroring. Lower = retry sooner (more "
            "aggressive); higher = gentler on strict servers. Default 30 s.")
        self.giveup_spin = QSpinBox()
        self.giveup_spin.setRange(0, 100000)
        self.giveup_spin.setSpecialValueText("Never")     # 0 shows as "Never"
        self.giveup_spin.setToolTip(
            "Stop the run when this many requests in a row fail with no success "
            "(a server refusing a block of tiles), then build a partial mosaic "
            "from what downloaded and leave the rest for a re-run. 0 = never give "
            "up (only the per-tile limit applies). Default 30.")
        # Polite mode: a per-run tile budget and an inter-cell rest, for servers
        # with a daily quota or a burst limit.
        self.max_tiles_spin = QSpinBox()
        self.max_tiles_spin.setRange(0, 10_000_000)
        self.max_tiles_spin.setSpecialValueText("No limit")   # 0 shows as "No limit"
        self.max_tiles_spin.setToolTip(
            "Stop the run after this many tiles are downloaded, then build a "
            "partial mosaic and leave the rest pending. Re-run later to continue "
            "(the resumable cache picks up where it left off). Use this to stay "
            "under a server's daily quota by filling the area over several days. "
            "0 = no limit.")
        self.rest_spin = QSpinBox()
        self.rest_spin.setRange(0, 3600)
        self.rest_spin.setSuffix(" s")
        self.rest_spin.setSpecialValueText("Off")             # 0 shows as "Off"
        self.rest_spin.setToolTip(
            "Pause for this long after finishing each 8×8 macro-cell. Tiles are "
            "fetched cell by cell (like panning a map), so a rest here spreads "
            "the load and can avoid a server's short-term burst limit. 0 = off.")

        advanced = QgsCollapsibleGroupBox("Advanced")
        advanced.setCollapsed(True)
        aform = QFormLayout(advanced)
        aform.addRow("Parallel downloads:", self.conc_spin)
        aform.addRow("Maximum attempts per tile:", self.attempts_spin)
        aform.addRow("Minimum delay between requests:", self.min_delay_spin)
        aform.addRow("Back-off cap:", self.backoff_cap_spin)
        aform.addRow("Give up after (server errors in a row):", self.giveup_spin)
        aform.addRow("Stop after (tiles this run):", self.max_tiles_spin)
        aform.addRow("Rest after each macro-cell:", self.rest_spin)

        # Reset just the Advanced options above to their defaults, right-aligned
        # on its own row inside the group.
        self.reset_adv_btn = QPushButton("Reset to defaults")
        self.reset_adv_btn.setToolTip(
            "Restore the Advanced options above to their default values "
            "(parallel downloads defaults to the source's own preference).")
        self.reset_adv_btn.clicked.connect(self._reset_advanced)
        reset_row = QHBoxLayout()
        reset_row.addStretch(1)
        reset_row.addWidget(self.reset_adv_btn)
        aform.addRow(reset_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(advanced)
        note = QLabel("WMS and local rasters (e.g. GeoTIFF) are read at the chosen "
                      "resolution/CRS; XYZ/WMTS tiles are fetched in their native "
                      "CRS at the chosen zoom and reprojected to the output CRS. "
                      "Changing the parameters or extent starts a fresh run.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(buttons)

        self._restore_state()
        self._on_layer_changed()
        self._update_estimate()
        self._sync_resample()

    # ── filtering / visibility ────────────────────────────────────────────────
    def _restrict_to_sources(self):
        excepted = [lyr for lyr in QgsProject.instance().mapLayers().values()
                    if isinstance(lyr, QgsRasterLayer) and engine.source_for(lyr) is None]
        self.layer_combo.setExceptedLayerList(excepted)

    def _current_source_name(self):
        layer = self.layer_combo.currentLayer()
        src = engine.source_for(layer) if layer else None
        return src.SOURCE_NAME if src else None

    def _set_row_visible(self, label, field, visible):
        label.setVisible(visible); field.setVisible(visible)

    def _on_layer_changed(self, *args):
        name = self._current_source_name()
        # WMS and local rasters (GeoTIFF) use tile-size + resolution.
        is_grid = name in ("WMS", "GeoTIFF")
        is_zoom = name in ("XYZ", "WMTS")       # both address tiles by zoom level
        self._set_row_visible(self.tile_lbl, self.tile_spin, is_grid)
        self._set_row_visible(self.res_lbl,  self.res_spin,  is_grid)
        self._set_row_visible(self.zoom_lbl, self.zoom_spin, is_zoom)
        # Show the note for both zoom sources: XYZ gets a Web-Mercator m/px
        # figure, WMTS a "tile-matrix index" note (its resolution is set by the
        # service, not a fixed grid) — see _update_zoom_label.
        self._set_row_visible(self.zoom_res_lbl, self.zoom_res_info, is_zoom)
        self._clamp_zoom_range(name)
        self._update_zoom_label()

        # On a source-type change, default the output CRS to that source's native.
        if name and name != self._last_source:
            layer = self.layer_combo.currentLayer()
            src = engine.source_for(layer)
            try:
                params = src.extract_params(layer)
                self.crs_widget.setCrs(
                    QgsCoordinateReferenceSystem(src.default_out_crs(params)))
                # Default a local raster's resolution to its native pixel size.
                if name == "GeoTIFF":
                    nres = params.get("native_res")
                    if nres and self.res_spin.minimum() <= nres <= self.res_spin.maximum():
                        self.res_spin.setValue(nres)
            except Exception:
                pass
            self.conc_spin.setValue(getattr(src, "CONCURRENCY", DEFAULT_CONCURRENCY))
        self._last_source = name
        self._update_estimate()

    def _clamp_zoom_range(self, name):
        """Limit the zoom spinner to what the layer serves. XYZ layers advertise
        zmin/zmax in their source; WMTS addresses matrices by index, so fall back
        to the widest range (build_tile_grid clamps to the real matrix count)."""
        lo, hi = 0, 22
        if name == "XYZ":
            layer = self.layer_combo.currentLayer()
            try:
                p = engine.source_for(layer).extract_params(layer)   # no network
                zmin, zmax = int(p.get("zmin", 0)), int(p.get("zmax", 22))
                if zmin <= zmax:
                    lo, hi = zmin, zmax
            except Exception:
                pass
        if (lo, hi) != (self.zoom_spin.minimum(), self.zoom_spin.maximum()):
            self.zoom_spin.setRange(lo, hi)

    def _extent_center_lat(self):
        """Latitude (°) of the extent's centre, or None."""
        bb = self._extent_bbox_in(QgsCoordinateReferenceSystem("EPSG:4326"))
        return None if bb is None else bb.center().y()

    def _update_zoom_label(self, *args):
        z = self.zoom_spin.value()
        if self._current_source_name() == "WMTS":
            # WMTS addresses tile matrices by index, and the true resolution comes
            # from the service's tile matrix set (often not Web Mercator), so a
            # Web-Mercator m/px figure here would be misleading — don't show one.
            self.zoom_res_info.setText(
                "tile-matrix index (resolution set by the service)")
            return
        lat = self._extent_center_lat()
        if lat is None:
            self.zoom_res_info.setText(
                f"≈ {tilemath.tile_resolution_m(z):.3f} m/px at the equator")
        else:
            self.zoom_res_info.setText(
                f"≈ {tilemath.tile_resolution_m_at_lat(z, lat):.3f} m/px "
                f"at the extent (~{lat:.1f}°)")

    # ── tile-count estimate ───────────────────────────────────────────────────
    def _extent_bbox_in(self, target_crs):
        """The chosen extent reprojected to target_crs, or None if not set."""
        if not self.extent_widget.isValid():
            return None
        try:
            ext = self.extent_widget.outputExtent()
            src = self.extent_widget.outputCrs()
            if ext.isEmpty():
                return None
            if src == target_crs:
                return ext
            xform = QgsCoordinateTransform(
                src, target_crs, QgsProject.instance().transformContext())
            return xform.transformBoundingBox(ext)
        except Exception:
            return None

    def _estimate_tiles(self):
        """Upper-bound tile count over the extent bounding box (no polygon
        intersection), or None if it can't be computed yet."""
        layer = self.layer_combo.currentLayer()
        name  = self._current_source_name()
        if layer is None or name is None:
            return None
        try:
            if name == "XYZ":
                bb = self._extent_bbox_in(QgsCoordinateReferenceSystem("EPSG:3857"))
                if bb is None:
                    return None
                xmin, xmax, ymin, ymax = tilemath.tile_range(
                    bb.xMinimum(), bb.yMinimum(), bb.xMaximum(), bb.yMaximum(),
                    self.zoom_spin.value())
                return (xmax - xmin + 1) * (ymax - ymin + 1)
            if name in ("WMS", "GeoTIFF"):
                params = engine.source_for(layer).extract_params(layer)   # no network
                bb = self._extent_bbox_in(QgsCoordinateReferenceSystem(params["crs"]))
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

    # ── advanced options ──────────────────────────────────────────────────────
    def _default_concurrency(self):
        """The default parallel-download count for the current source (a source
        may prefer fewer, e.g. WMS), or the global default if none is selected."""
        layer = self.layer_combo.currentLayer()
        src = engine.source_for(layer) if layer else None
        return getattr(src, "CONCURRENCY", DEFAULT_CONCURRENCY) if src else DEFAULT_CONCURRENCY

    def _reset_advanced(self):
        """Restore the Advanced options to their defaults."""
        self.conc_spin.setValue(self._default_concurrency())
        self.attempts_spin.setValue(DEFAULT_MAX_ATTEMPTS)
        self.min_delay_spin.setValue(DEFAULT_MIN_DELAY)
        self.backoff_cap_spin.setValue(DEFAULT_BACKOFF_CAP)
        self.giveup_spin.setValue(DEFAULT_GIVEUP_AFTER)
        self.max_tiles_spin.setValue(DEFAULT_MAX_TILES)
        self.rest_spin.setValue(DEFAULT_REST_SECS)

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

        # …then restore the remembered output CRS so it wins over the default,
        # and pin _last_source so the final refresh won't clobber it again.
        out_crs = s.value(f"{g}/out_crs", "")
        if out_crs:
            self.crs_widget.setCrs(QgsCoordinateReferenceSystem(out_crs))
        self.conc_spin.setValue(int(s.value(f"{g}/concurrency", DEFAULT_CONCURRENCY)))
        self.attempts_spin.setValue(int(s.value(f"{g}/max_attempts", DEFAULT_MAX_ATTEMPTS)))
        self.min_delay_spin.setValue(float(s.value(f"{g}/min_delay", DEFAULT_MIN_DELAY)))
        self.backoff_cap_spin.setValue(float(s.value(f"{g}/backoff_cap", DEFAULT_BACKOFF_CAP)))
        self.giveup_spin.setValue(int(s.value(f"{g}/giveup_after", DEFAULT_GIVEUP_AFTER)))
        self.max_tiles_spin.setValue(int(s.value(f"{g}/max_tiles", DEFAULT_MAX_TILES)))
        self.rest_spin.setValue(int(s.value(f"{g}/rest_seconds", DEFAULT_REST_SECS)))

        # Restore the last-used extent (overriding the default canvas extent that
        # setMapCanvas seeded). setOutputExtentFromUser fills the N/S/E/W fields.
        ext_str = s.value(f"{g}/extent", "")
        ext_crs = s.value(f"{g}/extent_crs", "")
        if ext_str and ext_crs:
            try:
                xmin, ymin, xmax, ymax = (float(v) for v in ext_str.split(","))
                crs = QgsCoordinateReferenceSystem(ext_crs)
                if crs.isValid():
                    self.extent_widget.setOutputExtentFromUser(
                        QgsRectangle(xmin, ymin, xmax, ymax), crs)
            except (ValueError, TypeError):
                pass

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
        s.setValue(f"{g}/concurrency", self.conc_spin.value())
        s.setValue(f"{g}/max_attempts", self.attempts_spin.value())
        s.setValue(f"{g}/min_delay", self.min_delay_spin.value())
        s.setValue(f"{g}/backoff_cap", self.backoff_cap_spin.value())
        s.setValue(f"{g}/giveup_after", self.giveup_spin.value())
        s.setValue(f"{g}/max_tiles", self.max_tiles_spin.value())
        s.setValue(f"{g}/rest_seconds", self.rest_spin.value())
        ly = self.layer_combo.currentLayer()
        s.setValue(f"{g}/layer_id", ly.id() if ly else "")

        # Remember the extent (in its own CRS) so it is restored next time.
        if self.extent_widget.isValid():
            ext = self.extent_widget.outputExtent()
            crs = self.extent_widget.outputCrs()
            if not ext.isEmpty() and crs.isValid():
                s.setValue(f"{g}/extent", "{},{},{},{}".format(
                    ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()))
                s.setValue(f"{g}/extent_crs", crs.authid() or crs.toWkt())

    def accept(self):
        n = self._estimate_tiles()
        name = self._current_source_name()
        # WMTS tile counts can't be estimated without fetching capabilities, so
        # its estimate is always None; still surface the ToS reminder there.
        large = bool(n and n > WARN_TILE_COUNT)
        unbounded_wmts = (name == "WMTS" and n is None)
        if large or unbounded_wmts:
            count_line = (
                f"This will download roughly {n:,} tiles, which may be slow and "
                f"put load on the server.\n\n" if large else
                "The tile count can't be estimated in advance for WMTS, but a fine "
                "zoom level over a large extent can be a very large download.\n\n")
            reply = QMessageBox.question(
                self, "Large download",
                count_line +
                "Bulk-downloading tiles may violate the provider's Terms of "
                "Service (e.g. Google, Bing, Esri). Make sure your intended use "
                "is permitted before continuing.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return          # keep the dialog open
        self._save_state()
        super().accept()

    # ── result ────────────────────────────────────────────────────────────────
    def values(self):
        layer = self.layer_combo.currentLayer()
        name  = self._current_source_name()
        if name in ("WMS", "GeoTIFF"):
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
        concurrency = self.conc_spin.value()
        max_attempts = self.attempts_spin.value()
        min_delay = self.min_delay_spin.value()
        backoff_cap = self.backoff_cap_spin.value()
        giveup_after = self.giveup_spin.value()
        max_tiles = self.max_tiles_spin.value()
        rest_seconds = self.rest_spin.value()
        valid = self.extent_widget.isValid()
        extent = self.extent_widget.outputExtent() if valid else None
        extent_crs = self.extent_widget.outputCrs().authid() if valid else None
        return (layer, extent, extent_crs, opts, out_crs, out_path, temporary,
                resample, clip, concurrency, max_attempts, min_delay,
                backoff_cap, giveup_after, max_tiles, rest_seconds)

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
import subprocess  # nosec B404
import textwrap

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import (
    QDialog, QFormLayout, QVBoxLayout, QHBoxLayout, QDialogButtonBox,
    QSpinBox, QDoubleSpinBox, QLabel, QWidget, QLineEdit, QToolButton,
    QMenu, QFileDialog, QMessageBox, QComboBox, QCheckBox, QPushButton,
)
from qgis.core import (
    QgsProject, QgsMapLayerProxyModel, QgsRasterLayer, QgsVectorLayer, QgsSettings,
    QgsRectangle, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
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
DEFAULT_CACHE_BUST   = False   # off: don't add the per-retry WMS cache-buster
DEFAULT_HARMONIZE    = False   # off: don't harmonise ArcGIS flight years
DEFAULT_HARMONIZE_MATCH = 0    # % brightness/contrast match on top of the colour match
# Sourced from the engine so the dialog's defaults can't drift from the real ones.
DEFAULT_BACKOFF_CAP  = engine.MAX_DELAY_SEC                # s; adaptive back-off ceiling
DEFAULT_GIVEUP_AFTER = engine.MAX_CONSECUTIVE_BACKPRESSURE  # consecutive fails → give up

# Ask for confirmation above this estimated tile count.
WARN_TILE_COUNT = 5000

# Qt renders a plain-text tooltip WITHOUT word wrap, so an unwrapped sentence
# becomes one very wide line running across the screen. Tooltips are therefore
# written as ordinary prose below and wrapped to this column in one pass (see
# _wrap_tooltips).
TOOLTIP_WIDTH = 40


def _wrap_tip(text):
    """`text` wrapped to TOOLTIP_WIDTH. Each explicit line is wrapped on its
    own, so the deliberate breaks between an "On:" / "Off:" clause survive;
    continuation lines are indented so those clauses still stand out once
    everything is this narrow. Rich text is left alone — Qt wraps that itself."""
    if text.lstrip().startswith("<"):
        return text
    return "\n".join(
        textwrap.fill(line, TOOLTIP_WIDTH, subsequent_indent="  ",
                      # Never split a long token: a path or URL mangled across
                      # two lines is far worse than one over-wide line.
                      break_long_words=False, break_on_hyphens=False)
        if line.strip() else line
        for line in text.splitlines())


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
        # Fixed argument list, no shell, no user input; "git" is intentionally
        # resolved via PATH (its location varies by platform/install). Dev-only:
        # an installed plugin has no .git, so this simply returns "".
        out = subprocess.run(  # nosec B603 B607
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
        self.btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
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
        self._src_cache = (None, None)   # (layer, SOURCE_NAME) — see _current_source_name

        form = QFormLayout()

        # One combo for every source type: raster layers minus anything that
        # isn't a recognised WMS/WMTS/XYZ or local (GDAL) raster.
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.Filter.RasterLayer)
        self.layer_combo.setAllowEmptyLayer(True)
        self.layer_combo.setToolTip(
            "The layer to download or export. The source type (WMS, WMTS, XYZ, "
            "ArcGIS REST, or a local GDAL raster such as a GeoTIFF) is detected "
            "automatically, and the dialog shows the fields that apply to it.\n"
            "Add the layer to the project first (Layer ▸ Data Source Manager); "
            "unrecognised raster layers are not listed.")
        self._restrict_to_sources()
        self.layer_combo.layerChanged.connect(self._on_layer_changed)
        form.addRow("Source layer (WMS/WMTS/XYZ/GeoTIFF):", self.layer_combo)

        # Extent selector like QGIS's "Convert Map to Raster" dialog: a dropdown
        # (Calculate from Layer / Use Current Map Canvas Extent / …) plus the
        # extent coordinates. Use the default expanded style, which lays the four
        # coordinates out in separate, clearly-labelled fields. The condensed
        # style packs them onto one comma-separated line that is unreadable in
        # locales using a comma decimal separator.
        self.extent_widget = QgsExtentWidget(None, QgsExtentWidget.WidgetStyle.ExpandedStyle)
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
        self.extent_widget.setToolTip(
            "The area to download/export, like QGIS's Convert Map to Raster "
            "dialog: pick 'Calculate from Layer' (a layer's bounding box), "
            "'Map Canvas Extent' (the current view), or type the coordinates.\n"
            "The extent may be in any CRS — it is reprojected to whatever the "
            "source needs.")
        extent_layout = QVBoxLayout(extent_group)
        extent_layout.setContentsMargins(6, 6, 6, 6)
        extent_layout.addWidget(self.extent_widget)
        # "Crop to the exact extent" lives with the extent it applies to.
        self.clip_check = QCheckBox("Crop output to the exact extent")
        self.clip_check.setToolTip(
            "The mosaic is assembled from whole tiles, so it normally extends a "
            "little past the requested extent.\n"
            "On: trim the output to the exact extent rectangle.\n"
            "Off: keep the full tile-aligned coverage.")
        extent_layout.addWidget(self.clip_check)
        form.addRow(extent_group)

        # Tile size + resolution (WMS and local rasters), in a collapsible group
        # that is open by default. Greyed out for GeoTIFF, which is exported at
        # its own native resolution.
        self.tile_spin = QSpinBox(); self.tile_spin.setRange(256, 8192)
        self.tile_spin.setSingleStep(256)
        self.tile_spin.setToolTip(
            "Width/height (in pixels) of each request sent to the server. "
            "Larger tiles mean fewer requests; smaller tiles are gentler on "
            "servers that reject big renders. 1024 suits most services.")
        self.tile_spin.valueChanged.connect(self._update_estimate)
        self.res_spin = QDoubleSpinBox(); self.res_spin.setDecimals(3)
        self.res_spin.setRange(0.001, 1000.0); self.res_spin.setSingleStep(0.1)
        self.res_spin.setToolTip(
            "Ground size of one output pixel, in the request CRS's units "
            "(e.g. 0.5 = 0.5 m/px for a metric CRS). Finer values add real "
            "detail only up to what the provider actually serves.\n"
            "Greyed out for a local raster, which is exported at its own "
            "native resolution.")
        self.res_spin.valueChanged.connect(self._update_estimate)
        # The live bounding-box tile-count estimate lives with the size controls.
        self.estimate_lbl = QLabel("")
        self.estimate_lbl.setToolTip(
            "Rough tile count over the extent's bounding box at the current "
            "settings — an upper bound, updated live. Above about "
            f"{WARN_TILE_COUNT:,} tiles the dialog asks for confirmation.")
        self.grid_group = QgsCollapsibleGroupBox("Tile size && resolution")
        self.grid_group.setCollapsed(False)
        gform = QFormLayout(self.grid_group)
        gform.addRow("Tile size (px):", self.tile_spin)
        gform.addRow("Resolution (units/px):", self.res_spin)
        gform.addRow("", self.estimate_lbl)
        form.addRow(self.grid_group)

        # XYZ-only rows ------------------------------------------------------
        self.zoom_lbl  = QLabel("Zoom level:")
        self.zoom_spin = QSpinBox(); self.zoom_spin.setRange(0, 22)
        self.zoom_spin.setToolTip(
            "Level of detail to download. XYZ: the {z} of the tile scheme "
            "(each step doubles the resolution — and quadruples the tile "
            "count); the range is clamped to what the layer advertises.\n"
            "WMTS: the index into the service's tile-matrix set (its true "
            "resolution is set by the service).")
        self.zoom_spin.valueChanged.connect(self._update_zoom_label)
        self.zoom_spin.valueChanged.connect(self._update_estimate)
        form.addRow(self.zoom_lbl, self.zoom_spin)

        self.zoom_res_lbl  = QLabel("")
        self.zoom_res_info = QLabel("")
        self.zoom_res_info.setToolTip(
            "The approximate ground resolution the chosen zoom gives at the "
            "extent's latitude (Web-Mercator pixels shrink toward the poles). "
            "Pick the coarsest zoom that still shows the detail you need.")
        form.addRow(self.zoom_res_lbl, self.zoom_res_info)

        # Tile-count estimate for the zoom sources (XYZ/WMTS). The grid sources'
        # estimate lives in the Tile size & resolution group, which is hidden here.
        self.zoom_est_lbl  = QLabel("")
        self.zoom_estimate_info = QLabel("")
        self.zoom_estimate_info.setToolTip(
            "Rough tile count over the extent's bounding box at the current "
            "zoom — an upper bound, updated live. Above about "
            f"{WARN_TILE_COUNT:,} tiles the dialog asks for confirmation. "
            "(No estimate is possible for WMTS before its capabilities are "
            "fetched.)")
        form.addRow(self.zoom_est_lbl, self.zoom_estimate_info)

        # Output settings (CRS, resampling, destination) in a collapsible group
        # that is open by default.
        self.crs_widget = QgsProjectionSelectionWidget()
        self.crs_widget.setToolTip(
            "CRS of the output GeoTIFF. Tiles are fetched in the source's own "
            "CRS and reprojected to this one while building the mosaic. "
            "Defaults to the source's native CRS when you pick a layer; "
            "disabled when Reproject sampling is 'None'.")
        self.resample_combo = QComboBox()
        self.resample_combo.addItem("Bilinear", "bilinear")
        self.resample_combo.addItem("Nearest neighbour", "near")
        self.resample_combo.addItem("Cubic", "cubic")
        self.resample_combo.addItem("None (keep native CRS, no reprojection)", "none")
        self.resample_combo.setToolTip(
            "How pixels are resampled when reprojecting to the output CRS.\n"
            "Bilinear: smooth, good default for imagery.\n"
            "Nearest neighbour: keeps exact values — use for categorical data.\n"
            "Cubic: smoothest, slightly sharper than bilinear.\n"
            "None: skip reprojection entirely — the mosaic stays in the "
            "source's native CRS, pixel-for-pixel.")
        self.resample_combo.currentIndexChanged.connect(self._sync_resample)
        self.out_widget = OutputDestinationWidget()
        self.out_widget.setToolTip(
            "Where to save the GeoTIFF. Leave empty for a temporary file. "
            "The finished mosaic is added to the project either way.\n"
            "Re-running with the same output file resumes an interrupted "
            "download instead of starting over.")
        # Off by default: normally the mosaic is built only once every tile is in
        # (an interrupted run resumes on re-run). Tick this to stitch whatever
        # downloaded now, leaving the missing tiles as gaps.
        self.partial_check = QCheckBox("Build mosaic even if some tiles are missing")
        self.partial_check.setToolTip(
            "Off (default): an interrupted or partly-failed run produces no "
            "output; progress is saved and re-running finishes it.\n"
            "On: build the mosaic from whatever downloaded, leaving the missing "
            "tiles as transparent gaps. A later re-run (with this off) still "
            "fills them and rebuilds without gaps.")
        # Processing (ArcGIS sources only): optional post-fetch steps. Collapsed by
        # default, and sits just above Output.
        self.harmonize_check = QCheckBox("Harmonise flight years (seamless colour)")
        self.harmonize_check.setToolTip(
            "ArcGIS orthophoto services often serve a mosaic of different survey "
            "years, leaving a visible colour seam where two years meet.\n"
            "On: download each year separately and colour-match the older years "
            "to the newest along their shared boundary, then composite — removing "
            "the seam while keeping each year's own colours (no global muting).\n"
            "Only applies to ArcGIS sources whose layers are per-year orthophotos.")
        # How strongly to also equalise brightness/contrast between years, on top
        # of the seam colour match. 0 = colour only (default); enabled only when
        # harmonise is on.
        self.harmonize_match_spin = QSpinBox()
        self.harmonize_match_spin.setRange(0, 100)
        self.harmonize_match_spin.setSuffix(" %")
        self.harmonize_match_spin.setToolTip(
            "How strongly to also match brightness and contrast between flight "
            "years, on top of the colour match at the seam.\n"
            "0% (default): match colour only — each year keeps its own brightness "
            "and contrast (richest result; the years may still differ in "
            "brightness/contrast away from the seam).\n"
            "Higher: pull the years' overall brightness/contrast together too — "
            "more uniform, but high values mute the image and can slightly "
            "re-expose the seam. Try 30–50%.")
        self.harmonize_match_spin.setEnabled(False)
        self.harmonize_check.toggled.connect(self.harmonize_match_spin.setEnabled)
        # Harmonise fetches each flight year separately, so it multiplies the
        # tile-count estimate — keep the estimate label in sync with the toggle.
        self.harmonize_check.toggled.connect(self._update_estimate)
        self.processing_group = QgsCollapsibleGroupBox("Processing")
        self.processing_group.setCollapsed(True)
        pform = QFormLayout(self.processing_group)
        pform.addRow("", self.harmonize_check)
        pform.addRow("Match brightness/contrast:", self.harmonize_match_spin)
        form.addRow(self.processing_group)

        self.output_group = QgsCollapsibleGroupBox("Output")
        self.output_group.setCollapsed(False)
        oform = QFormLayout(self.output_group)
        oform.addRow("Reproject sampling:", self.resample_combo)
        oform.addRow("Output CRS:", self.crs_widget)
        oform.addRow("Output:", self.out_widget)
        oform.addRow("", self.partial_check)
        form.addRow(self.output_group)

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
        # Off by default: only WMS uses it, and it forgoes any server-side cache
        # on retries (more load), so it's opt-in for the case it fixes.
        self.cache_bust_check = QCheckBox("Bypass cached server errors on retry")
        self.cache_bust_check.setToolTip(
            "For WMS servers behind a cache/CDN. A WMS ServiceException (e.g. the "
            "server briefly can't read its own data) comes back as a normal "
            "'200 OK', so a cache may store that error and replay it for every "
            "identical retry — the request never recovers.\n"
            "On: each retry adds a throwaway parameter so the request differs and "
            "the server actually re-renders instead of replaying the cached "
            "failure. The first attempt is unchanged (a good cached tile is still "
            "reused). No effect on XYZ/WMTS/local rasters.")

        self.advanced_group = advanced = QgsCollapsibleGroupBox("Advanced")
        advanced.setCollapsed(True)
        aform = QFormLayout(advanced)
        aform.addRow("Parallel downloads:", self.conc_spin)
        aform.addRow("Maximum attempts per tile:", self.attempts_spin)
        aform.addRow("Minimum delay between requests:", self.min_delay_spin)
        aform.addRow("Back-off cap:", self.backoff_cap_spin)
        aform.addRow("Give up after (server errors in a row):", self.giveup_spin)
        aform.addRow("", self.cache_bust_check)

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

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
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

        # Download-cache usage + a way to reclaim the space. Measured in the
        # background (see _start_cache_scan) because a big cache holds tens of
        # thousands of tile files.
        self.cache_lbl = QLabel("Download cache: measuring…")
        self.cache_lbl.setToolTip(
            "Disk space used by the download cache beside your project "
            "(__btdcache__): the per-export queues that let an interrupted run "
            "resume, plus the shared tile store that overlapping areas reuse.")
        self.cache_btn = QPushButton("Clear cache…")
        self.cache_btn.setToolTip(
            "Delete the cached tiles and queues to reclaim disk space. "
            "Finished exports are unaffected — but any interrupted download "
            "loses its progress, and shared tiles have to be fetched again "
            "(which counts against the provider's quota).")
        self.cache_btn.clicked.connect(self._purge_cache)
        cache_row = QHBoxLayout()
        cache_row.addWidget(self.cache_lbl, 1)
        cache_row.addWidget(self.cache_btn)
        layout.addLayout(cache_row)

        layout.addWidget(buttons)

        self._wrap_tooltips()
        self._restore_state()
        self._on_layer_changed()
        self._update_estimate()
        self._sync_resample()
        self._start_cache_scan()

    def _wrap_tooltips(self):
        """Wrap the tooltips set above to a readable column width. Runs over
        every widget this dialog owns, so a tooltip added later is wrapped too
        without having to remember. Widgets we merely borrow (the map canvas)
        keep QGIS's own tooltips untouched."""
        for w in vars(self).values():
            if isinstance(w, QWidget) and w is not self._canvas and w.toolTip():
                w.setToolTip(_wrap_tip(w.toolTip()))

    # ── download cache (usage + purge) ────────────────────────────────────────
    def _start_cache_scan(self):
        """Measure the cache without freezing the dialog. A big cache holds tens
        of thousands of tile files, so the walk is a generator stepped from a
        zero-delay timer: the event loop keeps running between chunks, and the
        label updates as the total grows. The timer is parented to the dialog, so
        closing the dialog stops the scan."""
        self._cache_usage = None
        self._cache_scan = engine.iter_cache_usage(engine.cache_root())
        timer = QTimer(self)
        timer.setInterval(0)
        timer.timeout.connect(self._cache_scan_step)
        self._cache_timer = timer
        timer.start()

    def _cache_scan_step(self):
        try:
            usage = next(self._cache_scan)
        except StopIteration:
            self._cache_timer.stop()
            return
        except Exception:       # a cache being written to underneath us
            self._cache_timer.stop()
            self.cache_lbl.setText("Download cache: size unavailable")
            return
        self._cache_usage = usage
        self._show_cache_usage(usage)
        if usage.get("done"):
            self._cache_timer.stop()

    @staticmethod
    def _cache_breakdown(usage, limit=6):
        """Largest folders in the cache, biggest first — the useful part when the
        total is surprising, since it names what is actually taking the space
        (including any folder left behind by an older version or a rename)."""
        rows = list((usage.get("jobs") or {}).items())
        if usage.get("shared"):
            rows.append((engine.SHARED_DIR_NAME + " (tiles reused between areas)",
                         usage["shared"]))
        rows.sort(key=lambda kv: -kv[1])
        return [f"{engine.format_size(size)}  —  {name}" for name, size in rows[:limit]]

    def _show_cache_usage(self, usage):
        total = usage.get("total", 0)
        if usage.get("done") and not total:
            self.cache_lbl.setText("Download cache: empty")
            self.cache_btn.setEnabled(False)
            return
        self.cache_btn.setEnabled(True)
        n_jobs = len(usage.get("jobs") or {})
        shared = usage.get("shared", 0)
        parts = []
        if n_jobs:
            parts.append(f"{n_jobs} export{'s' if n_jobs != 1 else ''}")
        if shared:
            parts.append(f"shared tiles {engine.format_size(shared)}")
        detail = f" ({', '.join(parts)})" if parts else ""
        suffix = "" if usage.get("done") else " so far…"
        self.cache_lbl.setText(
            f"Download cache: {engine.format_size(total)}{detail}{suffix}")
        if usage.get("done"):
            rows = self._cache_breakdown(usage)
            # Only the prose is wrapped; the path and the size rows are left as
            # single lines so a path stays intact and the sizes stay aligned.
            self.cache_lbl.setToolTip(
                _wrap_tip("Disk space used by the download cache beside your "
                          "project: the per-export queues that let an "
                          "interrupted run resume, plus the shared tile store "
                          "that overlapping areas reuse.")
                + f"\n\n{usage.get('files', 0):,} files in\n{usage.get('root', '')}"
                + ("\n\nLargest folders:\n  " + "\n  ".join(rows) if rows else ""))

    def _purge_cache(self):
        # Never delete a cache out from under a running job.
        if engine.active_task() is not None:
            QMessageBox.warning(
                self, "Download in progress",
                "A download is running and is writing to the cache. Cancel it in "
                "the Task Manager before clearing the cache.")
            return

        root = engine.cache_root()
        usage = self._cache_usage if (self._cache_usage or {}).get("done") else \
            engine.cache_usage(root)
        total, n_jobs = usage.get("total", 0), len(usage.get("jobs") or {})
        if not total:
            QMessageBox.information(self, "Cache empty",
                                    f"There is nothing cached in:\n{root}")
            return

        shared = usage.get("shared", 0)
        rows = self._cache_breakdown(usage)
        breakdown = ("\n\nLargest folders:\n  " + "\n  ".join(rows)) if rows else ""
        if QMessageBox.question(
                self, "Clear download cache?",
                f"Delete the whole download cache?\n\n{root}\n\n"
                f"{engine.format_size(total)} in {usage.get('files', 0):,} files "
                f"— {n_jobs} export queue(s) and "
                f"{engine.format_size(shared)} of shared tiles."
                f"{breakdown}\n\n"
                "GeoTIFFs you have already produced are NOT affected. But any "
                "interrupted download loses its progress and starts over, and "
                "tiles that overlapping areas were reusing must be downloaded "
                "again — which counts against the provider's quota.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return

        # Stop the background scan first: it would otherwise be walking a tree
        # that is being deleted underneath it.
        if getattr(self, "_cache_timer", None) is not None:
            self._cache_timer.stop()
        freed, errors = engine.purge_cache(root)
        if errors:
            QMessageBox.warning(
                self, "Cache partly cleared",
                f"Freed {engine.format_size(freed)}, but {len(errors)} item(s) "
                "could not be deleted — they may be open in another QGIS "
                "instance:\n\n" + "\n".join(errors[:5]))
        else:
            QMessageBox.information(
                self, "Cache cleared",
                f"Freed {engine.format_size(freed)}.")
        self._start_cache_scan()

    # ── filtering / visibility ────────────────────────────────────────────────
    def _restrict_to_sources(self):
        excepted = [lyr for lyr in QgsProject.instance().mapLayers().values()
                    if isinstance(lyr, QgsRasterLayer) and engine.source_for(lyr) is None]
        self.layer_combo.setExceptedLayerList(excepted)

    def _current_source_name(self):
        # Memoised: source_for() runs every backend's detect(), and this is called
        # several times per interaction — recompute only when the layer changes.
        layer = self.layer_combo.currentLayer()
        if layer is not self._src_cache[0]:
            src = engine.source_for(layer) if layer else None
            self._src_cache = (layer, src.SOURCE_NAME if src else None)
        return self._src_cache[1]

    def _set_row_visible(self, label, field, visible):
        label.setVisible(visible); field.setVisible(visible)

    def _on_layer_changed(self, *args):
        name = self._current_source_name()
        # WMS and local rasters (GeoTIFF) use tile-size + resolution; the group is
        # hidden for the zoom sources and greyed out for GeoTIFF (native res).
        # WMS/ArcGIS and local rasters (GeoTIFF) use tile-size + resolution; the
        # group is hidden for the zoom sources and greyed out for GeoTIFF (native
        # res). ArcGIS exports over a bbox at a chosen resolution, like WMS.
        is_grid = name in ("WMS", "GeoTIFF", "ArcGIS")
        is_zoom = name in ("XYZ", "WMTS")       # both address tiles by zoom level
        self.grid_group.setVisible(is_grid)
        self.grid_group.setEnabled(name in ("WMS", "ArcGIS"))
        # GeoTIFF uses its native resolution, so fold the group away (and grey it).
        self.grid_group.setCollapsed(name == "GeoTIFF")
        # The Advanced options are all about network downloading, which a local
        # raster doesn't do (it's a windowed read), so grey them out for GeoTIFF.
        self.advanced_group.setEnabled(name != "GeoTIFF")
        # Processing (harmonise flight years) is ArcGIS-only.
        self.processing_group.setVisible(name == "ArcGIS")
        self._set_row_visible(self.zoom_lbl, self.zoom_spin, is_zoom)
        # Show the note for both zoom sources: XYZ gets a Web-Mercator m/px
        # figure, WMTS a "tile-matrix index" note (its resolution is set by the
        # service, not a fixed grid) — see _update_zoom_label.
        self._set_row_visible(self.zoom_res_lbl, self.zoom_res_info, is_zoom)
        self._set_row_visible(self.zoom_est_lbl, self.zoom_estimate_info, is_zoom)
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
            except Exception:  # nosec B110
                pass
            self.conc_spin.setValue(getattr(src, "CONCURRENCY", DEFAULT_CONCURRENCY))
        self._last_source = name
        self._update_estimate()

    def _clamp_zoom_range(self, name):
        """Limit the zoom spinner to what the layer serves. XYZ layers advertise
        zmin/zmax in their source; WMTS addresses matrices by index, so fall back
        to the widest range (build_tile_grid clamps to the real matrix count)."""
        lo, hi = 0, 22
        if name == "WMTS":
            # WMTS "zoom" is a tile-matrix INDEX; some services publish more
            # than 22 matrices, so allow a wider range (build_tile_grid clamps
            # to the real matrix count).
            hi = 30
        elif name == "XYZ":
            layer = self.layer_combo.currentLayer()
            try:
                p = engine.source_for(layer).extract_params(layer)   # no network
                zmin, zmax = int(p.get("zmin", 0)), int(p.get("zmax", 22))
                if zmin <= zmax:
                    lo, hi = zmin, zmax
            except Exception:  # nosec B110
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
            if name in ("WMS", "GeoTIFF", "ArcGIS"):
                # ArcGIS tiles the extent the same origin-anchored way as WMS.
                params = engine.source_for(layer).extract_params(layer)   # no network
                bb = self._extent_bbox_in(QgsCoordinateReferenceSystem(params["crs"]))
                if bb is None:
                    return None
                # GeoTIFF is exported at its native resolution (the field is
                # greyed), so estimate with that, not the spinbox value.
                res = (params.get("native_res") if name == "GeoTIFF"
                       else self.res_spin.value()) or self.res_spin.value()
                step = self.tile_spin.value() * res
                if step <= 0:
                    return None
                return (max(1, math.ceil(bb.width() / step)) *
                        max(1, math.ceil(bb.height() / step)))
        except Exception:
            return None
        return None

    def _harmonising(self):
        """True when the current run would fetch each ArcGIS flight year
        separately — multiplying the tile count by the number of years."""
        return (self._current_source_name() == "ArcGIS"
                and self.harmonize_check.isChecked())

    def _update_estimate(self, *args):
        n = self._estimate_tiles()
        if n is None:
            text = ""
        elif self._harmonising():
            # Harmonise downloads the grid once per flight year (the year count
            # is only known after contacting the service, so it can't be shown).
            text = f"≈ {n:,} tiles per flight year (bounding-box estimate)"
        else:
            text = f"≈ {n:,} tiles (bounding-box estimate)"
        # Both labels carry the text; visibility (grid group vs zoom rows) decides
        # which one the user actually sees for the current source.
        self.estimate_lbl.setText(text)
        self.zoom_estimate_info.setText(text)

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
        self.cache_bust_check.setChecked(DEFAULT_CACHE_BUST)

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
        self.partial_check.setChecked(s.value(f"{g}/partial_mosaic", False, type=bool))

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
        self.cache_bust_check.setChecked(
            s.value(f"{g}/cache_bust", DEFAULT_CACHE_BUST, type=bool))
        self.harmonize_check.setChecked(
            s.value(f"{g}/harmonize", DEFAULT_HARMONIZE, type=bool))
        self.harmonize_match_spin.setValue(
            int(s.value(f"{g}/harmonize_match", DEFAULT_HARMONIZE_MATCH)))
        self.harmonize_match_spin.setEnabled(self.harmonize_check.isChecked())

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
        s.setValue(f"{g}/partial_mosaic", self.partial_check.isChecked())
        s.setValue(f"{g}/concurrency", self.conc_spin.value())
        s.setValue(f"{g}/max_attempts", self.attempts_spin.value())
        s.setValue(f"{g}/min_delay", self.min_delay_spin.value())
        s.setValue(f"{g}/backoff_cap", self.backoff_cap_spin.value())
        s.setValue(f"{g}/giveup_after", self.giveup_spin.value())
        s.setValue(f"{g}/cache_bust", self.cache_bust_check.isChecked())
        s.setValue(f"{g}/harmonize", self.harmonize_check.isChecked())
        s.setValue(f"{g}/harmonize_match", self.harmonize_match_spin.value())
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

    def _would_resume(self):
        """True if a re-run with the current settings would resume an existing
        job (matching cache) rather than start fresh — used to skip the
        overwrite-output prompt."""
        v = self.values()
        return engine.has_resumable_cache(v["layer"], v["extent"], v["extent_crs"],
                                          v["opts"], v["output_path"], v["temporary"])

    def _multi_feature_extent_warning(self):
        """When the extent was taken from a layer (Calculate from Layer), the
        extent is the bounding box of *all* its features — so a stray or unwanted
        feature silently enlarges it (e.g. a vertex near 0,0 in EPSG:3857 pulls
        the South down to 0). Return a warning if that layer has more than one
        feature, else None."""
        try:
            if self.extent_widget.extentState() != \
                    QgsExtentWidget.ExtentState.ProjectLayerExtent:
                return None
            name = self.extent_widget.extentLayerName()
            for lyr in (QgsProject.instance().mapLayersByName(name) if name else []):
                if isinstance(lyr, QgsVectorLayer):
                    n = lyr.featureCount()
                    if n and n > 1:
                        return (f"The extent comes from layer “{name}”, which has "
                                f"{n} features. The extent is the bounding box of "
                                f"ALL of them, so a stray or unwanted feature can "
                                f"enlarge it unexpectedly (e.g. a vertex near 0,0 "
                                f"drops the South to 0). Use this extent anyway?")
        except Exception:  # nosec B110
            pass
        return None

    def accept(self):
        # Resuming an existing job just continues it, so skip both the
        # large-download/ToS reminder and the overwrite-output prompt.
        resuming = self._would_resume()

        # The extent came from a multi-feature layer? Its bbox spans all features,
        # so warn before an unwanted feature silently blows up the download area.
        aoi_warn = None if resuming else self._multi_feature_extent_warning()
        if aoi_warn and QMessageBox.question(
                self, "Extent layer has multiple features", aoi_warn,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return          # keep the dialog open

        n = self._estimate_tiles()
        name = self._current_source_name()
        # WMTS tile counts can't be estimated without fetching capabilities, so
        # its estimate is always None; still surface the ToS reminder there.
        harmonising = self._harmonising()
        # Harmonise fetches the grid once per flight year (at least 2 when it
        # applies), so gauge "large" on that lower bound, not the per-year count.
        effective = n * 2 if (n and harmonising) else n
        large = bool(effective and effective > WARN_TILE_COUNT)
        unbounded_wmts = (name == "WMTS" and n is None)
        if (large or unbounded_wmts) and not resuming:
            if large and harmonising:
                count_line = (
                    f"This will download roughly {n:,} tiles per flight year "
                    "(harmonise fetches each year separately), which may be slow "
                    "and put load on the server.\n\n")
            elif large:
                count_line = (
                    f"This will download roughly {n:,} tiles, which may be slow "
                    "and put load on the server.\n\n")
            else:
                count_line = (
                    "The tile count can't be estimated in advance for WMTS, but "
                    "a fine zoom level over a large extent can be a very large "
                    "download.\n\n")
            reply = QMessageBox.question(
                self, "Large download",
                count_line +
                "Bulk-downloading tiles may violate the provider's Terms of "
                "Service (e.g. Google, Bing, Esri). Make sure your intended use "
                "is permitted before continuing.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return          # keep the dialog open

        # Confirm before overwriting an existing output file (temporary output
        # has no path, so it never prompts). Skip the prompt when a re-run would
        # just resume this job — the existing file is our own partial output.
        out_path = self.out_widget.file_path()
        if out_path and os.path.exists(out_path) and not resuming:
            reply = QMessageBox.question(
                self, "Overwrite file?",
                f"The output file already exists:\n{out_path}\n\nOverwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return          # keep the dialog open

        self._save_state()
        super().accept()

    # ── result ────────────────────────────────────────────────────────────────
    def values(self):
        """The dialog's settings as a dict whose keys match engine.run()'s
        keyword arguments, so the caller can simply engine.run(**values)."""
        layer = self.layer_combo.currentLayer()
        name  = self._current_source_name()
        if name in ("WMS", "GeoTIFF", "ArcGIS"):
            opts = {"tile_pixels": self.tile_spin.value(),
                    "resolution":  self.res_spin.value()}
            if name == "ArcGIS":
                opts["harmonize"] = self.harmonize_check.isChecked()
                opts["harmonize_match"] = self.harmonize_match_spin.value() / 100.0
        elif name in ("XYZ", "WMTS"):
            opts = {"zoom": self.zoom_spin.value()}
        else:
            opts = {}
        crs = self.crs_widget.crs()
        valid = self.extent_widget.isValid()
        return {
            "layer":        layer,
            "extent":       self.extent_widget.outputExtent() if valid else None,
            "extent_crs":   (self.extent_widget.outputCrs().authid()
                             if valid else None),
            "opts":         opts,
            "out_crs":      crs.authid() if crs.isValid() else None,
            "output_path":  self.out_widget.file_path(),
            "temporary":    self.out_widget.is_temporary(),
            "resample":     self.resample_combo.currentData(),
            "clip":         self.clip_check.isChecked(),
            "concurrency":  self.conc_spin.value(),
            "max_attempts": self.attempts_spin.value(),
            "min_delay":    self.min_delay_spin.value(),
            "backoff_cap":  self.backoff_cap_spin.value(),
            "giveup_after": self.giveup_spin.value(),
            "partial_ok":   self.partial_check.isChecked(),
            "cache_bust":   self.cache_bust_check.isChecked(),
        }

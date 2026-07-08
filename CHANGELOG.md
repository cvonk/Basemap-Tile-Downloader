# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

## [1.4.22] - 2026-07-08
### Changed
- Fully-transparent tiles are now kept and mosaicked like any other tile — WMS no
  longer drops them — so a WMS export covers the whole grid over the extent.
- The tile cache stores file paths **relative** to the job folder, so an
  `__btdcache__` job can be moved, restored, or backed up to a different
  folder/drive/machine and still resume. Existing caches with absolute paths keep
  working (paths are resolved either way).
- A local raster (GeoTIFF) is exported at its true **native resolution**
  regardless of the (greyed) resolution field.
### Fixed
- Resuming a job no longer re-requests known-empty tiles (legitimate 404/204
  gaps) — previously each resume wasted a request, and a daily-quota tile, on
  every gap.
- Stopping on the per-run tile budget (or cancel / circuit breaker) now records
  the fetches already in flight instead of discarding them (the pool waits for
  them regardless), so they aren't re-fetched next run.
- The inter-cell rest now fires only on forward progress into a new macro-cell; a
  retried tile from an earlier cell no longer triggers a spurious pause.

## [1.4.21] - 2026-07-07
### Changed
- A local raster (GeoTIFF) is **read/exported**, not downloaded, and the plugin's
  wording now reflects that: the QGIS Task Manager entry reads *"Basemap raster
  export"*, and the run log and message bar say "read"/"export" rather than
  "download" for a local source (remote sources are unchanged).
- Dialog: the live tile-count estimate now sits with the **Tile size &
  resolution** controls, and under the **Zoom level** for XYZ; in the **Output**
  group, **Reproject sampling** now comes before **Output CRS**.
- Refreshed the dialog screenshot and updated the README (polite mode, per-job
  cache, GeoTIFF read/export, the overwrite prompt, and the new Advanced options).

## [1.4.20] - 2026-07-07
### Added
- A confirmation prompt before overwriting an existing output file.
### Changed
- Reorganized the dialog into collapsible groups (all open by default):
  - "Crop output to the exact extent" moved into the **Extent to render** group.
  - **Tile size & resolution** grouped together.
  - **Output** groups Output CRS, Reproject sampling, and the output destination.
- A **GeoTIFF** source is a local windowed read, not a download, so its dialog
  now reflects that: the Tile size & resolution group is collapsed and greyed
  (the raster is exported at its native resolution), and the Advanced options
  and the tile-count estimate are greyed out.

## [1.4.19] - 2026-07-07
### Added
- One-time automatic migration of a pre-1.4.18 flat `__btdcache__` cache: on the
  next run of the **same** job (matching fingerprint), the old flat cache is
  moved into the new per-job subdirectory and its stored tile paths are
  repointed, so an interrupted job resumes without re-downloading. A flat cache
  belonging to a different job is left untouched. (Supersedes the 1.4.18 note
  about finishing a job before upgrading.)

## [1.4.18] - 2026-07-07
### Added
- **Polite mode** for rate-limited / daily-quota servers:
  - Tiles are now fetched by walking an **8×8 grid of macro-cells** (like panning
    a map) rather than one long raster scan, so an interrupted or budget-limited
    run leaves a **spatially contiguous** finished region.
  - New Advanced option **"Stop after (tiles this run)"** — a per-run tile
    budget. The run stops, builds a partial mosaic, and leaves the rest pending;
    a re-run resumes. Use it to fill a daily-quota server's area over several
    days.
  - New Advanced option **"Rest after each macro-cell"** — pause N seconds
    between cells to ease off a server's short-term burst limit.
- Each job now caches under its **own subdirectory** of `__btdcache__` (keyed by
  the output filename, or the job fingerprint for a temporary output), so
  starting a different download no longer wipes an in-progress (e.g.
  rate-limited) job's cache.

### Note
- The cache layout changed, so a job that was in progress under a previous
  version won't auto-resume after upgrading — finish it first, or delete the old
  `__btdcache__`.

## [1.4.17] - 2026-07-06
### Added
- A "Reset to defaults" button in the Advanced options group, restoring the five
  Advanced settings to their defaults (parallel downloads resets to the current
  source's own preference).
### Removed
- The "Draw on Canvas" button in the extent selector — it didn't work usefully
  from the modal dialog. "Map Canvas Extent" and the other extent options are
  unchanged.

## [1.4.16] - 2026-07-06
### Changed
- Removed the `.flake8` config from the plugin package — a hidden dotfile inside
  the package is flagged as a "suspicious hidden file" by the QGIS Plugin
  Repository. It stays at the repo root for local dev and CI only; the plugin
  package now ships no hidden files.

## [1.4.15] - 2026-07-06
### Changed
- The `.flake8` config now ships **inside** the plugin package
  (`basemap_tile_downloader/.flake8`) so the QGIS Plugin Repository's Flake8
  Code Quality check — which runs against the uploaded package — honours it.
  E501 is disabled there (ruff owns the 100-col limit) alongside the
  compact-style pycodestyle codes.
### Added
- CI check-in now also runs **bandit** (security analysis, gated at medium+
  severity) and **detect-secrets** (secrets detection), in addition to ruff,
  flake8, byte-compile, unit tests, and metadata validation.

## [1.4.14] - 2026-07-05
### Changed
- Code-quality cleanups so the plugin passes flake8: renamed an ambiguous loop
  variable (`l`), and fixed inline-comment and comma spacing. Added a `.flake8`
  config (`max-line-length=100`, ignoring the pycodestyle codes for the plugin's
  deliberate compact style, mirroring `ruff.toml`) and a CI flake8 step. No
  behavior change.

## [1.4.13] - 2026-07-05
### Security
- Hardened XML parsing of remote WMS/WMTS GetCapabilities and ServiceException
  responses (which come from a user-chosen, untrusted server) against
  entity-expansion ("billion laughs") and external-entity (XXE) attacks. A new
  dependency-free `safexml.fromstring` replaces
  `xml.etree.ElementTree.fromstring` in the WMS and WMTS backends: it rejects
  in-document entity definitions and never resolves external entities, while
  still parsing legitimate responses unchanged (including WMS 1.1.1 DOCTYPEs).
  defusedxml would be the usual fix but isn't bundled with QGIS, so the
  standard-library expat parser is hardened directly instead. Added unit tests.

## [1.4.12] - 2026-07-05
### Fixed
- Added a `LICENSE` file inside the plugin package. The QGIS Plugin Repository
  requires one there, and the release zip archives only the package subtree, so
  the repo-root LICENSE wasn't included. CI now checks the package LICENSE
  exists.

## [1.4.11] - 2026-07-05
### Fixed
- `metadata.txt` now parses on the QGIS Plugin Repository. Its `changelog`
  field held literal percent signs, which the repository's `configparser`-based
  validator (percent interpolation enabled) rejected; the wording was adjusted
  to drop them.

## [1.4.10] - 2026-07-05
### Added
- First public release on the QGIS Plugin Repository (plugins.qgis.org).

## [1.4.9] - 2026-07-05
### Added
- When the fetch phase finishes and the mosaic build begins, a message-bar
  notice ("All tiles fetched — building the GeoTIFF mosaic…") now appears. The
  progress bar is already pinned at 100% by then and the mosaic step reports no
  progress of its own, so this reassures you the run isn't stuck.
### Changed
- Declared the plugin's license (`GPL-3.0-or-later`) in `metadata.txt`.

## [1.4.8] - 2026-07-04
### Changed
- Renamed the cache/work folder `btd_cache/` → `__btdcache__/` (next to the
  project, or in the QGIS profile), aligning with Python's `__pycache__`
  convention. An in-progress resumable queue in an old `btd_cache/` won't carry
  over — delete the stale folder, or just re-run to rebuild.
- README refreshed: documented the **Back-off cap** and **Give up after**
  Advanced options and the circuit-breaker "stopped early" behavior, noted that
  the run raises the **Log Messages** panel, added the new `backoff_cap` /
  `giveup_after` kwargs to the Python-console example, and corrected the
  `sync.ps1` developer note (it lives in the repo root).
### Fixed
- Mosaic-build GDAL failures now surface as a clear `Mosaic creation failed: …`
  message instead of a raw, cryptic GDAL exception.
- The WMTS zoom control no longer shows a misleading Web-Mercator m/px figure —
  it is a tile-matrix index, and the real resolution comes from the service's
  tile matrix set.

## [1.4.7] - 2026-07-04
### Fixed
- The task progress bar no longer sits frozen at the last fetch percentage
  (e.g. 93%) while the mosaic is built — including after a **cancel**, where the
  partial-mosaic build (VRT → Warp/Translate → overviews) can take a while and
  reports no progress of its own. Progress now jumps to 100% the moment the
  fetch phase ends and clears cleanly once finalization completes.

## [1.4.6] - 2026-07-03
### Fixed
- A resumed run no longer grinds for hours when a provider refuses a block of
  tiles (every request returning a ServiceException / 429 with the throttle
  pinned at its back-off cap). A run-level **circuit breaker** now stops after
  N requests in a row with no success (~10 min at the cap by default), builds a
  partial mosaic from what downloaded, and leaves the rest `pending` so a later
  re-run fills the gaps. Previously the progress bar would sit at (e.g.) 93% for
  hours while the same failing tiles were requeued.
### Added
- Two **Advanced** options to tune patience vs. speed per provider:
  *Back-off cap* (the adaptive throttle's ceiling, default 30 s) and *Give up
  after (server errors in a row)* (the circuit-breaker threshold, default 30;
  set to 0 / "Never" to disable it and keep only the per-tile limit).
### Changed
- Renamed the GitHub repository to `qgis-basemap-tile-downloader`; updated the
  tracker/repository/homepage URLs and the README CI badge. (GitHub redirects the
  old URL, and the plugin package folder `basemap_tile_downloader/` is unchanged.)

## [1.4.5] - 2026-07-03
### Changed
- Starting a download now raises QGIS's **Log Messages** panel (the *Basemap Tile
  Downloader* tab), so the live run log is visible without hunting for it, and the
  "download started" message points there.

## [1.4.4] - 2026-07-03
### Added
- Note in the README and plugin description that this plugin is for personal and
  educational use only, and to respect each provider's Terms of Service.
- README Q&A entry explaining that a `NaN` extent from "Calculate from Layer"
  usually means the layer's CRS doesn't match — reproject that layer.
### Changed
- The menu entry moved from **Web** to **Raster** (`Raster ▸ Basemap Tile
  Downloader…`), and the plugin category is now Raster — it exports a raster, so
  it belongs with the raster tools.

## [1.4.3] - 2026-07-02
### Added
- **Minimum delay between requests** (Advanced, default 0 s): a floor on the
  pace so requests are never sent closer together than this. Useful to pin a
  known-good rate for a strict server; 0 lets the adaptive throttle decide.
### Changed
- The mosaic is now **always built from whatever downloaded**, including after a
  **cancel** — so the gaps show exactly which tiles are missing (a cancelled run
  loads a partial mosaic and can be re-run to fill in the rest).
- Shorter tail on persistently-broken tiles: the back-pressure budget is 8 (was
  20) and the adaptive-backoff cap is 30 s (was 60 s), so a truly-unavailable
  region gives up in minutes rather than hours. Server-error back-off is now
  logged as "server error", not "429".

## [1.4.2] - 2026-07-02
### Changed
- WMS `ServiceException`s (the server transiently failing to draw a tile, e.g.
  "unable to access file") are now treated as back-pressure: the run backs off
  and retries the tile on the larger back-pressure budget instead of burning its
  6-attempt error budget with instant retries. Transient provider glitches that
  succeed on a later request now recover far more often.
- Repeated error messages in the log are collapsed: an error is logged in full
  the first time, then as a one-line "(repeat ×N)", with a per-error tally at the
  end of the run — so a provider outage no longer spams thousands of identical
  multi-line exceptions.

## [1.4.1] - 2026-07-01
### Added
- The dialog title bar now shows the plugin version (from metadata.txt, e.g.
  "Basemap Tile Downloader — v1.4.1"). When run from a git checkout it also
  appends the short commit hash.

## [1.4.0] - 2026-07-01
### Fixed
- A resumed run now re-fetches tiles that the queue marked `done` but whose
  cached file has gone missing (cache cleared/moved), instead of failing with
  "No tiles downloaded; cannot build mosaic".
### Added
- Local raster support: a layer backed by a file (GeoTIFF, etc. — the GDAL
  provider) can now be used as the source. There's nothing to download; the
  raster is read over the chosen extent at the chosen resolution and run through
  the same reproject/crop/mosaic pipeline. Resolution defaults to the raster's
  native pixel size. Single-band rasters (e.g. a DTM) keep their nodata value
  through the mosaic (instead of gaining an alpha band), so the exported clip
  renders the same as the source rather than being re-stretched by QGIS.
### Changed
- Clearer end-of-run log line: the misleading "All tiles resolved" is now
  "Queue drained: N of M tiles downloaded, K failed …" (logged as a warning when
  any tiles failed), so a run with failures is no longer reported like a success.

## [1.3.2] - 2026-07-01
### Changed
- The `download.log` is now truncated at the start of each run instead of being
  appended to, so it no longer grows without bound (the resumable state lives in
  the SQLite queue, not the log).
- Server rate-limiting is now logged explicitly: each throttle/timeout logs the
  reason (incl. any HTTP status and the server's `Retry-After`), the new pacing
  delay, and the back-pressure retry count.

## [1.3.1] - 2026-07-01
### Added
- The dialog now remembers the last-used extent (North/South/East/West and its
  CRS) and restores it the next time it opens.
### Changed
- The extent widget now uses the expanded (multi-field) layout instead of the
  condensed single line, which was unreadable in locales that use a comma as the
  decimal separator. It sits in a collapsible "Extent to render" group (open by
  default).
- Renamed the per-job working folder (SQLite queue + downloaded tiles) to
  `btd_cache/` (was `basemap_tile_downloader/` in 1.3.0).
- Suppressed QGIS's own task-finished/terminated notification (`QgsTask.Silent`);
  the plugin already posts its own completion and error messages, so QGIS's
  generic one was redundant noise after a failure.
### Fixed
- Throttle (HTTP 429/403/503) and timeout responses are now treated as
  back-pressure: the run slows down and the tile is retried on a separate,
  larger retry budget instead of spending its error budget, so sustained
  rate-limiting no longer marks otherwise-good tiles as permanently failed. The
  back-pressure budget is still bounded so a server that refuses forever can't
  loop indefinitely.
- A server-directed `Retry-After` is now honoured (as a bounded one-shot wait,
  up to 300 s) rather than being clamped to the 60 s adaptive-backoff cap, so we
  wait as long as the server asks instead of retrying too early and getting
  re-throttled. The wait remains cancelable from the Task Manager.

## [1.3.0] - 2026-07-01
### Changed
- Raised `qgisMinimumVersion` to 3.40.8 (the version the plugin is developed and
  tested against).
- Moved the "Crop output to the exact extent" checkbox up directly under the
  "Extent to render" selector, so it sits with the extent it applies to.
- Renamed the per-job working folder from `<project>/aoi_download/` to
  `<project>/basemap_tile_downloader/`, and the internal task/dialog/plugin
  classes from `Aoi*` to `BasemapTile*`, to match the plugin name. An
  interrupted run left in the old `aoi_download/` folder won't auto-resume;
  delete that folder or start the export again.
### Fixed
- Network requests now honour a 60 s transfer timeout, and a genuine timeout is
  detected reliably (`QgsBlockingNetworkRequest.TimeoutError`) and retried
  instead of being mistaken for an empty XYZ/WMTS tile (a permanent gap).
- `Retry-After` HTTP-date parsing no longer assumes GMT and drops the deprecated
  `datetime.utcnow()`.
- The XYZ zoom spinner is clamped to the layer's advertised `zmin`/`zmax`.
- WMTS downloads now show the large-download / Terms-of-Service confirmation
  (its tile count can't be estimated in advance, so it prompts each run).

## [1.2.1] - 2026-07-01
### Changed
- Renamed the plugin package folder `aoi_downloader` → `basemap_tile_downloader`
  (the installed plugin id changes accordingly). Settings are stored under a new
  group, so dialog settings reset once. The per-job working folder
  (`<project>/aoi_download/`) is unchanged.

## [1.2.0] - 2026-07-01
### Changed
- **Renamed the plugin to "Basemap Tile Downloader"** (menu, metadata, log tab).
  The Python package folder (`aoi_downloader`) is unchanged; the repository was
  renamed to `Basemap-Tile-Downloader`.
- **The download area is now a rectangular extent instead of an AOI polygon
  layer.** The dialog uses an extent selector (Calculate from Layer / Use
  Current Map Canvas Extent / Draw on Canvas), like QGIS's "Convert Map to
  Raster" dialog. The old "Clip to AOI polygon" option is now "Crop output to
  the exact extent". Downloading/clipping to an irregular polygon shape is no
  longer available.
### Added
- Collapsible **Advanced** section in the dialog holding "Parallel downloads"
  (concurrency) and a new "Maximum attempts per tile". WMS defaults to 2
  parallel downloads (stricter servers reject many simultaneous connections);
  XYZ/WMTS default to 4. Both settings are remembered per run.
### Fixed
- Re-running now retries tiles that failed on a previous run (previously
  'failed' tiles were skipped on resume), so gaps from transient server errors
  can be recovered without re-downloading everything.

## [1.1.1] - 2026-06-30
### Changed
- The large-download confirmation now also warns about respecting the
  provider's Terms of Service when many tiles are requested.
- The XYZ zoom label reports the resolution at the AOI's latitude (Web Mercator
  scale varies with latitude) instead of at the equator.

## [1.1.0] - 2026-06-30
### Added
- Clip the output to the AOI polygon (cutline) — optional in the dialog.
- WMTS source backend (in addition to WMS and XYZ).
- `ruff` lint and expanded unit tests in CI; automated release-zip build on tag.

## [1.0.0]
### Added
- Combined WMS + XYZ plugin with auto-detected source type.
- Resumable SQLite work queue; adaptive, per-source request throttling.
- Parallel tile fetching with a bounded worker pool.
- GDAL mosaic with overviews; optional reprojection with a selectable
  resampling method (bilinear / nearest / cubic / none).
- Live tile-count estimate with a large-download confirmation.
- Message-bar completion feedback (loaded / failed-tile / error).
- QGIS-style output widget (Save to File… / Save to Temporary File).
- Unit-tested Web-Mercator tile math; GitHub Actions CI.

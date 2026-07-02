# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

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

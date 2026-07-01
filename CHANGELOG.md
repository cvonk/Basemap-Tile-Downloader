# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]
### Changed
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

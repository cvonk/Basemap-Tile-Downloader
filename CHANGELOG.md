# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]
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

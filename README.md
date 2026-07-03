# Basemap Tile Downloader for QGIS

[![CI](https://github.com/cvonk/qgis-basemap-tile-downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/cvonk/qgis-basemap-tile-downloader/actions/workflows/ci.yml)

A QGIS plugin that exports a high-resolution GeoTIFF from a **WMS**, **WMTS**, or
**XYZ** basemap — or from a **local raster** (e.g. a GeoTIFF already loaded in
the project) — over a chosen rectangular extent.

It:
- Auto-detects whether the chosen layer is a WMS, WMTS, or XYZ tile source, or a
  local (GDAL) raster such as a GeoTIFF.
- Tiles the request over the extent — WMS `GetMap` at a chosen resolution/CRS,
  WMTS / Web-Mercator `{z}/{x}/{y}` tiles at a chosen zoom level, or a windowed
  read of a local raster at a chosen resolution.
- Throttles requests adaptively (tuned per source type) and fetches tiles in
  parallel, with a configurable number of parallel downloads. A server-directed
  `Retry-After` is honoured; a local raster has nothing to download, so it is
  read at full speed.
- Tracks progress in a resumable SQLite queue, so an interrupted run continues
  where it left off, and a re-run retries any tiles that failed previously.
- Georeferences each tile and mosaics them into a compressed, tiled GeoTIFF
  (with overviews), optionally reprojected to a chosen output CRS (selectable
  resampling) and cropped to the exact extent, then loads it into the project.
  Single-band rasters (e.g. a DTM) keep their nodata value instead of gaining an
  alpha band.

Requires the GDAL Python bindings (bundled with QGIS). Written for QGIS 3.40.8.

> **Note:** This plugin is intended for personal and educational use only.
> Bulk-downloading tiles may violate a provider's Terms of Service (e.g. Google,
> Bing, Esri). Make sure your intended use is permitted, and respect each
> provider's usage limits, before downloading.

## Installation

The installable plugin lives in the **`basemap_tile_downloader/`** sub-folder of
this repository (the repo root holds the README, licence and screenshots).

1. Copy the `basemap_tile_downloader` folder into your QGIS plugins folder:
   `$env:APPDATA\QGIS\QGIS3\profiles\default\python\plugins\`
2. In QGIS, open **Plugins ▸ Manage and Install Plugins ▸ Installed**.
3. Check the box next to **Basemap Tile Downloader** to activate it.

The tool then appears under **Raster ▸ Basemap Tile Downloader…** and on the toolbar.

> If you are developing, `sync.ps1` in the parent folder mirrors every plugin
> package here into the QGIS plugins folder; pair it with the *Plugin Reloader*
> plugin.

## Usage

### 1. Match the coordinate reference system

Set the project CRS to suit your source by clicking the EPSG code in the
bottom-right of the window. WMS is requested in that CRS; XYZ/WMTS tiles are
fetched in their native CRS (EPSG:3857 for XYZ) and reprojected to the output
CRS you pick in the dialog — or left in the native CRS if you choose **None**
for the resampling. (For example, **EPSG:32632** for an Italian UTM-32 source.)

### 2. Add the basemap

**WMS** — get the WMS URL from your provider, then
**Layer ▸ Data Source Manager ▸ WMS/WMTS ▸ New**:
  - **Name** – e.g. `Copertura regioni WMS`
  - **URL** – e.g. `http://wms.pcn.minambiente.it/ogc?map=/ms_ogc/WMS_v1.3/raster/ortofoto_colore_12.map`

  Connect and add the layer (e.g. *Ortofoto a colori anno 2012 ▸ Copertura …
  WGS84 - UTM32*).

**WMTS** — **Layer ▸ Data Source Manager ▸ WMS/WMTS ▸ New**, connect to the
service's `GetCapabilities` URL and add a WMTS layer / tile matrix set.

**XYZ** — **Layer ▸ Data Source Manager ▸ XYZ ▸ New**, give it a name and a
`{z}/{x}/{y}` URL template.

**Local raster (GeoTIFF)** — just load the file in QGIS
(**Layer ▸ Add Layer ▸ Add Raster Layer…**). Any GDAL-readable raster works;
there is nothing to download, so the tool exports it over the chosen extent
(reprojecting/cropping as configured).

### 3. Export to GeoTIFF

Open **Raster ▸ Basemap Tile Downloader…**. Pick the source layer (the dialog shows
the fields for its type), choose the **extent to render**, then set the output.

![Basemap Tile Downloader dialog](media/dialog.png)

The **Extent to render** selector works like QGIS's *Convert Map to Raster*
dialog — set it from the dropdown:
- **Calculate from Layer** – the bounding box of a layer,
- **Use Current Map Canvas Extent** – the current view,
- or type the min/max coordinates directly.

**WMS example**

| Setting | Example |
| --- | --- |
| Source layer | `Copertura regioni WMS` |
| Extent to render | Current map canvas extent |
| Tile size | `1024` |
| Resolution | `0.5` |
| Output CRS | `EPSG:32632` |
| Reproject sampling | `Bilinear` (or Nearest / Cubic / None) |
| Crop output to the exact extent | ☐ |
| Parallel downloads (Advanced) | `2` (lower for strict servers) |
| Output | `C:\Users\you\output.tif` (or a temporary file) |

**XYZ example**

| Setting | Example |
| --- | --- |
| Source layer | `OpenStreetMap` |
| Extent to render | Current map canvas extent |
| Zoom level | `18` (≈ 0.6 m/px) |
| Output CRS | `EPSG:32632` |
| Reproject sampling | `Bilinear` (or Nearest / Cubic / None) |
| Crop output to the exact extent | ☐ |
| Parallel downloads (Advanced) | `4` |
| Output | `C:\Users\you\output.tif` (or a temporary file) |

(A **WMTS** layer uses the same fields as XYZ — pick a **zoom level**.)

**Local raster (GeoTIFF) example**

| Setting | Example |
| --- | --- |
| Source layer | `DTM Italy (10m)` |
| Extent to render | Current map canvas extent |
| Tile size | `1024` |
| Resolution | `10` (defaults to the raster's native pixel size) |
| Output CRS | `EPSG:32632` |
| Reproject sampling | `Bilinear` (or Nearest / Cubic / None) |
| Crop output to the exact extent | ☑ |
| Output | `C:\Users\you\clip.tif` (or a temporary file) |

(A local raster uses the same fields as WMS — tile size + resolution. The
resolution defaults to the raster's native pixel size when you pick the layer.)

Notes:
- The dialog shows a live tile-count estimate as you adjust the settings; above
  about 5,000 tiles it asks for confirmation (and a Terms-of-Service reminder)
  before starting, to avoid an accidental huge download.
- **Reproject sampling: None** keeps the mosaic in its native CRS (no
  reprojection, no resampling).
- **Crop output to the exact extent** trims the tile-aligned mosaic to the
  precise extent rectangle.
- **Parallel downloads**, **Maximum attempts per tile** and **Minimum delay
  between requests** are in the collapsible **Advanced** section. Lower the
  parallel downloads (1–2) for strict servers that reject many simultaneous
  connections; WMS defaults to 2, XYZ/WMTS to 4. The **minimum delay** (default
  0 s) is a floor on the pace — raise it (e.g. 2 s) to pin a known-good rate for
  a strict server; at 0 the adaptive throttle sets the pace on its own.
- If you **cancel** a run, the mosaic is still built from whatever downloaded so
  far (with gaps where tiles are missing), and re-running fills in the rest.

Click **OK** to start. Progress is shown in the Task Manager, and the finished
mosaic is added to the project automatically.

## Q & A

**"Calculate from Layer" fills the extent with `NaN`.**
This usually means the CRS of the layer you picked for the extent doesn't line
up with the extent's CRS, so reprojecting its bounding box produces invalid
(`NaN`) coordinates. Reproject the layer you're using for the extent so its CRS
matches (right-click the layer ▸ **Export ▸ Save Features As…** and choose the
target CRS) — or, if its assigned CRS is simply wrong, fix it under the layer's
**Properties ▸ Source ▸ Assigned CRS**. Then pick the layer again.

**Why is my map blurry?**
Check that the resolution / zoom and the coordinate reference systems suit your
source. For XYZ, requesting a zoom finer than the provider serves only
interpolates — it adds no real detail.

**Why are some tiles missing?**
Tiles can fail from server-side rate-limiting/throttling or a transient server
error (the completion message and `download.log` report how many failed).
**Just run the export again with the same settings** — it keeps the tiles
already downloaded and retries the failed ones, so the gaps fill in once the
server cooperates. If a specific server keeps failing many parallel requests,
lower **Parallel downloads** (to 1–2). For XYZ, `404`/`204` tiles are treated as
legitimate gaps (no data at that tile).

If the *same* tiles keep failing no matter how often you re-run, open
`download.log` (in the `btd_cache/` folder next to your project) and read the
per-tile errors: the service may simply not have data for that area. A WMS
`ServiceException` such as *"Unable to access file … tile_33_12.shp"*, or errors
confined to one part of the extent, usually mean the provider can't serve that
region — not a transient glitch, so retrying won't help. Often it is one
sublayer that doesn't cover your whole extent (e.g. an adjacent UTM-zone layer):
recreate the WMS layer requesting only the sublayer that covers your area, or
shrink the extent to the covered region. Note the log is rewritten on each run,
so copy it before re-running if you want to keep the evidence.

**A run failed with a WMS `ServiceException` about a file it can't open.**
That's the *provider's* server failing to read its own data (often intermittent)
— not a plugin or network problem on your side. Wait and re-run; the failed
tiles will be retried.

**Can I export a local GeoTIFF instead of downloading?**
Yes. Load any GDAL-readable raster in QGIS and pick it as the source layer. There
is nothing to download — the raster is read over the chosen extent and run
through the same reproject / crop / mosaic pipeline. Use it to clip, reproject,
or resample a raster to a new GeoTIFF. (A clip may look brighter than the source
in QGIS: that's QGIS recomputing its contrast stretch over the smaller value
range, not a change in the data — match the layers' Min/Max in Symbology to
compare.)

**Which version of QGIS is this for?**
It was written for QGIS 3.40.8.

**Can I run it from the QGIS Python Console?**
Yes — the source backend is auto-detected from the layer you pass:

```python
from basemap_tile_downloader import engine
from qgis.core import QgsProject
from qgis.utils import iface

wms = QgsProject.instance().mapLayersByName("Copertura regioni WMS")[0]

extent = iface.mapCanvas().extent()               # any QgsRectangle
extent_crs = QgsProject.instance().crs().authid()

# WMS / local raster: opts = {tile_pixels, resolution};  XYZ/WMTS: opts = {zoom}
engine.run(layer=wms, extent=extent, extent_crs=extent_crs,
           opts={"tile_pixels": 1024, "resolution": 0.5},
           out_crs="EPSG:32632",
           resample="bilinear",            # near | bilinear | cubic | none
           clip=True,                      # crop to the exact extent
           concurrency=2,                  # parallel tile fetches
           min_delay=0,                    # floor (s) on the pace; 0 = adaptive
           output_path=r"C:\Users\you\output.tif")  # or temporary=True for a temp file
```

## Licence

See [LICENSE](LICENSE).

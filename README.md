# AOI Downloader for QGIS

A QGIS plugin that exports a high-resolution GeoTIFF from a **WMS** or **XYZ**
basemap, clipped to a polygon area of interest (AOI).

It:
- Auto-detects whether the chosen layer is a WMS or an XYZ tile source.
- Tiles the request over the AOI — WMS `GetMap` at a chosen resolution/CRS, or
  Web-Mercator `{z}/{x}/{y}` at a chosen zoom level.
- Throttles requests adaptively to stay within the server's rate limits.
- Tracks progress in a resumable SQLite queue, so an interrupted run continues
  where it left off.
- Georeferences each tile and mosaics them into a compressed, tiled GeoTIFF
  (with overviews), optionally reprojected to a chosen output CRS, then loads it
  into the project.

Requires the GDAL Python bindings (bundled with QGIS). Written for QGIS 3.40.8.

## Installation

The installable plugin lives in the **`aoi_downloader/`** sub-folder of this
repository (the repo root holds the README, licence and screenshots).

1. Copy the `aoi_downloader` folder into your QGIS plugins folder:
   `$env:APPDATA\QGIS\QGIS3\profiles\default\python\plugins\`
2. In QGIS, open **Plugins ▸ Manage and Install Plugins ▸ Installed**.
3. Check the box next to **AOI Downloader** to activate it.

The tool then appears under **Web ▸ AOI Downloader…** and on the toolbar.

> If you are developing, `sync.ps1` in the parent folder mirrors every plugin
> package here into the QGIS plugins folder; pair it with the *Plugin Reloader*
> plugin.

## Usage

### 1. Match the coordinate reference system

Set the project CRS to suit your source by clicking the EPSG code in the
bottom-right of the window. WMS is requested in that CRS; XYZ is always fetched
in EPSG:3857 and reprojected to the output CRS you pick in the dialog.
(For example, **EPSG:32632** for an Italian UTM-32 source.)

### 2. Add the basemap

**WMS** — get the WMS URL from your provider, then
**Layer ▸ Data Source Manager ▸ WMS/WMTS ▸ New**:
  - **Name** – e.g. `Copertura regioni WMS`
  - **URL** – e.g. `http://wms.pcn.minambiente.it/ogc?map=/ms_ogc/WMS_v1.3/raster/ortofoto_colore_12.map`

  Connect and add the layer (e.g. *Ortofoto a colori anno 2012 ▸ Copertura …
  WGS84 - UTM32*).

**XYZ** — **Layer ▸ Data Source Manager ▸ XYZ ▸ New**, give it a name and a
`{z}/{x}/{y}` URL template.

### 3. Define the area of interest

Create a polygon layer to outline the region to export:

- **Layer ▸ Create Layer ▸ New Temporary Scratch Layer**
  - **Name** – e.g. `Area of Interest (EPSG:32632)`
  - **Geometry type** – Polygon
  - **CRS** – your project CRS

Then draw the boundary (e.g. roughly 10 × 10 km):

1. Center the target area on the canvas and set the scale to about 1:30,000.
2. Select the AOI layer in the **Layers** panel.
3. Enable editing (the yellow pencil in the toolbar).
4. Use **Add Polygon Feature** to draw the boundary: left-click to place each
   corner, then right-click to finish.
5. Turn editing off again and save the changes when prompted.

### 4. Export to GeoTIFF

Open **Web ▸ AOI Downloader…**. Pick the source layer — the dialog shows the
fields for its type — and the AOI polygon, then set the output.

![AOI Downloader dialog](media/dialog.png)

**WMS example**

| Setting | Example |
| --- | --- |
| Source layer | `Copertura regioni WMS` |
| AOI polygon layer | `Area of Interest (EPSG:32632)` |
| Tile size | `1024` |
| Resolution | `0.5` |
| Output CRS | `EPSG:32632` |
| Output | `C:\Users\you\output.tif` (or a temporary file) |

**XYZ example**

| Setting | Example |
| --- | --- |
| Source layer | `OpenStreetMap` |
| AOI polygon layer | `Area of Interest (EPSG:32632)` |
| Zoom level | `18` (≈ 0.6 m/px) |
| Output CRS | `EPSG:32632` |
| Output | `C:\Users\you\output.tif` (or a temporary file) |

Click **OK** to start. Progress is shown in the Task Manager, and the finished
mosaic is added to the project automatically.

## Q & A

**Why is my map blurry?**
Check that the resolution / zoom and the coordinate reference systems suit your
source. For XYZ, requesting a zoom finer than the provider serves only
interpolates — it adds no real detail.

**Why are some tiles missing?**
For WMS, the request rate may not have adapted quickly enough to server-side
throttling — re-run the export and the resumable queue fills in the gaps. For
XYZ, `404`/`204` tiles are treated as legitimate gaps (no data at that tile).

**Which version of QGIS is this for?**
It was written for QGIS 3.40.8.

**Can I run it from the QGIS Python Console?**
Yes — the source backend is auto-detected from the layer you pass:

```python
from aoi_downloader import engine
from qgis.core import QgsProject

wms = QgsProject.instance().mapLayersByName("Copertura regioni WMS")[0]
aoi = QgsProject.instance().mapLayersByName("Area of Interest (EPSG:32632)")[0]

# WMS: opts = {tile_pixels, resolution};  XYZ: opts = {zoom}
engine.run(layer=wms, aoi_layer=aoi,
           opts={"tile_pixels": 1024, "resolution": 0.5},
           out_crs="EPSG:32632",
           output_path=r"C:\Users\you\output.tif")
```

## Licence

See [LICENSE](LICENSE).

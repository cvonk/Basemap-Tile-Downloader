# Basemap Tile Downloader for QGIS

[![CI](https://github.com/cvonk/qgis-basemap-tile-downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/cvonk/qgis-basemap-tile-downloader/actions/workflows/ci.yml)

A QGIS plugin that exports a high-resolution GeoTIFF from an online **WMS**, **WMTS**, or
**XYZ** basemap — or from a **local raster** (e.g. a GeoTIFF already loaded in
the project) — over a chosen rectangular extent.

This build:
- Auto-detects whether the chosen layer is a WMS, WMTS, or XYZ tile source, or a
  local (GDAL) raster such as a GeoTIFF.
- Tiles the request over the extent — WMS `GetMap` at a chosen resolution or zoom level.
- Throttles requests adaptively (tuned per source type) and fetches tiles in parallel, with a configurable number of parallel downloads.
- Tracks progress in a resumable SQLite queue (one per job, kept beside your project), so an interrupted run continues where it left off and a re-run retries any tiles that failed previously. A cache folder can also be moved or backed up and still resume.
- Georeferences each tile and mosaics them into a compressed, tiled GeoTIFF (with overviews), optionally reprojected to a chosen output CRS and cropped to the exact extent, then loads it into the project.
- Single-band rasters (e.g. a DTM) keep their nodata value instead of gaining an alpha band.

Requires the GDAL Python bindings (bundled with QGIS). Originally written for QGIS 3.40.8.  Tested in QGIS 4.2.0.

> **Note:** This plugin is intended for personal and educational use only. Bulk-downloading tiles may violate a provider's Terms of Service (e.g. Google, Bing, Esri). Make sure your intended use is permitted, and respect each provider's usage limits, before downloading.

## Native Installation

Install from QGIS using the Plugins > Manage and Install Plugins, and search for "Basemap Tile Downloader".

## Manual Installation

The installable plugin lives in the **`basemap_tile_downloader/`** sub-folder of
this repository (the repo root holds the README, licence and screenshots).

1. Copy the `basemap_tile_downloader` folder into your QGIS plugins folder.
   QGIS 4 uses a separate profile root from QGIS 3, so pick the one for your
   version:
   - QGIS 3: `$env:APPDATA\QGIS\QGIS3\profiles\default\python\plugins\`
   - QGIS 4: `$env:APPDATA\QGIS\QGIS4\profiles\default\python\plugins\`

   (On macOS/Linux the base is `~/.local/share/QGIS/QGIS3` or `…/QGIS4`.)
2. In QGIS, open **Plugins ▸ Manage and Install Plugins ▸ Installed**.
3. Check the box next to **Basemap Tile Downloader** to activate it.

The tool then appears under **Raster ▸ Basemap Tile Downloader…** and on the toolbar.

> If you are developing, run `sync.ps1` in the repo root (`pwsh -File sync.ps1`)
> to mirror this plugin into your QGIS plugins folder; pair it with the *Plugin
> Reloader* plugin. (The script finds any plugin package — a folder with a
> `metadata.txt` — under its own directory, so it also works from a parent
> folder holding several plugin repos.)

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
| Tile size &amp; resolution | *greyed — exported at the raster's native resolution* |
| Output CRS | `EPSG:32632` |
| Reproject sampling | `Bilinear` (or Nearest / Cubic / None) |
| Crop output to the exact extent | ☑ |
| Output | `C:\Users\you\clip.tif` (or a temporary file) |

(A local raster is **read**, not downloaded, so it is exported at its native
resolution: the *Tile size &amp; resolution* group is collapsed and greyed, and so
are the *Advanced* options and the tile-count estimate — they only apply to a
network download. The QGIS Task Manager labels the run *"Basemap raster export"*.)

Notes:
- The dialog is organised into collapsible groups — **Extent to render** (with
  the *Crop to the exact extent* option), **Tile size &amp; resolution**, and
  **Output** — all open by default.
- A live tile-count estimate updates as you adjust the settings — with the *Tile
  size &amp; resolution* controls for WMS, or under the *Zoom level* for XYZ. Before
  a large download the dialog asks for confirmation (with a Terms-of-Service
  reminder): above about 5,000 estimated tiles, or for any WMTS export, whose
  count can't be predicted in advance.
- If the chosen **output file already exists**, the dialog asks before
  overwriting it.
- **Reproject sampling: None** keeps the mosaic in its native CRS (no
  reprojection, no resampling).
- **Crop output to the exact extent** trims the tile-aligned mosaic to the
  precise extent rectangle.
- The collapsible **Advanced** section holds the tuning knobs (**Reset to
  defaults** restores them; the whole section is greyed out for a local raster):
  - **Parallel downloads** — lower it (1–2) for strict servers that reject many
    simultaneous connections; WMS defaults to 2, XYZ/WMTS to 4.
  - **Maximum attempts per tile** — how many times a tile is retried before it is
    marked failed.
  - **Minimum delay between requests** (default 0 s) — a floor on the pace; raise
    it (e.g. 2 s) to pin a known-good rate for a strict server. At 0 the adaptive
    throttle sets the pace on its own.
  - **Back-off cap** (default 30 s) — the longest the adaptive throttle will wait
    between requests while a server is throttling/erroring. Lower it to retry
    sooner (more aggressive); raise it to be gentler.
  - **Give up after (server errors in a row)** (default 30) — stop the run when
    this many requests in a row fail with no success (a server refusing a block
    of tiles), then build a partial mosaic from what downloaded and leave the
    rest for a re-run. Set it to 0 (“Never”) to keep only the per-tile limit.
- If you **cancel** a run, the mosaic is still built from whatever downloaded so
  far (with gaps where tiles are missing), and re-running fills in the rest.

Click **OK** to start. Progress is shown in the Task Manager, the live run log
opens in the **Log Messages** panel (the *Basemap Tile Downloader* tab), and the
finished mosaic is added to the project automatically.

## Q & A

**"Calculate from Layer" fills the extent with `NaN`.**
This usually means the CRS of the layer you picked for the extent doesn't line
up with the extent's CRS, so reprojecting its bounding box produces invalid
(`NaN`) coordinates. Reproject the layer you're using for the extent so its CRS
matches (right-click the layer ▸ **Export ▸ Save Features As…** and choose the
target CRS) — or, if its assigned CRS is simply wrong, fix it under the layer's
**Properties ▸ Source ▸ Assigned CRS**. Then pick the layer again.

**Does the extent (AOI) layer need the same CRS as the source layer?**
No. The extent is reprojected automatically: **Calculate from Layer** transforms
the AOI layer's bounding box into the project CRS, and the plugin then reprojects
that to whatever the source needs (EPSG:3857 for XYZ, the request CRS for WMS,
the tile-matrix-set CRS for WMTS, or the raster's CRS for a local GeoTIFF). So
the AOI, the project, and the source can all be in different CRSs. The only real
requirement is that each layer's **assigned CRS is correct** — a *mislabelled*
AOI CRS is what produces a wrong or `NaN`/0 extent (see the previous entry).
Because the extent is a reprojected bounding box, for an oblique CRS it may be a
little larger than the tight box in the source CRS — harmless, just a few extra
edge tiles.

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
legitimate gaps (no data at that tile), and a resume won't re-request them, so it
doesn't waste requests (or a quota tile) re-confirming known gaps.

If a server refuses a whole block of tiles and keeps failing every request, the
run **stops early** rather than grinding for hours (“Server unavailable — stopped
early…”): it builds a partial mosaic from what downloaded and leaves the rest to
a re-run. You can tune when this kicks in with **Give up after** / **Back-off
cap** in the Advanced section (see above).

If the *same* tiles keep failing no matter how often you re-run, open
`download.log` (each export gets its own subfolder under `__btdcache__/`, next to
your project, named after the output file) and read the per-tile errors: the
service may simply not have data for that area. A WMS
`ServiceException` such as *"Unable to access file … tile_33_12.shp"*, or errors
confined to one part of the extent, usually mean the provider can't serve that
region — not a transient glitch, so retrying won't help. Often it is one
sublayer that doesn't cover your whole extent (e.g. an adjacent UTM-zone layer):
recreate the WMS layer requesting only the sublayer that covers your area, or
shrink the extent to the covered region. Note the log is rewritten on each run,
so copy it before re-running if you want to keep the evidence.

**A server rate-limits me, or blocks me after a while.**
The adaptive throttle already backs off and retries, but for a strict server you
can ease it further: drop **Parallel downloads** to 1 and raise **Minimum delay**
(e.g. 2–8 s) in the Advanced section. If a provider enforces a **daily quota**
(everything works, then stops for the day), no pacing beats it — spread the work
across days: run until it stops, then **re-run the next day** and it continues
where it left off (the resumable per-job cache picks up the pending tiles).

**Can I move, back up, or restore the download cache?**
Yes. Each export's progress lives in its own subfolder of `__btdcache__/` (next
to your project, named after the output file): the SQLite queue plus a `tiles/`
folder. The queue stores file paths *relative* to that subfolder, so you can
move, copy, or restore the **whole subfolder** — queue and `tiles/` together — to
another folder, drive, or machine, and a re-run with the same settings resumes
without re-downloading. Just keep the queue and its `tiles/` folder as siblings.
(Caches from before this feature stored absolute paths; those still work in place,
but move them and their tiles will be re-fetched.)

**I download overlapping areas — can it reuse tiles between them?**
Yes, for **XYZ**, **WMTS** and **WMS** sources. Their tiles have a fixed global
identity (`{z}/{x}/{y}` for XYZ, matrix/col/row for WMTS, and — because the WMS
grid is anchored to the CRS origin — global col/row for WMS), so a tile one area
already fetched is reused by any overlapping area at the same settings: no repeat
request, and no hit against the provider's quota. These shared tiles live in
`__btdcache__/shared/<source>/`, separate from the per-job folders and keyed by
the source so different providers or settings never mix (for WMS the key covers
the endpoint, layers, styles, CRS, format, resolution and tile size — change any
and it's a different set). Delete that `shared/` folder to clear it (e.g. if a
provider refreshes its imagery). A local GeoTIFF isn't downloaded, so nothing is
cached for it. Note WMS tiles now align to the CRS origin rather than the
extent's corner, so overlapping WMS exports share a pixel grid; the exact-extent
crop still trims the final mosaic, so the output isn't enlarged.

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
It was written for QGIS 3.40.8 and also runs on QGIS 4.2.0. The same package
works on both Qt 5 (QGIS 3.40+) and Qt 6 (QGIS 4).

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
           backoff_cap=30,                 # s; adaptive back-off ceiling
           giveup_after=30,                # consecutive failures → stop; 0 = never
           output_path=r"C:\Users\you\output.tif")  # or temporary=True for a temp file
```

## Licence


See [LICENSE](LICENSE).

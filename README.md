# WMS AOI Downloader for QGIS

- Requests tiles using WMS GetMap within the bounding box of a polygon area of interest (AOI)
- Downloads with adaptive throttling
- Uses a resumable SQLite queue
- Mosaics the tiles into a compressed, tiled GeoTIFF (with overviews) that is loaded into the project.

## Installation

Copying this directory to your `$env:APPDATA\QGIS\QGIS3\profiles\default\python\plugins\` directory.

Plugins ▸ Plugin Manager ▸ Manage and Install Plugins ▸ Installed > WMS AOI Downloader
- put a checkmark before the WMS AOI Downloader plugin to activate it. 

## Usage

Ensure the Coordinate Reference System corresponds with your WMS source. 

Bottom-right > EPSG:xx
- Set to make the project CRS match your WMS source (e.g. EPSG:32632 for East Italy)

### Specify how to get the base map 

Consult your source for the WMS URL.

In QGIS
- Layer ▸ Data Source Manager ▸ WMS/WMTS ▸ right-click New Connection
  - Name = e.g. Copertura regioni WMS
  - URL = e.g. http://wms.pcn.minambiente.it/ogc?map=/ms_ogc/WMS_v1.3/raster/ortofoto_colore_12.map
    - Double-click each
      - Italy Geoportale Nazionale Ortho (1m) > Orthofoto a colori anno 2012 > Copertura .. WGS84 - UTM32
      - Italy Geoportale Nazionale Ortho (1m) > Orthofoto a colori anno 2012 > Copertura .. WGS84 - UTM33

### Set an export boundary (e.g. 10x10 km e.g.)

Layer ▸ Create Layer ▸ New Temporary Scratch Layer
  - Name = Area of Interest (EPSG:32632)
  - Geometry type = Polygon
  - CRS = (your project CRS)
  - OK
  
Center your Area of Interest in the middle of the canvas

Set the canvas scale to about 1:30,000	

Layers Panel on the left-size > Area of Interest (EPSG:32632)
  - Make sure the Editing mode is enabled in the top main toolbar (icon looks like a yellow pencil)
  - Use the Add Polygon Feature button on the Main (Editing) toolbar to draw a box around your Area of Interest (the icon looks like an irregular green shape with a small starburst)
    - Left-click on the map canvas to place your first corner
    - Left-click to place the other corners
    - Right-click anywhere on the canvas to finish drawing
    T- oggle the Edit mode to disable (and save the changes to the layer)
    
## Exporting to HiRes GeoTIFF

Web ▸ WMS AOI Downloader…
  - e.g.
    - WMS layer = Copertura regioni WMS
    - AOI polygon layer = Area of Interest (EPSG:32632)
    - Tile size = 1024
    - Resolution = 0.5
    - Output path = C:\User\you\output.tiff





Usage:

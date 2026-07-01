# -*- coding: utf-8 -*-
"""
tilemath.py – pure Web-Mercator (EPSG:3857) XYZ tile math.

Deliberately has NO QGIS/GDAL imports so it can be unit-tested standalone
(see tests/test_tilemath.py). The XYZ source backend builds on these.
"""

import math

TILE_PIXELS = 256                     # XYZ tiles are 256×256 by definition
WM_ORIGIN   = 20037508.342789244      # half the world extent, metres


def tile_span_m(z):
    """Width/height of one tile, in metres, at zoom level z."""
    return (2.0 * WM_ORIGIN) / (2 ** z)


def tile_resolution_m(z):
    """Ground resolution (m/px) at the equator for the given zoom."""
    return tile_span_m(z) / TILE_PIXELS


def tile_resolution_m_at_lat(z, lat_deg):
    """Ground resolution (m/px) at a given latitude. Web Mercator scale grows
    towards the poles, so the true ground resolution is the equatorial value
    times cos(latitude)."""
    return tile_resolution_m(z) * math.cos(math.radians(lat_deg))


def tile_bounds_3857(x, y, z):
    """EPSG:3857 bounds of tile (x, y) at zoom z as (ulx, uly, lrx, lry)."""
    span = tile_span_m(z)
    ulx = -WM_ORIGIN + x * span
    uly =  WM_ORIGIN - y * span
    return ulx, uly, ulx + span, uly - span


def xyz_url(template, x, y, z):
    """Fill a {z}/{x}/{y} (and {-y} TMS) template."""
    return (template.replace("{z}", str(z))
                    .replace("{x}", str(x))
                    .replace("{y}", str(y))
                    .replace("{-y}", str((2 ** z) - 1 - y)))


def tile_range(minx, miny, maxx, maxy, z):
    """Inclusive tile index range (xmin, xmax, ymin, ymax) covering an
    EPSG:3857 bounding box at zoom z, clamped to the valid [0, 2^z-1] grid."""
    span = tile_span_m(z)
    n    = 2 ** z
    def clamp(v):
        return max(0, min(n - 1, v))
    xmin = clamp(int(math.floor((minx + WM_ORIGIN) / span)))
    xmax = clamp(int(math.floor((maxx + WM_ORIGIN) / span)))
    ymin = clamp(int(math.floor((WM_ORIGIN - maxy) / span)))
    ymax = clamp(int(math.floor((WM_ORIGIN - miny) / span)))
    return xmin, xmax, ymin, ymax


def wms_grid_dims(width, height, step):
    """Number of (cols, rows) of `step`-sized tiles covering a width×height
    bounding box, at least 1 each. Used by the WMS backend."""
    if step <= 0:
        raise ValueError("step must be > 0")
    return (max(1, math.ceil(width / step)), max(1, math.ceil(height / step)))

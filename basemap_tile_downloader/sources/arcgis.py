# -*- coding: utf-8 -*-
"""ArcGIS REST MapServer source backend.

Downloads via the service's `export` endpoint (bbox + size → image), tiling an
extent the same origin-anchored way WMS does. Faithful by default (no colour
change). Optionally *harmonises flight years*: for a service whose layers are
per-year orthophotos (e.g. Land Salzburg's `Orthofoto_Land_Salzburg`), it fetches
each year separately and colour-matches the older years to the newest on the
seam between them, then composites — removing the year-boundary seam without the
global muting a whole-image balance would cause.  Requires GDAL + numpy only.
"""

import re
import json
import math
import os
import urllib.parse

from qgis.core import (
    QgsProject, QgsRectangle, QgsGeometry, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsRasterLayer, QgsDataSourceUri,
)

from .. import engine
from ..engine import DownloaderError, TileFetchError

try:
    from osgeo import gdal
    import numpy as np
except Exception:  # pragma: no cover - GDAL/numpy always present under QGIS
    gdal = None
    np = None

SOURCE_NAME = "ArcGIS"
INITIAL_DELAY_SEC = 0.5
CONCURRENCY = 3
# Some services publish a deep archive of dated layers (Tirol goes back to 1940).
# Harmonise only the newest few — a current basemap wants recent imagery, and
# the older layers only fill gaps the newest ones leave (and are fetched too).
_MAX_HARMONISE_YEARS = 4

# ArcGIS returns a ServiceException-style error as JSON even for f=image, so a
# failed tile has a small JSON body rather than image bytes.
# A 4-digit 19xx/20xx year not glued to other digits — matches "2022" and the
# first year of a range like "2019_2021" (\b won't do: '_' is a word char).
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d\d(?!\d)")


# ─────────────────────────────────────────────
# DETECTION / PARAMS
# ─────────────────────────────────────────────
def detect(layer):
    if not isinstance(layer, QgsRasterLayer):
        return False
    return layer.providerType() == "arcgismapserver"


def _base_url(raw):
    """The MapServer/ImageServer endpoint from a QGIS arcgismapserver URI.

    arcgismapserver stores a `key='value'` datasource string (e.g.
    ``format='' layer='19' url='…/MapServer'``); the QgsDataSourceUri *constructor*
    parses that, whereas setEncodedUri (used for the WMS providers) does not."""
    uri = QgsDataSourceUri(raw)
    url = (uri.param("url") or uri.param("URL") or "").strip()
    if not url:                                  # fall back to a raw url=… token
        dec = urllib.parse.unquote(raw)
        for part in dec.split("&"):
            if part.lower().startswith("url="):
                url = part[4:].strip(); break
    if not url and raw.strip().lower().startswith(("http://", "https://")):
        url = raw.strip()                        # source is just the bare URL
    return url.rstrip("/"), uri


def extract_params(layer):
    url, uri = _base_url(layer.source())
    if not url:
        raise DownloaderError("Could not read the ArcGIS MapServer URL from the layer.")
    if "/export" in url.lower():
        url = url[: url.lower().index("/export")]
    # QGIS points a chosen sublayer at …/MapServer/<id>; reduce to the service
    # base (needed for the ?f=json metadata and the /export endpoint) and keep
    # <id> as the selected layer for a plain, non-harmonised download.
    sel = uri.param("layer")
    m = re.search(r"/(\d+)$", url)
    if m and url[: m.start()].lower().endswith(("mapserver", "imageserver")):
        sel = sel or m.group(1)
        url = url[: m.start()]
    crs = (uri.param("crs") or uri.param("CRS") or "").upper()
    if not crs:
        crs = layer.crs().authid()
    return {
        "url": url,
        "crs": crs,                              # refined to the service SR in prepare()
        "format": "png32",                       # RGBA: alpha marks a tile's coverage
        "sel_show": sel,                         # None → composite (all visible layers)
        "years": [],                             # filled by prepare() when harmonising
    }


# ─────────────────────────────────────────────
# PREPARE  (discover service SR + per-year layers)
# ─────────────────────────────────────────────
def _service_json(url, logger):
    status, _h, body, err, timed_out = engine.blocking_get(url + "?f=json")
    if timed_out:
        raise DownloaderError("Timed out reading the ArcGIS service metadata.")
    if err or (status and status >= 400):
        raise DownloaderError(f"ArcGIS service metadata request failed (HTTP {status}; {err}).")
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except Exception as e:
        raise DownloaderError(f"Could not parse ArcGIS service metadata: {e}")


def prepare(params, opts, logger):
    meta = _service_json(params["url"], logger)
    sr = (meta.get("spatialReference") or {})
    wkid = sr.get("latestWkid") or sr.get("wkid")
    if wkid:
        params["crs"] = f"EPSG:{wkid}"
    logger.info("ArcGIS service SR: %s", params["crs"])

    # Per-year layers: a group layer whose name carries a year, with an "Image"
    # child (…/Orthofotos 2024 → child "Image"). Map year → that Image layer id.
    layers = meta.get("layers") or []
    by_id = {lyr["id"]: lyr for lyr in layers}
    years = []
    for lyr in layers:
        m = _YEAR_RE.search(lyr.get("name", ""))
        if not m or not lyr.get("subLayerIds"):
            continue
        img = next((cid for cid in lyr["subLayerIds"]
                    if by_id.get(cid, {}).get("name", "").lower().startswith("image")), None)
        if img is not None:
            years.append((int(m.group(0)), img))
    years.sort(reverse=True)                      # newest first
    params["years"] = years
    if opts.get("harmonize"):
        active = _years_active(params, opts)
        if not active:
            logger.warning("Harmonise requested but the service has <2 dated layers "
                           "(%s) — downloading the composite instead.",
                           [y for y, _ in years])
        else:
            extra = (f" (of {len(years)} dated layers, using the newest {len(active)})"
                     if len(years) > len(active) else "")
            logger.info("Harmonising flight years: %s (reference = %d)%s.",
                        [y for y, _ in active], active[0][0], extra)


def native_crs(params, opts):
    return params["crs"]


def default_out_crs(params):
    return params["crs"]


def _years_active(params, opts):
    """The (year, layer_id) list to fetch separately (newest first, capped), or
    [] for a single pass."""
    ys = params.get("years", [])                 # sorted newest-first in prepare()
    if opts.get("harmonize") and len(ys) >= 2:
        return ys[:_MAX_HARMONISE_YEARS]
    return []


# ─────────────────────────────────────────────
# TILE GRID  (origin-anchored, like WMS; ×years when harmonising)
# ─────────────────────────────────────────────
def build_tile_grid(extent_geom, extent_crs, params, opts, logger):
    tile_pixels = int(opts.get("tile_pixels", 1024))
    resolution = float(opts.get("resolution", 0.5))
    req_crs = QgsCoordinateReferenceSystem(params["crs"])
    if not req_crs.isValid():
        raise DownloaderError(f"Request CRS '{params['crs']}' is invalid.")
    ctx = QgsProject.instance().transformContext()
    src_crs = QgsCoordinateReferenceSystem(extent_crs)
    region = QgsGeometry(extent_geom)
    if src_crs != req_crs and region.transform(
            QgsCoordinateTransform(src_crs, req_crs, ctx)) != 0:
        raise DownloaderError("Could not reproject the extent to the request CRS.")
    bb = region.boundingBox()
    step = tile_pixels * resolution
    if step <= 0:
        raise DownloaderError("Tile size in map units is ≤ 0 – check resolution.")

    c0 = math.floor(bb.xMinimum() / step); c1 = math.floor(bb.xMaximum() / step)
    r0 = math.floor(bb.yMinimum() / step); r1 = math.floor(bb.yMaximum() / step)
    logger.info("Grid: %d×%d cells, %.2f map-units/cell (origin-anchored)",
                c1 - c0 + 1, r1 - r0 + 1, step)

    years = _years_active(params, opts)
    passes = years if years else [(None, params.get("sel_show"))]   # (year, layer_id)

    tiles, tid = [], 0
    for row in range(r0, r1 + 1):
        for col in range(c0, c1 + 1):
            xmin = col * step; ymin = row * step
            xmax, ymax = xmin + step, ymin + step
            if not QgsGeometry.fromRect(
                    QgsRectangle(xmin, ymin, xmax, ymax)).intersects(region):
                continue
            for year, lid in passes:
                tiles.append({"id": tid, "col": col, "row": row,
                              "xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
                              "year": year, "layer_id": lid})
                tid += 1
    logger.info("Kept %d tiles%s.", len(tiles),
                f" across {len(passes)} year(s)" if years else "")
    if not tiles:
        raise DownloaderError("No tiles intersect the extent.")
    return tiles


# ─────────────────────────────────────────────
# EXPORT + FETCH
# ─────────────────────────────────────────────
def _export_url(params, opts, tile):
    tile_pixels = int(opts.get("tile_pixels", 1024))
    epsg = params["crs"].split(":")[-1]
    q = {"bbox": f'{tile["xmin"]},{tile["ymin"]},{tile["xmax"]},{tile["ymax"]}',
         "bboxSR": epsg, "imageSR": epsg,
         "size": f"{tile_pixels},{tile_pixels}",
         "format": params.get("format", "png32"), "transparent": "true", "f": "image"}
    if tile.get("layer_id") not in (None, ""):
        q["layers"] = f'show:{tile["layer_id"]}'
    return params["url"] + "/export?" + urllib.parse.urlencode(q)


def fetch_one_tile(params, opts, tile, out_path, logger, attempt=0):
    url = _export_url(params, opts, tile)
    # Logged with credential-looking query values (?token=…) masked, so a shared
    # download.log can't leak them.
    logger.debug("export tile %d (year=%s): %s", tile["id"], tile.get("year"),
                 engine.redact_url(url))
    status, headers, body, err, timed_out = engine.blocking_get(url)
    if timed_out:
        raise TileFetchError("Request timed out.")
    if status == 429:
        raise TileFetchError("HTTP 429.", retry_after=engine.parse_retry_after(
            headers.get("retry-after")), is_throttle=True)
    if status in (500, 502, 503):
        raise TileFetchError(f"HTTP {status}.", retry_after=engine.parse_retry_after(
            headers.get("retry-after")), is_throttle=True)
    if status and status >= 400:
        raise TileFetchError(f"HTTP {status}.")
    if err:
        raise TileFetchError(f"Network error: {err}")
    if not body:
        raise TileFetchError("Empty response body.")
    if body[:1] == b"{":                          # ArcGIS returns errors as JSON
        raise TileFetchError(f"ArcGIS error: {body[:200].decode('utf-8', 'replace')}",
                             is_server_error=True)
    bounds = (tile["xmin"], tile["ymax"], tile["xmax"], tile["ymin"])   # ulx,uly,lrx,lry
    problem = engine.georeference(body, out_path, bounds, params["crs"])
    if problem:
        raise TileFetchError(f"Invalid image: {problem}")
    return out_path


# ─────────────────────────────────────────────
# FINGERPRINT
# ─────────────────────────────────────────────
def fingerprint_parts(params, opts):
    # Only prepare()-independent inputs: prepare() refines params["crs"] to the
    # service SR and fills the per-year layer list, but the engine fingerprints
    # BEFORE prepare() (and the dialog's resume check / engine.run fingerprint
    # freshly-extracted params), so the parts must not depend on what prepare()
    # resolves — same reason WMS keeps its negotiated format out. The harmonise
    # *flag* therefore stands in for the resolved year list.
    return [params["url"], params["crs"], params.get("format", "png32"),
            opts.get("tile_pixels"), opts.get("resolution"),
            "harmonize" if opts.get("harmonize")
            else (params.get("sel_show") or "composite")]


def mosaic_hints(params, opts):
    # RGBA export → keep an alpha band (transparent where a tile had no data).
    return {"add_alpha": True}


# ─────────────────────────────────────────────
# SHARED CACHE  (reusable across jobs / knob-tuning re-runs)
# ─────────────────────────────────────────────
# An export tile has a global identity (origin-anchored col/row for a given grid,
# per requested layer), so overlapping AOIs — and re-runs that only change the
# harmonise match strength or the output file — reuse tiles instead of
# re-downloading. The per-tile layer id goes in the path (so the composite, a
# single year, and a harmonised year's tiles don't collide but a faithful single
# year and that same harmonised year *do* share, being identical requests).
SHAREABLE = True


def shared_signature(params, opts):
    """Identity of the tile source/grid (everything but the tile's position and
    which layer): endpoint, CRS, format, tile size and resolution."""
    return "arcgis\n" + "\n".join([
        params.get("url", ""), params.get("crs", ""), params.get("format", "png32"),
        str(int(opts.get("tile_pixels", 1024))),
        repr(float(opts.get("resolution", 0.5)))])


def shared_rel_path(tile):
    """Path under the shared dir: <layer>/<col>/<row>.tif. `layer` is the
    per-tile layer id (a year's Image layer when harmonising, the selected
    sublayer, or 'composite'). None if the tile predates col/row."""
    if "col" not in tile or "row" not in tile:
        return None
    lid = tile.get("layer_id")
    layer = str(lid) if lid not in (None, "") else "composite"
    return "{}/{}/{}.tif".format(layer, tile["col"], tile["row"])


# ─────────────────────────────────────────────
# HARMONISE FLIGHT YEARS  (compose hook, called by the engine)
# ─────────────────────────────────────────────
_STATS_MAXDIM = 2200     # read each year at ≤ this for the seam statistics
_SEAM_PX = 6             # strip width (in the reduced-res image) either side of a seam
_MIN_SEAM = 400          # too few seam pixels → the years don't really touch; skip


def _read_reduced(vrt):
    ds = gdal.Open(vrt)
    W, H = ds.RasterXSize, ds.RasterYSize
    scale = max(1.0, max(W, H) / _STATS_MAXDIM)
    w, h = max(1, round(W / scale)), max(1, round(H / scale))
    m = gdal.Translate("", ds, format="MEM", width=w, height=h, resampleAlg="average")
    rgb = np.dstack([m.GetRasterBand(i + 1).ReadAsArray().astype(np.float64) for i in range(3)])
    alpha = m.GetRasterBand(4).ReadAsArray() if m.RasterCount >= 4 else np.full((h, w), 255)
    return rgb, alpha > 0


def _dilate(mask, it):
    for _ in range(it):
        d = mask.copy()
        d[1:] |= mask[:-1]; d[:-1] |= mask[1:]
        d[:, 1:] |= mask[:, :-1]; d[:, :-1] |= mask[:, 1:]
        mask = d
    return mask


def _seam_transform(rgb_ref, m_ref, rgb_oth, m_oth):
    """Per-channel (gain, offset) mapping the other year onto the reference,
    estimated on the strips either side of their shared boundary. Identity if
    the two years don't meaningfully touch."""
    seam_o = m_oth & _dilate(m_ref, _SEAM_PX)
    seam_r = m_ref & _dilate(m_oth, _SEAM_PX)
    if seam_o.sum() < _MIN_SEAM or seam_r.sum() < _MIN_SEAM:
        return [(1.0, 0.0)] * 3, 0
    out = []
    for c in range(3):
        ro, rr = rgb_oth[..., c][seam_o], rgb_ref[..., c][seam_r]
        so = ro.std() or 1.0
        g = min(2.0, max(0.5, (rr.std() or 1.0) / so))
        o = rr.mean() - g * ro.mean()
        out.append((g, o))
    return out, int(min(seam_o.sum(), seam_r.sum()))


def _global_transform(rgb_ref, m_ref, rgb_oth, m_oth):
    """Per-channel (gain, offset) matching the other year's *whole-region* stats
    to the reference's — a global brightness/contrast alignment. Blended into the
    seam-local fit by the 'match brightness/contrast' strength."""
    out = []
    for c in range(3):
        ro, rr = rgb_oth[..., c][m_oth], rgb_ref[..., c][m_ref]
        so = ro.std() or 1.0
        g = min(2.0, max(0.5, (rr.std() or 1.0) / so))
        o = rr.mean() - g * ro.mean()
        out.append((g, o))
    return out


def compose_mosaic(done, work_dir, out_path, native_crs, out_crs,
                   resample, cutline, params, opts, logger):
    """Build a seam-harmonised mosaic from per-year tiles. `done` is a list of
    (tile_spec, absolute_path). Returns the output GeoTIFF path."""
    if gdal is None or np is None:
        raise DownloaderError("GDAL/numpy unavailable; cannot harmonise.")
    by_year = {}
    for spec, path in done:
        by_year.setdefault(spec.get("year"), []).append(path)
    years = sorted((y for y in by_year if y is not None), reverse=True)
    if not years:
        raise DownloaderError("Harmonise: no dated tiles to compose.")
    # 0 = colour match at the seam only (keep each year's own brightness/contrast);
    # →1 = also pull the years' overall brightness/contrast together (more uniform,
    # but mutes and can slightly re-expose the seam).
    strength = min(1.0, max(0.0, float(opts.get("harmonize_match", 0.0) or 0.0)))

    tmp = os.path.join(work_dir, "_harmonise")
    os.makedirs(tmp, exist_ok=True)
    vrts = {}
    for y in years:
        v = os.path.join(tmp, f"year_{y}.vrt")
        gdal.BuildVRT(v, by_year[y], options=gdal.BuildVRTOptions(resampleAlg="nearest"))
        vrts[y] = v

    # Reference = the year covering the most of the AOI (its region stays untouched).
    reduced = {y: _read_reduced(vrts[y]) for y in years}
    ref = max(years, key=lambda y: reduced[y][1].mean())
    logger.info("Harmonise: reference year %d (%.0f%% coverage); matching %s.",
                ref, reduced[ref][1].mean() * 100, [y for y in years if y != ref])

    warp_inputs = []
    for y in years:
        if y == ref:
            continue
        (gains, npx) = _seam_transform(reduced[ref][0], reduced[ref][1],
                                       reduced[y][0], reduced[y][1])
        adj = os.path.join(tmp, f"year_{y}_adj.tif")
        if npx == 0:
            logger.info("Harmonise: year %d does not border the reference — left as-is.", y)
            gdal.Translate(adj, vrts[y], options=gdal.TranslateOptions(format="GTiff"))
        else:
            if strength > 0:                      # blend toward a global brightness/contrast match
                gg = _global_transform(reduced[ref][0], reduced[ref][1],
                                       reduced[y][0], reduced[y][1])
                gains = [((1 - strength) * gl + strength * ggl,
                          (1 - strength) * ol + strength * ogl)
                         for (gl, ol), (ggl, ogl) in zip(gains, gg)]
            sp = [[0, 255, o, o + 255 * g] for (g, o) in gains] + [[0, 255, 0, 255]]
            logger.info("Harmonise: year %d → ref on %d seam px (match=%.0f%%), gains=%s.",
                        y, npx, strength * 100, [f"{g:.2f}+{o:.0f}" for g, o in gains])
            gdal.Translate(adj, vrts[y], options=gdal.TranslateOptions(
                format="GTiff", bandList=[1, 2, 3, 4], scaleParams=sp,
                outputType=gdal.GDT_Byte))
        warp_inputs.append(adj)
    warp_inputs.append(vrts[ref])                 # reference last → on top

    tif = out_path or os.path.join(work_dir, "mosaic.tif")
    out_dir = os.path.dirname(tif)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    creation = ["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES",
                "BLOCKXSIZE=256", "BLOCKYSIZE=256", "BIGTIFF=IF_SAFER"]
    warp_alg = "near" if resample == "none" else resample
    kw = dict(format="GTiff", dstSRS=out_crs, srcAlpha=True, dstAlpha=True,
              resampleAlg=warp_alg, creationOptions=creation, multithread=True)
    if cutline:
        kw.update(cutlineDSName=cutline, cropToCutline=True)
    logger.info("Harmonise: compositing %d year(s) → %s (%s → %s).",
                len(years), tif, native_crs, out_crs)
    try:
        ds = gdal.Warp(tif, warp_inputs, options=gdal.WarpOptions(**kw))
    except Exception as e:
        raise DownloaderError(f"Harmonised composite failed: {e}")
    ds.BuildOverviews("AVERAGE", [2, 4, 8, 16])
    ds = None
    return tif

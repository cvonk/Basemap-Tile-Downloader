# -*- coding: utf-8 -*-
"""How each source backend classifies an HTTP response in fetch_one_tile.

QgsBlockingNetworkRequest sets a non-empty error string for ANY HTTP status
>= 400 (it reports ServerExceptionError), so the status-specific handling must
run before the generic network-error raise. These tests pin that ordering: a
429/403/500/503 must surface as a *throttle* (with Retry-After honoured), a
404/204 as a legitimate gap (XYZ/WMTS), and only a status-less failure as a
generic network error. See the review that found the original ordering made
every throttle branch unreachable.
"""

import pytest

from basemap_tile_downloader import engine
from basemap_tile_downloader.engine import TileFetchError
from basemap_tile_downloader.sources import arcgis, wcs, wms, wmts, xyz

PNG = b"\x89PNG fake image bytes"


def _respond(status, body=PNG, headers=None, err=None, timed_out=False):
    """blocking_get replacement returning one canned response. Mimics QGIS: any
    HTTP status >= 400 also carries a non-empty error string."""
    if err is None and status is not None and status >= 400:
        err = f"Error transferring URL - server replied: HTTP {status}"
    return lambda url, timeout_ms=0: (status, headers or {}, body, err, timed_out)


@pytest.fixture
def ok_georeference(monkeypatch):
    """Stub the GDAL georeference step (returns None = success)."""
    monkeypatch.setattr(engine, "georeference", lambda *a, **k: None)


XYZ_PARAMS = {"template": "https://tiles.example/{z}/{x}/{y}.png"}
XYZ_TILE = {"id": 1, "z": 3, "x": 2, "y": 5}

WMS_PARAMS = {"url": "https://wms.example/ogc?map=x", "layers": ["ortho"],
              "styles": [""], "crs": "EPSG:32632", "format": "image/png",
              "extra": {}}
WMS_TILE = {"id": 1, "col": 0, "row": 0,
            "xmin": 0.0, "ymin": 0.0, "xmax": 512.0, "ymax": 512.0}

WMTS_PARAMS = {"caps_url": "https://wmts.example/caps.xml", "layer": "ortho",
               "style": "", "format": "image/png", "tile_matrix_set": "EPSG:3857",
               "tms_crs": "EPSG:3857", "kvp_base": "https://wmts.example/wmts",
               "rest_template": None,
               "matrices": [{"id": "0", "scale": 1.0, "topx": 0.0, "topy": 0.0,
                             "tsx": 100.0, "tsy": 100.0, "mw": 4, "mh": 4}]}
WMTS_TILE = {"id": 1, "m": 0, "col": 1, "row": 1}

ARCGIS_PARAMS = {"url": "https://gis.example/arcgis/rest/services/Ortho/MapServer",
                 "crs": "EPSG:31254", "format": "png32", "sel_show": None,
                 "years": []}
ARCGIS_TILE = {"id": 1, "col": 0, "row": 0, "xmin": 0.0, "ymin": 0.0,
               "xmax": 512.0, "ymax": 512.0, "year": None, "layer_id": None}

WCS_PARAMS = {"url": "https://wcs.example/geoserver/ows", "coverage": "dtm:2.5m",
              "crs": "EPSG:25832", "format": "GeoTIFF", "bands": 1,
              "nodata": -9999.0, "native_res": 2.5,
              "src_bounds": (0.0, 0.0, 10000.0, 10000.0)}
WCS_TILE = {"id": 1, "col": 0, "row": 0,
            "xmin": 0.0, "ymin": 0.0, "xmax": 512.0, "ymax": 512.0}


class _Log:
    def debug(self, *a): pass
    def info(self, *a): pass
    def warning(self, *a): pass
    def error(self, *a): pass


LOG = _Log()


def _fetch(source, params, tile, monkeypatch, **response):
    monkeypatch.setattr(engine, "blocking_get", _respond(**response))
    return source.fetch_one_tile(params, {}, tile, "out.tif", LOG)


def _fetch_error(source, params, tile, monkeypatch, **response):
    monkeypatch.setattr(engine, "blocking_get", _respond(**response))
    with pytest.raises(TileFetchError) as exc:
        source.fetch_one_tile(params, {}, tile, "out.tif", LOG)
    return exc.value


# ── throttle statuses must be throttles, not generic errors ────────────────────
@pytest.mark.parametrize("status", [429, 403, 500, 503])
def test_xyz_throttle_statuses(monkeypatch, status):
    e = _fetch_error(xyz, XYZ_PARAMS, XYZ_TILE, monkeypatch, status=status)
    assert e.is_throttle


@pytest.mark.parametrize("status", [429, 500, 503])
def test_wms_throttle_statuses(monkeypatch, status):
    e = _fetch_error(wms, WMS_PARAMS, WMS_TILE, monkeypatch, status=status)
    assert e.is_throttle


@pytest.mark.parametrize("status", [429, 403, 500, 503])
def test_wmts_throttle_statuses(monkeypatch, status):
    e = _fetch_error(wmts, WMTS_PARAMS, WMTS_TILE, monkeypatch, status=status)
    assert e.is_throttle


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_arcgis_throttle_statuses(monkeypatch, status):
    e = _fetch_error(arcgis, ARCGIS_PARAMS, ARCGIS_TILE, monkeypatch, status=status)
    assert e.is_throttle


def test_retry_after_header_is_honoured(monkeypatch):
    e = _fetch_error(xyz, XYZ_PARAMS, XYZ_TILE, monkeypatch,
                     status=429, headers={"retry-after": "120"})
    assert e.is_throttle and e.retry_after == 120.0


# ── gaps: 404/204 are empty tiles for XYZ/WMTS, an error for WMS ───────────────
@pytest.mark.parametrize("status", [404, 204])
def test_xyz_gap_statuses(monkeypatch, status):
    assert _fetch(xyz, XYZ_PARAMS, XYZ_TILE, monkeypatch, status=status) is None


@pytest.mark.parametrize("status", [404, 204])
def test_wmts_gap_statuses(monkeypatch, status):
    assert _fetch(wmts, WMTS_PARAMS, WMTS_TILE, monkeypatch, status=status) is None


def test_wms_404_is_an_error_not_a_gap(monkeypatch):
    e = _fetch_error(wms, WMS_PARAMS, WMS_TILE, monkeypatch, status=404)
    assert "HTTP 404" in str(e)
    assert not e.is_throttle and not e.is_server_error


# ── other classifications ──────────────────────────────────────────────────────
def test_other_4xx_is_a_plain_error(monkeypatch):
    e = _fetch_error(xyz, XYZ_PARAMS, XYZ_TILE, monkeypatch, status=418)
    assert "HTTP 418" in str(e)
    assert not e.is_throttle and not e.is_server_error


def test_statusless_network_error(monkeypatch):
    e = _fetch_error(xyz, XYZ_PARAMS, XYZ_TILE, monkeypatch,
                     status=None, body=b"", err="Connection refused")
    assert "Network error" in str(e)
    assert not e.is_throttle


def test_timeout_wins_over_everything(monkeypatch):
    e = _fetch_error(xyz, XYZ_PARAMS, XYZ_TILE, monkeypatch,
                     status=None, body=b"", err="timeout", timed_out=True)
    assert "timed out" in str(e).lower()


def test_arcgis_404_is_an_error_not_a_gap(monkeypatch):
    e = _fetch_error(arcgis, ARCGIS_PARAMS, ARCGIS_TILE, monkeypatch, status=404)
    assert "HTTP 404" in str(e)
    assert not e.is_throttle and not e.is_server_error


def test_arcgis_json_error_body_is_server_error(monkeypatch):
    # ArcGIS reports export failures as a JSON body even with f=image, HTTP 200.
    body = b'{"error":{"code":500,"message":"Unable to complete operation."}}'
    e = _fetch_error(arcgis, ARCGIS_PARAMS, ARCGIS_TILE, monkeypatch,
                     status=200, body=body)
    assert e.is_server_error and "Unable to complete" in str(e)


def test_arcgis_empty_body_is_an_error(monkeypatch):
    e = _fetch_error(arcgis, ARCGIS_PARAMS, ARCGIS_TILE, monkeypatch,
                     status=200, body=b"")
    assert "Empty response" in str(e)


def test_wms_service_exception_is_server_error(monkeypatch):
    body = (b'<?xml version="1.0"?><ServiceExceptionReport>'
            b"<ServiceException>msDrawMap(): failed</ServiceException>"
            b"</ServiceExceptionReport>")
    e = _fetch_error(wms, WMS_PARAMS, WMS_TILE, monkeypatch, status=200, body=body)
    assert e.is_server_error and "msDrawMap" in str(e)


def test_success_paths(monkeypatch, ok_georeference):
    assert _fetch(xyz, XYZ_PARAMS, XYZ_TILE, monkeypatch, status=200) == "out.tif"
    assert _fetch(wms, WMS_PARAMS, WMS_TILE, monkeypatch, status=200) == "out.tif"
    assert _fetch(wmts, WMTS_PARAMS, WMTS_TILE, monkeypatch, status=200) == "out.tif"
    assert _fetch(arcgis, ARCGIS_PARAMS, ARCGIS_TILE, monkeypatch, status=200) == "out.tif"


def test_empty_2xx_body_is_a_gap_for_xyz(monkeypatch):
    assert _fetch(xyz, XYZ_PARAMS, XYZ_TILE, monkeypatch, status=200, body=b"") is None


def test_empty_2xx_body_is_an_error_for_wms(monkeypatch):
    e = _fetch_error(wms, WMS_PARAMS, WMS_TILE, monkeypatch, status=200, body=b"")
    assert "Empty response" in str(e)


# ── WMTS KVP base keeps auth query params, drops protocol ones ─────────────────
def test_wmts_kvp_base_keeps_api_key():
    base = wmts._kvp_base(
        "https://wmts.example/service?SERVICE=WMTS&REQUEST=GetCapabilities"
        "&VERSION=1.0.0&apikey=SECRET")
    assert base == "https://wmts.example/service?apikey=SECRET"


def test_wmts_kvp_base_plain_url_unchanged():
    assert wmts._kvp_base("https://wmts.example/1.0.0/caps.xml") == \
        "https://wmts.example/1.0.0/caps.xml"


def test_wmts_kvp_tile_url_appends_with_ampersand():
    params = dict(WMTS_PARAMS, kvp_base="https://wmts.example/service?apikey=SECRET")
    url = wmts._tile_url(params, WMTS_PARAMS["matrices"][0], 1, 2)
    assert url.startswith("https://wmts.example/service?apikey=SECRET&")
    assert "TILEROW=1" in url and "TILECOL=2" in url


# ── WCS: same classification contract as WMS, plus its own "off the edge" gap ──
@pytest.mark.parametrize("status", [429, 500, 503])
def test_wcs_throttle_statuses(monkeypatch, status):
    e = _fetch_error(wcs, WCS_PARAMS, WCS_TILE, monkeypatch, status=status)
    assert e.is_throttle


def test_wcs_404_is_an_error_not_a_gap(monkeypatch):
    e = _fetch_error(wcs, WCS_PARAMS, WCS_TILE, monkeypatch, status=404)
    assert "HTTP 404" in str(e)
    assert not e.is_throttle and not e.is_server_error


def test_wcs_empty_2xx_body_is_an_error(monkeypatch):
    e = _fetch_error(wcs, WCS_PARAMS, WCS_TILE, monkeypatch, status=200, body=b"")
    assert "Empty response" in str(e)


def test_wcs_service_exception_is_a_server_error(monkeypatch):
    # A ServiceException arrives as HTTP 200 with an XML body; it is usually the
    # service momentarily failing to render, so it must reach the back-pressure
    # budget rather than burning the tile's error budget.
    body = (b'<?xml version="1.0"?><ServiceExceptionReport>'
            b'<ServiceException code="NoApplicableCode">Failed to read the '
            b'coverage store</ServiceException></ServiceExceptionReport>')
    e = _fetch_error(wcs, WCS_PARAMS, WCS_TILE, monkeypatch, status=200, body=body)
    assert e.is_server_error and not e.is_throttle
    assert "Failed to read the coverage store" in str(e)


@pytest.mark.parametrize("phrase", [
    b"The requested bounding box does not intersect the coverage",
    b"Requested envelope does not overlap the coverage envelope",
    b"No data available for the requested area",
])
def test_wcs_off_coverage_exception_is_a_gap(monkeypatch, phrase):
    # A boundary tile the service has no data for is a legitimate gap: recording
    # it as empty stops the engine retrying a request that can never succeed.
    body = (b'<?xml version="1.0"?><ServiceExceptionReport><ServiceException>'
            + phrase + b'</ServiceException></ServiceExceptionReport>')
    assert _fetch(wcs, WCS_PARAMS, WCS_TILE, monkeypatch,
                  status=200, body=body) is None


def test_wcs_success_returns_the_tile_path(monkeypatch, ok_georeference):
    assert _fetch(wcs, WCS_PARAMS, WCS_TILE, monkeypatch,
                  status=200, body=b"II*\x00 fake tiff bytes") == "out.tif"


def test_wcs_tile_bytes_are_written_without_a_byte_predictor(monkeypatch):
    """Coverage samples are often Float32, where TIFF predictor 2 is the wrong
    transform — the backend must override the engine's imagery default."""
    seen = {}

    def _spy(body, out_tif, bounds, srs, creation_options=None):
        seen["opts"] = creation_options
        return None

    monkeypatch.setattr(engine, "georeference", _spy)
    _fetch(wcs, WCS_PARAMS, WCS_TILE, monkeypatch,
           status=200, body=b"II*\x00 fake tiff bytes")
    assert seen["opts"] == ["COMPRESS=DEFLATE"]
    assert not any("PREDICTOR" in o for o in seen["opts"])

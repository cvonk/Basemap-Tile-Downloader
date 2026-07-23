# -*- coding: utf-8 -*-
"""WCS backend: DescribeCoverage parsing, CRS normalisation and GetCoverage URLs.

The capabilities fixture below is a trimmed copy of a real WCS 1.0.0
DescribeCoverage response (GeoServer), so the parser is pinned against what a
service actually emits — nested GML, a lonLatEnvelope that must NOT be mistaken
for the spatial envelope, and the offset vectors that carry the pixel size.
"""

import urllib.parse

import pytest

from basemap_tile_downloader.engine import DownloaderError
from basemap_tile_downloader.sources import wcs

DESCRIBE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<wcs:CoverageDescription xmlns:wcs="http://www.opengis.net/wcs"
    xmlns:gml="http://www.opengis.net/gml" version="1.0.0">
  <wcs:CoverageOffering>
    <wcs:description>Generated from GeoTIFF</wcs:description>
    <wcs:name>p_bz-Elevation:DigitalTerrainModel-2.5m</wcs:name>
    <wcs:label>Digitales Gelaendemodell</wcs:label>
    <wcs:lonLatEnvelope srsName="urn:ogc:def:crs:OGC:1.3:CRS84">
      <gml:pos>10.36155279782117 46.18071682526831</gml:pos>
      <gml:pos>12.533622950030843 47.13234549695491</gml:pos>
    </wcs:lonLatEnvelope>
    <wcs:domainSet>
      <wcs:spatialDomain>
        <gml:Envelope srsName="EPSG:25832">
          <gml:pos>604998.7500008999 5119998.749878633</gml:pos>
          <gml:pos>768201.2500008999 5220801.249878633</gml:pos>
        </gml:Envelope>
        <gml:RectifiedGrid dimension="2" srsName="EPSG:25832">
          <gml:limits>
            <gml:GridEnvelope>
              <gml:low>0 0</gml:low>
              <gml:high>65280 40320</gml:high>
            </gml:GridEnvelope>
          </gml:limits>
          <gml:axisName>E</gml:axisName>
          <gml:axisName>N</gml:axisName>
          <gml:origin><gml:pos>605000.0 5220799.99</gml:pos></gml:origin>
          <gml:offsetVector>2.5 0.0</gml:offsetVector>
          <gml:offsetVector>0.0 -2.5</gml:offsetVector>
        </gml:RectifiedGrid>
      </wcs:spatialDomain>
    </wcs:domainSet>
    <wcs:supportedCRSs>
      <wcs:requestResponseCRSs>EPSG:25832</wcs:requestResponseCRSs>
    </wcs:supportedCRSs>
    <wcs:supportedFormats nativeFormat="GeoTIFF">
      <wcs:formats>GeoTIFF</wcs:formats>
      <wcs:formats>GIF</wcs:formats>
      <wcs:formats>JPEG</wcs:formats>
      <wcs:formats>PNG</wcs:formats>
      <wcs:formats>TIFF</wcs:formats>
    </wcs:supportedFormats>
  </wcs:CoverageOffering>
</wcs:CoverageDescription>
"""

COVERAGE = "p_bz-Elevation:DigitalTerrainModel-2.5m"


# ── DescribeCoverage parsing ──────────────────────────────────────────────────
def test_parses_the_spatial_envelope_not_the_lonlat_one():
    info = wcs.parse_coverage_description(DESCRIBE_XML, COVERAGE)
    assert info["crs"] == "EPSG:25832"
    assert info["bounds"] == pytest.approx(
        (604998.75, 5119998.7498786, 768201.25, 5220801.2498786))


def test_parses_native_resolution_from_the_offset_vectors():
    assert wcs.parse_coverage_description(DESCRIBE_XML, COVERAGE)["native_res"] == 2.5


def test_parses_supported_crss_and_formats():
    info = wcs.parse_coverage_description(DESCRIBE_XML, COVERAGE)
    assert info["crss"] == ["EPSG:25832"]
    assert info["formats"][0] == "GeoTIFF"


def test_picks_the_named_offering_when_several_are_returned():
    two = DESCRIBE_XML.replace(
        b"<wcs:CoverageOffering>",
        b"<wcs:CoverageOffering><wcs:name>other:coverage</wcs:name>"
        b"<gml:Envelope srsName=\"EPSG:4326\"><gml:pos>0 0</gml:pos>"
        b"<gml:pos>1 1</gml:pos></gml:Envelope></wcs:CoverageOffering>"
        b"<wcs:CoverageOffering>", 1)
    assert wcs.parse_coverage_description(two, COVERAGE)["crs"] == "EPSG:25832"


def test_missing_offering_is_a_clear_error():
    empty = (b'<wcs:CoverageDescription xmlns:wcs="http://www.opengis.net/wcs"/>')
    with pytest.raises(DownloaderError) as exc:
        wcs.parse_coverage_description(empty, COVERAGE)
    assert "no CoverageOffering" in str(exc.value)


def test_unparseable_xml_is_a_downloader_error():
    with pytest.raises(DownloaderError) as exc:
        wcs.parse_coverage_description(b"<not xml", COVERAGE)
    assert "Cannot parse" in str(exc.value)


# ── CRS spellings ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", [
    "EPSG:25832",
    "epsg:25832",
    "urn:ogc:def:crs:EPSG::25832",
    "urn:ogc:def:crs:EPSG:6.6:25832",
    "http://www.opengis.net/def/crs/EPSG/0/25832",
])
def test_crs_spellings_normalise_to_an_authid(raw):
    assert wcs.normalise_crs(raw) == "EPSG:25832"


def test_unknown_crs_spelling_is_passed_through():
    assert wcs.normalise_crs(" CRS:84 ") == "CRS:84"
    assert wcs.normalise_crs(None) == ""


# ── format negotiation ────────────────────────────────────────────────────────
class _Log:
    def debug(self, *a): pass
    def info(self, *a): pass
    def warning(self, *a): pass
    def error(self, *a): pass


@pytest.mark.parametrize("formats,expected", [
    (["GIF", "JPEG", "PNG", "TIFF", "GeoTIFF"], "GeoTIFF"),
    (["JPEG", "PNG", "TIFF"], "TIFF"),           # lossless TIFF beats PNG
    (["JPEG", "PNG"], "PNG"),                    # never JPEG for measured values
    (["image/tiff;application=geotiff"], "image/tiff;application=geotiff"),
])
def test_format_preference_is_lossless_first(formats, expected):
    assert wcs._choose_format(formats, _Log()) == expected


# ── request URLs ──────────────────────────────────────────────────────────────
def _query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


def test_describe_url_carries_the_1_0_0_request():
    q = _query(wcs.describe_url("https://wcs.example/ows", COVERAGE))
    assert q["SERVICE"] == "WCS" and q["VERSION"] == "1.0.0"
    assert q["REQUEST"] == "DescribeCoverage" and q["COVERAGE"] == COVERAGE


def test_describe_url_preserves_existing_query_params():
    q = _query(wcs.describe_url("https://wcs.example/ows?apikey=SECRET", COVERAGE))
    assert q["apikey"] == "SECRET"


PARAMS = {"url": "https://wcs.example/ows", "coverage": COVERAGE,
          "crs": "EPSG:25832", "format": "GeoTIFF"}
TILE = {"id": 0, "col": 290, "row": 2015,
        "xmin": 742400.0, "ymin": 5158400.0, "xmax": 744960.0, "ymax": 5160960.0}


def test_getcoverage_url_is_a_bbox_plus_size_request():
    q = _query(wcs._getcoverage_url(PARAMS, {"tile_pixels": 1024}, TILE))
    assert q["REQUEST"] == "GetCoverage" and q["VERSION"] == "1.0.0"
    assert q["COVERAGE"] == COVERAGE and q["CRS"] == "EPSG:25832"
    assert q["WIDTH"] == "1024" and q["HEIGHT"] == "1024"
    assert q["FORMAT"] == "GeoTIFF"


def test_getcoverage_bbox_is_always_x_then_y():
    # WCS 1.0.0 fixes the BBOX axis order at (x, y) whatever the CRS declares —
    # no WMS-1.3.0-style swap for northing-first CRSs.
    q = _query(wcs._getcoverage_url(dict(PARAMS, crs="EPSG:4326"),
                                    {"tile_pixels": 512}, TILE))
    assert q["BBOX"] == "742400.0,5158400.0,744960.0,5160960.0"


def test_first_attempt_has_no_cache_buster():
    assert "_btd_cb" not in wcs._getcoverage_url(PARAMS, {}, TILE, attempt=0)


def test_retries_add_a_per_attempt_cache_buster():
    url = wcs._getcoverage_url(PARAMS, {}, TILE, attempt=3)
    assert _query(url)["_btd_cb"] == "3"


def test_case_of_existing_params_is_preserved():
    # Some services are picky about parameter case; a URL that already spells
    # "service" lowercase must not gain a second "SERVICE".
    url = wcs._getcoverage_url(
        dict(PARAMS, url="https://wcs.example/ows?service=WCS"), {}, TILE)
    assert url.count("service") + url.count("SERVICE") == 1


# ── grid resolution fallback ──────────────────────────────────────────────────
def test_grid_resolution_prefers_the_requested_one():
    assert wcs._grid_resolution({"native_res": 2.5}, {"resolution": 10.0}) == 10.0


def test_grid_resolution_falls_back_to_native():
    assert wcs._grid_resolution({"native_res": 2.5}, {}) == 2.5
    assert wcs._grid_resolution({"native_res": 2.5}, {"resolution": 0}) == 2.5


def test_grid_resolution_last_resort_default():
    assert wcs._grid_resolution({}, {}) == 0.5


# ── single-band coverages keep their nodata instead of gaining alpha ──────────
def test_single_band_coverage_preserves_nodata():
    hints = wcs.mosaic_hints({"bands": 1, "nodata": -9999.0}, {})
    assert hints == {"add_alpha": False, "nodata": -9999.0}


def test_rgb_coverage_gets_an_alpha_band():
    hints = wcs.mosaic_hints({"bands": 3, "nodata": 0.0}, {})
    assert hints == {"add_alpha": True, "nodata": None}


# ── shared cache identity ─────────────────────────────────────────────────────
def test_shared_signature_changes_with_resolution():
    a = wcs.shared_signature(PARAMS, {"tile_pixels": 1024, "resolution": 2.5})
    b = wcs.shared_signature(PARAMS, {"tile_pixels": 1024, "resolution": 10.0})
    assert a != b


def test_shared_signature_is_stable_for_the_same_grid():
    opts = {"tile_pixels": 1024, "resolution": 2.5}
    assert wcs.shared_signature(PARAMS, opts) == wcs.shared_signature(dict(PARAMS), opts)


def test_shared_rel_path_uses_the_global_col_row():
    assert wcs.shared_rel_path(TILE) == "290/2015.tif"


def test_shared_rel_path_none_without_col_row():
    assert wcs.shared_rel_path({"id": 0, "xmin": 0}) is None

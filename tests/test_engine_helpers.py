# -*- coding: utf-8 -*-
"""Pure-Python engine helpers: log-URL credential masking, and the per-job
cache keys (readable output name + path hash so same-named outputs in different
folders can't wipe each other's queue). Also pins that the ArcGIS fingerprint
does not depend on anything prepare() resolves — the engine fingerprints BEFORE
prepare(), and the dialog's resume check fingerprints freshly-extracted params,
so the two must always agree (the bug this guards against re-showed the
overwrite/ToS prompts on every resume of a harmonised job)."""

from basemap_tile_downloader import engine
from basemap_tile_downloader.sources import arcgis


# ── redact_url ─────────────────────────────────────────────────────────────────
def test_redact_url_masks_credential_params():
    url = "https://wmts.example/service?apikey=SECRET&layer=ortho&token=ALSO"
    red = engine.redact_url(url)
    assert "SECRET" not in red and "ALSO" not in red
    assert "layer=ortho" in red and "apikey=REDACTED" in red


def test_redact_url_without_credentials_is_unchanged():
    for url in ("https://tiles.example/3/2/5.png",
                "https://wms.example/ogc?map=/ms/WMS.map&LAYERS=a,b"):
        assert engine.redact_url(url) == url


def test_redact_url_survives_garbage():
    assert engine.redact_url("not a url at all") == "not a url at all"


# ── cache keys ─────────────────────────────────────────────────────────────────
def test_cache_key_distinct_for_same_basename_in_different_dirs():
    a = engine.cache_key_for(r"C:\jobs\a\ortho.tif", False, "fp")
    b = engine.cache_key_for(r"C:\jobs\b\ortho.tif", False, "fp")
    assert a != b
    assert a.startswith("ortho-") and b.startswith("ortho-")


def test_cache_key_stable_for_same_path():
    p = r"C:\jobs\a\ortho.tif"
    assert engine.cache_key_for(p, False, "fp") == engine.cache_key_for(p, False, "fp")


def test_cache_key_temporary_uses_fingerprint():
    assert engine.cache_key_for(None, True, "fp123") == "fp123"
    assert engine.cache_key_for(r"C:\x\y.tif", True, "fp123") == "fp123"


def test_legacy_cache_key_is_the_plain_basename():
    assert engine.legacy_cache_key(r"C:\jobs\a\ortho.tif", False) == "ortho"
    assert engine.legacy_cache_key(None, True) is None


# ── ArcGIS fingerprint must ignore what prepare() resolves ─────────────────────
ARCGIS_PRE = {"url": "https://gis.example/rest/services/Ortho/MapServer",
              "crs": "EPSG:31254", "format": "png32", "sel_show": None,
              "years": []}


def test_arcgis_fingerprint_ignores_resolved_years():
    opts = {"tile_pixels": 1024, "resolution": 0.5, "harmonize": True}
    post = dict(ARCGIS_PRE, years=[(2024, 5), (2022, 9), (2019, 13)])
    assert (arcgis.fingerprint_parts(ARCGIS_PRE, opts)
            == arcgis.fingerprint_parts(post, opts))


def test_arcgis_fingerprint_still_distinguishes_harmonise():
    base = {"tile_pixels": 1024, "resolution": 0.5}
    assert (arcgis.fingerprint_parts(ARCGIS_PRE, dict(base, harmonize=True))
            != arcgis.fingerprint_parts(ARCGIS_PRE, dict(base, harmonize=False)))

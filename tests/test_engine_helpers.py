# -*- coding: utf-8 -*-
"""Pure-Python engine helpers: log-URL credential masking, and the per-job
cache keys (readable output name + path hash so same-named outputs in different
folders can't wipe each other's queue). Also pins that the ArcGIS fingerprint
does not depend on anything prepare() resolves — the engine fingerprints BEFORE
prepare(), and the dialog's resume check fingerprints freshly-extracted params,
so the two must always agree (the bug this guards against re-showed the
overwrite/ToS prompts on every resume of a harmonised job)."""

import os

from basemap_tile_downloader import engine
from basemap_tile_downloader.sources import arcgis

# Platform-native paths: the plugin only ever sees paths in the running OS's
# own separators, and os.path.basename doesn't split Windows separators on the
# POSIX CI runners.
PATH_A = os.path.join(os.sep, "jobs", "a", "ortho.tif")
PATH_B = os.path.join(os.sep, "jobs", "b", "ortho.tif")


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
    a = engine.cache_key_for(PATH_A, False, "fp")
    b = engine.cache_key_for(PATH_B, False, "fp")
    assert a != b
    assert a.startswith("ortho-") and b.startswith("ortho-")


def test_cache_key_stable_for_same_path():
    assert (engine.cache_key_for(PATH_A, False, "fp")
            == engine.cache_key_for(PATH_A, False, "fp"))


def test_cache_key_temporary_uses_fingerprint():
    assert engine.cache_key_for(None, True, "fp123") == "fp123"
    assert engine.cache_key_for(PATH_A, True, "fp123") == "fp123"


def test_legacy_cache_key_is_the_plain_basename():
    assert engine.legacy_cache_key(PATH_A, False) == "ortho"
    assert engine.legacy_cache_key(None, True) is None


# ── cache accounting (dialog's usage line + purge button) ─────────────────────
def _make_cache(root):
    """A cache tree shaped like a real one: two export folders plus the shared
    tile store."""
    for rel, size in (
            ("job-a/tiles/tile_000001.tif", 1000),
            ("job-a/tiles/tile_000002.tif", 2000),
            ("job-a/tiles.sqlite", 500),
            ("job-b/tiles/tile_000001.tif", 4000),
            ("shared/abc123/18/1/2.tif", 8000),
            ("shared/abc123/18/1/3.tif", 8000)):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    return 23500


def test_cache_usage_totals_and_split(tmp_path):
    total = _make_cache(tmp_path)
    u = engine.cache_usage(str(tmp_path))
    assert u["done"] and u["total"] == total and u["files"] == 6
    assert u["shared"] == 16000
    assert u["jobs"] == {"job-a": 3500, "job-b": 4000}


def test_cache_usage_missing_root_is_zero(tmp_path):
    u = engine.cache_usage(str(tmp_path / "nope"))
    assert u["done"] and u["total"] == 0 and u["jobs"] == {}


def test_iter_cache_usage_yields_progress_then_final(tmp_path):
    _make_cache(tmp_path)
    # chunk=1 forces a yield per file, so the scan can be stepped by a UI timer.
    steps = list(engine.iter_cache_usage(str(tmp_path), chunk=1))
    assert len(steps) > 1
    assert steps[-1]["done"] and steps[-1]["total"] == 23500


def test_purge_cache_frees_everything(tmp_path):
    root = tmp_path / "__btdcache__"
    root.mkdir()
    total = _make_cache(root)
    freed, errors = engine.purge_cache(str(root))
    assert freed == total and errors == []
    assert not root.exists()


def test_purge_cache_on_missing_root_is_a_no_op(tmp_path):
    assert engine.purge_cache(str(tmp_path / "nope")) == (0, [])


def test_format_size():
    assert engine.format_size(0) == "0 B"
    assert engine.format_size(999) == "999 B"
    assert engine.format_size(1536) == "1.5 KB"
    assert engine.format_size(5 * 1024**3) == "5.0 GB"


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

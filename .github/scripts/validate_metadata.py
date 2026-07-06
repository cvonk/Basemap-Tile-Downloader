#!/usr/bin/env python3
"""Validate the plugin's metadata.txt has the fields QGIS requires."""
import configparser
import os
import sys

PATH = "basemap_tile_downloader/metadata.txt"
PKG_DIR = os.path.dirname(PATH)
REQUIRED = ["name", "qgisMinimumVersion", "description",
            "version", "author", "email"]

cp = configparser.ConfigParser(interpolation=None)   # tolerate % in values
if not cp.read(PATH, encoding="utf-8"):
    sys.exit(f"ERROR: could not read {PATH}")
if not cp.has_section("general"):
    sys.exit(f"ERROR: {PATH} has no [general] section")

missing = [k for k in REQUIRED if not cp.get("general", k, fallback="").strip()]
if missing:
    sys.exit(f"ERROR: metadata.txt missing required field(s): {', '.join(missing)}")

# plugins.qgis.org parses metadata.txt with configparser's DEFAULT interpolation
# (BasicInterpolation), under which a literal '%' in any value is an error unless
# doubled ('%%'). Our runtime reader uses interpolation=None and would not catch
# that, so validate the strict way here — this is what actually rejects uploads.
strict = configparser.ConfigParser()   # BasicInterpolation, like the QGIS repo
strict.read(PATH, encoding="utf-8")
for key in strict.options("general"):
    try:
        strict.get("general", key)     # forces interpolation; raises on a bad '%'
    except configparser.InterpolationError as e:
        sys.exit(f"ERROR: metadata.txt field '{key}' fails the QGIS Plugin "
                 f"Repository's percent interpolation — write a literal '%' as "
                 f"'%%': {e}")

# plugins.qgis.org requires a LICENSE file *inside* the plugin package (the repo
# root LICENSE isn't in the git-archived package subtree), so check it ships here.
if not os.path.isfile(os.path.join(PKG_DIR, "LICENSE")):
    sys.exit(f"ERROR: {PKG_DIR}/LICENSE is missing — the QGIS Plugin Repository "
             f"requires a LICENSE file inside the plugin package.")

print(f"metadata.txt OK — {cp.get('general', 'name')} "
      f"v{cp.get('general', 'version')}")

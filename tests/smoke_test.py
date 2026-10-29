"""Smoke test for an installed AVL GeoViz extension. Run through
tests/run_smoke.sh, which installs a built zip into a throwaway Blender
profile first:

    tests/run_smoke.sh <blender> <zip> [--network]

Checks that every bundled dependency imports and that each tool runs once
on small inline data. --network also downloads Natural Earth boundaries.
"""

import importlib
import json
import os
import sys
import tempfile

import addon_utils
import bpy

PKG = "bl_ext.user_default.avl_geoviz"
NETWORK = "--network" in sys.argv
failures = []


def check(name, fn):
    try:
        detail = fn()
        print(f"ok    {name}" + (f": {detail}" if detail else ""))
    except Exception as exc:  # report every failure, not just the first
        import traceback

        traceback.print_exc()
        failures.append(name)
        print(f"FAIL  {name}: {exc}")


def enabled():
    loaded_default, loaded_state = addon_utils.check(PKG)
    assert loaded_state, f"{PKG} isn't enabled"


def imports():
    names = ["geopandas", "geonamescache", "matplotlib", "rasterio", "osmnx", "osm2geojson",
             "gpx", "PIL", "scipy", "databpy", "nodebpy", "d3blender"]
    versions = []
    for name in names:
        module = importlib.import_module(name)
        versions.append(f"{name} {getattr(module, '__version__', '?')}")
    return ", ".join(versions)


def write(suffix, text):
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def geojson_import():
    path = write(".geojson", json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "line"},
         "geometry": {"type": "LineString", "coordinates": [[-88.2, 40.1], [-87.6, 41.9], [-86.2, 39.8]]}},
        {"type": "Feature", "properties": {"name": "area"},
         "geometry": {"type": "Polygon", "coordinates": [[[-90, 38], [-88, 38], [-88, 40], [-90, 38]]]}},
        {"type": "Feature", "properties": {"name": "spot"},
         "geometry": {"type": "Point", "coordinates": [-88.2, 40.1]}},
    ]}))
    assert bpy.ops.object.import_geojson_wireframes(filepath=path) == {"FINISHED"}
    return f"{len(bpy.data.objects)} objects in the scene"


def gpx_import():
    path = write(".gpx", """<?xml version="1.0"?>
<gpx version="1.1" creator="smoke" xmlns="http://www.topografix.com/GPX/1/1">
  <wpt lat="40.11" lon="-88.24"><name>Start</name></wpt>
  <trk><name>Loop</name><trkseg>
    <trkpt lat="40.110" lon="-88.240"><ele>220</ele><time>2026-01-01T00:00:00Z</time></trkpt>
    <trkpt lat="40.115" lon="-88.230"><ele>222</ele><time>2026-01-01T00:01:00Z</time></trkpt>
    <trkpt lat="40.120" lon="-88.235"><ele>221</ele><time>2026-01-01T00:02:00Z</time></trkpt>
  </trkseg></trk>
</gpx>""")
    counts = []
    for _ in range(2):  # a rerun updates the same objects
        assert bpy.ops.object.import_gpx(filepath=path) == {"FINISHED"}
        counts.append(len(bpy.data.objects))
    assert counts[0] == counts[1], f"rerun changed the object count: {counts}"
    return f"{counts[0]} objects after each of two imports"


def emission_generator():
    import databpy as db
    import numpy as np

    x = np.linspace(0.0, 10.0, 50)
    source = db.create_object(np.zeros((50, 3)), name="SmokeSeries")
    db.store_named_attribute(source, x, "x")
    db.store_named_attribute(source, x ** 2, "y")
    result = bpy.ops.object.cmet_add(source_object_name=source.name, num_spheres=20, result_type="MESH")
    assert result == {"FINISHED"}, result
    obj = bpy.context.active_object
    status = obj.cmet_settings.last_status
    assert not status.startswith("ERROR"), status
    assert len(obj.data.vertices) > 0, f"no points generated ({status})"
    return f"{len(obj.data.vertices)} points, status: {status}"


def pair_names_node_group():
    module = sys.modules[PKG + ".country_pair_batch_emitter"]
    group = module._build_pair_names_node_group("Smoke Pair Names", ["USA-CAN", "CAN-USA"])
    group = group or bpy.data.node_groups["Smoke Pair Names"]
    return f"{len(group.nodes)} nodes"


def natural_earth():
    assert bpy.ops.object.refresh_geo_country_list() == {"FINISHED"}
    return f"{len(bpy.context.scene.country_wireframe_settings.country_items)} countries"


check("extension enabled", enabled)
check("bundled dependencies import", imports)
check("GeoJSON import", geojson_import)
check("GPX import", gpx_import)
check("constant-mass emission generator", emission_generator)
check("pair-names node group (nodebpy)", pair_names_node_group)
if NETWORK:
    check("Natural Earth country list (network)", natural_earth)

print(f"\n{len(failures)} failure(s)" + (f": {', '.join(failures)}" if failures else ""))
sys.exit(1 if failures else 0)

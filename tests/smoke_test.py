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


def globe_flat_node_groups():
    import math

    module = sys.modules[PKG + ".country_wireframes"]
    coll = bpy.data.collections.new("Smoke Globe")
    bpy.context.scene.collection.children.link(coll)
    module._create_reference_globe(coll, radius=10.0, resolution=16)
    to_flat = bpy.data.node_groups["Geo Globe To Flat"]
    to_globe = bpy.data.node_groups["Geo Flat To Globe"]

    def xyz(lat, lon, r=10.0):
        lat, lon = math.radians(lat), math.radians(lon)
        return (r * math.cos(lat) * math.cos(lon), r * math.cos(lat) * math.sin(lon), r * math.sin(lat))

    # A square on the globe, filled in flat space and put back on the globe.
    curve = bpy.data.curves.new("SmokeRing", "CURVE")
    curve.dimensions = "3D"
    spline = curve.splines.new("POLY")
    spline.points.add(3)
    for point, (lat, lon) in zip(spline.points, [(38, -90), (38, -88), (40, -88), (40, -90)]):
        point.co = (*xyz(lat, lon), 1.0)
    spline.use_cyclic_u = True
    obj = bpy.data.objects.new("SmokeRing", curve)
    coll.objects.link(obj)
    tree = bpy.data.node_groups.new("Smoke Fill On Globe", "GeometryNodeTree")
    tree.is_modifier = True
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    group_in, group_out = tree.nodes.new("NodeGroupInput"), tree.nodes.new("NodeGroupOutput")
    flat, fill, back = (tree.nodes.new(t) for t in ("GeometryNodeGroup", "GeometryNodeFillCurve", "GeometryNodeGroup"))
    flat.node_tree, back.node_tree = to_flat, to_globe
    for node in (flat, back):
        node.inputs["Center Latitude"].default_value = math.radians(39)
        node.inputs["Center Longitude"].default_value = math.radians(-89)
    for a, b in ((group_in, flat), (flat, fill), (fill, back), (back, group_out)):
        tree.links.new(a.outputs[0], b.inputs[0])
    obj.modifiers.new("Fill", "NODES").node_group = tree
    mesh = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh()
    radii = [v.co.length for v in mesh.vertices]
    assert len(mesh.polygons) > 0, "fill made no faces"
    assert max(abs(r - 10.0) for r in radii) < 1e-4, f"radius drifted: {min(radii)}..{max(radii)}"
    return f"{len(mesh.polygons)} faces, all on the globe"


def geotiff_layer():
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    def tif(crs, bounds):
        fd, path = tempfile.mkstemp(suffix=".tif")
        os.close(fd)
        with rasterio.open(path, "w", driver="GTiff", width=8, height=8, count=3, dtype="uint8",
                           crs=crs, transform=from_bounds(*bounds, 8, 8)) as dst:
            dst.write(np.full((3, 8, 8), 200, np.uint8))
        return path

    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    path = tif("EPSG:4326", (-100, 30, -80, 50))
    assert bpy.ops.object.add_geotiff_layer(filepath=path) == {"FINISHED"}
    name = os.path.splitext(os.path.basename(path))[0]
    image = bpy.data.images[name]
    assert image.packed_file is not None, "image isn't packed"
    assert tuple(image["geo_bounds"]) == (-100, 30, -80, 50), tuple(image["geo_bounds"])
    globe = bpy.data.objects["GeoReferenceGlobe"]
    layers = [n for m in globe.data.materials for n in m.node_tree.nodes
              if n.bl_idname == "ShaderNodeGroup" and n.node_tree.name == "GeoTIFF: " + name]
    assert layers, "layer not added to the reference globe"
    # Projected rasters are refused unless reprojecting is turned on.
    utm = tif("EPSG:32616", (3e5, 4e6, 4e5, 4.1e6))
    try:
        bpy.ops.object.add_geotiff_layer(filepath=utm)
        raise AssertionError("projected raster was accepted")
    except RuntimeError as exc:
        assert "Reproject" in str(exc), exc
    assert bpy.ops.object.add_geotiff_layer(filepath=utm, reproject=True) == {"FINISHED"}
    warped = bpy.data.images[os.path.splitext(os.path.basename(utm))[0]]
    assert warped["geo_reprojected_from"] == "EPSG:32616", warped.get("geo_reprojected_from")
    return (f"{image.size[0]}x{image.size[1]} layer on the reference globe; projected raster "
            "refused, then reprojected when asked")


def fill_extent_segments():
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    path = write(".geojson", json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "fill_me"},
         "geometry": {"type": "Polygon", "coordinates": [[[-90, 38], [-80, 38], [-80, 45], [-85, 41], [-90, 45], [-90, 38]]]}}]}))
    before = set(bpy.data.objects)
    assert bpy.ops.object.import_geojson_wireframes(filepath=path) == {"FINISHED"}
    outline = next(o for o in bpy.data.objects if o not in before and o.get("geo_name") == "fill_me")
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    outline.select_set(True)
    assert bpy.ops.object.fill_geo_polygons() == {"FINISHED"}
    filled = outline.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh()
    faces = len(filled.polygons)
    assert faces > 0, "Fill Polygons made no faces"

    fd, tif = tempfile.mkstemp(suffix=".tif")
    os.close(fd)
    with rasterio.open(tif, "w", driver="GTiff", width=4, height=4, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_bounds(-90, 38, -80, 45, 4, 4)) as dst:
        dst.write(np.arange(16, dtype="float32").reshape(1, 4, 4))
    assert bpy.ops.object.import_geotiff_extent(filepath=tif) == {"FINISHED"}
    extent = bpy.context.active_object
    assert extent.get("geo_kind") == "geotiff_extent" and len(extent.data.polygons) > 0

    settings = bpy.context.scene.country_wireframe_settings
    globe = bpy.data.objects["GeoReferenceGlobe"]
    settings.globe_segments = 32
    assert len(globe.data.vertices) == 32 * 15 + 2, len(globe.data.vertices)
    settings.globe_segments = 256
    return (f"{faces} filled faces; extent object {extent.name}; globe rebuilt at 32 then "
            f"{len(globe.data.vertices)} vertices")


def area_imagery():
    """Fetch Area Imagery with tiles faked locally: one web-map (USGS) and
    one GIBS source, as an object and as a layer; a missing tile comes out
    transparent."""
    import io

    import numpy as np
    from PIL import Image

    tiles = sys.modules[PKG + ".geo_tiles"]
    requested = []

    def fake_get(url):
        requested.append(url)
        if url.endswith("/0.jpeg") or "missing" in url:
            return None
        size = 512 if "gibs" in url else 256
        out = io.BytesIO()
        Image.new("RGB", (size, size), (40, 160, 90)).save(out, "JPEG")
        return out.getvalue()

    real_get, real_online, real_cache = tiles._http_get, tiles.online_access, tiles.cache_dir
    cache = tempfile.mkdtemp()  # not the real tile cache
    tiles._http_get, tiles.online_access, tiles.cache_dir = fake_get, lambda: True, lambda: cache
    try:
        source = tiles.SOURCES_BY_KEY["USGS_IMAGERY"]
        plan = tiles.plan(source, (40.10, -88.24, 40.12, -88.22), 16)
        assert abs(plan.resolution_m - 1.83) < 0.05, plan.resolution_m
        assert tiles.level_for(source, 2.0, 40.11) == 16
        assert tiles.level_for(source, 0.1, 40.11) == 16  # finest it has
        mosaic = tiles.fetch(source, plan.bbox, plan.level)
        assert mosaic.rgba.shape[:2] == (plan.size[1], plan.size[0]), (mosaic.rgba.shape, plan.size)
        assert mosaic.bounds == (-88.24, 40.10, -88.22, 40.12), mosaic.bounds
        assert (mosaic.rgba[..., 3] == 255).all(), "gaps in a fully covered USGS mosaic"

        osm = bpy.context.scene.avl_osm_settings
        osm.bbox = "40.10, -88.24, 40.12, -88.22"
        settings = bpy.context.scene.geo_imagery_settings
        settings.source, settings.resolution = "USGS_IMAGERY", 2.0
        assert bpy.ops.object.fetch_area_imagery(target="OBJECT") == {"FINISHED"}
        obj = bpy.context.active_object
        image = next(i for i in bpy.data.images if i.name.startswith("USGS L16"))
        assert "USGS" in image["geo_attribution"], dict(image)
        bounds = tuple(round(v, 6) for v in image["geo_bounds"])
        assert bounds == (-88.24, 40.10, -88.22, 40.12), bounds
        objects = len(bpy.data.objects)
        assert bpy.ops.object.fetch_area_imagery(target="OBJECT") == {"FINISHED"}
        assert len(bpy.data.objects) == objects, "fetching again made a new object"

        # GIBS: a column 0 tile is missing, so that part is transparent.
        osm.bbox = "-10, -179.9, 10, -100"
        settings.source, settings.resolution = "BLUE_MARBLE", 20000.0
        source, split = sys.modules[PKG + ".area_imagery"].current_split(bpy.context)
        plan = split.whole
        assert split.count == 1 and plan.cols.start == 0 and len(plan.cols) > 1, plan.cols
        mosaic = tiles.fetch(source, plan.bbox, plan.level)
        alpha = mosaic.rgba[..., 3]
        assert mosaic.missing > 0 and alpha[:, 0].max() == 0 and alpha[:, -1].min() == 255, mosaic.missing
        for o in bpy.context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        assert bpy.ops.object.fetch_area_imagery(target="LAYER") == {"FINISHED"}
        names = [n.node_tree.name for m in obj.data.materials for n in m.node_tree.nodes
                 if n.bl_idname == "ShaderNodeGroup" and n.node_tree.name.startswith("GeoTIFF: BlueMarble")]
        assert names, "Blue Marble layer not on the selected object"
    finally:
        tiles._http_get, tiles.online_access, tiles.cache_dir = real_get, real_online, real_cache
    return f"{len(requested)} fake tile requests; USGS object, then Blue Marble layer on it"


def area_imagery_patches():
    """A request bigger than Patch Size is split into patches that tile the
    area exactly, each georeferenced right: fake tile colors encode their
    column and row, checked at tile centers across every patch."""
    import io
    import math
    import re

    import numpy as np
    from PIL import Image

    tiles = sys.modules[PKG + ".geo_tiles"]

    def fake_get(url):
        z, row, col = map(int, re.search(r"/tile/(\d+)/(\d+)/(\d+)$", url).groups())
        out = io.BytesIO()
        Image.new("RGB", (256, 256), (col % 256, row % 256, 128)).save(out, "PNG")
        return out.getvalue()

    real_get, real_online, real_cache = tiles._http_get, tiles.online_access, tiles.cache_dir
    cache = tempfile.mkdtemp()  # not the real tile cache
    tiles._http_get, tiles.online_access, tiles.cache_dir = fake_get, lambda: True, lambda: cache
    try:
        bbox = (40.10, -88.24, 40.12, -88.22)
        bpy.context.scene.avl_osm_settings.bbox = ", ".join(map(str, bbox))
        settings = bpy.context.scene.geo_imagery_settings
        settings.source, settings.resolution, settings.patch_size = "USGS_IMAGERY", 2.0, 512
        source, split = sys.modules[PKG + ".area_imagery"].current_split(bpy.context)
        assert split.count > 1 and split.problem() is None, (split.nx, split.ny, split.problem())
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        assert bpy.ops.object.fetch_area_imagery(target="OBJECT") == {"FINISHED"}
        made = [o for o in bpy.context.selected_objects if o.get("geo_kind") == "geotiff_extent"]
        assert len(made) == split.count, (len(made), split.count)
        assert len({o.users_collection[0].name for o in made}) == 1, "patches not in one collection"

        # Extents tile the area: total area matches, and the outer edges match the bbox.
        extents = [p.extent for p in split.patches]
        area = sum((e - w) * (n - s) for w, s, e, n in extents)
        assert abs(area - (bbox[3] - bbox[1]) * (bbox[2] - bbox[0])) < 1e-12, area
        assert min(e[0] for e in extents) == bbox[1] and max(e[2] for e in extents) == bbox[3]

        z = split.whole.level
        n = 2 ** z
        checked = 0
        for patch, obj in zip(split.patches, sorted(made, key=lambda o: o.name)):
            name = obj.name[len("GeoTIFF_"):]
            image = bpy.data.images[name]
            w, h = image.size
            assert max(w, h) <= 512, (w, h)
            gw, gs, ge, gn = image["geo_bounds"]
            pw, ps, pe, pn = patch.extent
            assert gw <= pw and gs <= ps and ge >= pe and gn >= pn, "image doesn't cover its patch"
            px = np.array(image.pixels[:]).reshape(h, w, 4)
            for col in range(math.floor((pw + 180) / 360 * n), math.floor((pe + 180) / 360 * n) + 1):
                for row in range(n):
                    lon = (col + 0.5) / n * 360 - 180
                    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (row + 0.5) / n))))
                    if not (pw < lon < pe and ps < lat < pn):
                        continue
                    x = int((lon - gw) / (ge - gw) * w)
                    y = int((lat - gs) / (gn - gs) * h)
                    r, g = (px[y, x, :2] * 255).round()
                    assert (r, g) == (col % 256, row % 256), (patch.row, patch.col, lat, lon, r, g, col, row)
                    checked += 1
        assert checked >= split.count, checked
    finally:
        tiles._http_get, tiles.online_access, tiles.cache_dir = real_get, real_online, real_cache
    return f"{split.nx}x{split.ny} patches, {checked} tile centers in the right place"


def area_imagery_background():
    """The threaded fetch: placing patches as they arrive, then cancelling
    one partway through, which keeps the patches already made."""
    import io
    import threading
    import time

    from PIL import Image

    tiles = sys.modules[PKG + ".geo_tiles"]
    area = sys.modules[PKG + ".area_imagery"]
    count = [0]

    def fake_get(url):
        count[0] += 1
        time.sleep(0.01)
        out = io.BytesIO()
        Image.new("RGB", (256, 256), (90, 90, 90)).save(out, "PNG")
        return out.getvalue()

    class Reports:
        def __init__(self):
            self.messages = []

        def report(self, level, message):
            self.messages.append((set(level), message))

    real_get, real_online, real_cache = tiles._http_get, tiles.online_access, tiles.cache_dir
    cache = tempfile.mkdtemp()
    tiles._http_get, tiles.online_access, tiles.cache_dir = fake_get, lambda: True, lambda: cache
    try:
        # fetch() itself stops when cancelled.
        source = tiles.SOURCES_BY_KEY["USGS_IMAGERY"]
        cancel = threading.Event()
        try:
            tiles.fetch(source, (40.0, -88.4, 40.2, -88.2), 15,
                        progress=lambda done, _t: done >= 3 and cancel.set(), cancel=cancel)
            raise AssertionError("fetch wasn't cancelled")
        except tiles.Cancelled:
            pass

        bpy.context.scene.avl_osm_settings.bbox = "40.10, -88.24, 40.12, -88.22"
        settings = bpy.context.scene.geo_imagery_settings
        settings.source, settings.resolution, settings.patch_size = "USGS_IMAGERY", 2.0, 512
        source, split = area.current_split(bpy.context)

        def run_to_end(cancel_after=None):
            run = area.FetchRun(bpy.context, source, split, "", "OBJECT")
            run.start(threaded=True)
            deadline = time.time() + 120
            while not run.drain(bpy.context):
                if cancel_after is not None and run.patches_done >= cancel_after:
                    run.cancel.set()
                assert time.time() < deadline, "background fetch never ended"
                time.sleep(0.02)
            reports = Reports()
            return run, run.finish(bpy.context, reports), reports.messages

        run, result, messages = run_to_end()
        assert result == {"FINISHED"} and len(run.made) == split.count, (result, run.made, messages)

        tiles.cache_dir = lambda: tempfile.mkdtemp()  # so the second run downloads again
        run, result, messages = run_to_end(cancel_after=1)
        assert run.cancel.is_set() and 1 <= len(run.made) < split.count, (len(run.made), split.count)
        assert result == {"FINISHED"} and "Cancelled" in messages[-1][1], messages
    finally:
        tiles._http_get, tiles.online_access, tiles.cache_dir = real_get, real_online, real_cache
    return f"{split.count} patches in the background; cancelled after {len(run.made)}"


def area_tools():
    """Area from Camera on a rotated globe, the box maths at the
    antimeridian and poles, the outline, offline place search, and globe
    imagery objects sitting just above the sphere."""
    import math

    from mathutils import Euler, Vector

    tools = sys.modules[PKG + ".area_tools"]
    geo = sys.modules[PKG + ".geo_coord"]
    cs = bpy.context.scene.geo_coord_settings
    cs.coordinate_space = "GLOBE"
    R = cs.globe_radius

    # Box maths.
    assert tools.bbox_of_lonlats([(-88, 40), (-87, 41)]) == (40, -88, 41, -87)
    try:
        tools.bbox_of_lonlats([(179, 0), (-179, 1)])
        raise AssertionError("antimeridian box accepted")
    except RuntimeError as exc:
        assert "antimeridian" in str(exc)
    pole = tools.bbox_of_lonlats([(lon, 80) for lon in range(-180, 180, 10)])
    assert pole == (80, -180, 90, 180), pole

    # A camera looking straight down at 40N 88W, on a globe turned 30
    # degrees: the box is centered there either way.
    bpy.ops.object.apply_texture_from_raster.poll()  # globe module loaded
    root_mod = sys.modules[PKG + ".country_wireframes"]
    root = root_mod._ensure_globe_root_empty(bpy.context)
    cam = bpy.data.objects.new("SmokeCam", bpy.data.cameras.new("SmokeCam"))
    bpy.context.scene.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    centers = []
    for turn in (0.0, 30.0):
        root.rotation_euler = Euler((0.0, 0.0, math.radians(turn)))
        bpy.context.view_layer.update()
        surface = root.matrix_world @ Vector(geo.latlon_to_xyz(40.0, -88.0, R))
        up = (surface - root.matrix_world.translation).normalized()
        cam.location = surface + up * (R * 0.05)
        cam.rotation_euler = up.to_track_quat("Z", "Y").to_euler()
        bpy.context.view_layer.update()
        assert bpy.ops.object.area_from_view(source="CAMERA") == {"FINISHED"}
        s, w, n, e = sys.modules[PKG + "._osm_safety_shared"].parse_bbox_text(
            bpy.context.scene.avl_osm_settings.bbox)
        centers.append(((s + n) / 2, (w + e) / 2))
        assert abs((s + n) / 2 - 40) < 0.5 and abs((w + e) / 2 + 88) < 0.5, (turn, s, w, n, e)
        # 320 km up, 50 mm lens, 16:9: about 130 km (1.2 degrees) north-south.
        assert 1.0 < n - s < 1.4 and 2.0 < e - w < 3.5, (s, w, n, e)
    root.rotation_euler = Euler((0.0, 0.0, 0.0))
    bpy.context.view_layer.update()

    # The outline runs along the box's edges, just above the globe.
    pts = tools.outline_points((40, -90, 42, -87), R, root.matrix_world)
    radii = [Vector(p).length / R - 1 for p in pts]
    lats = [math.degrees(math.asin(p[2] / Vector(p).length)) for p in pts]
    assert all(0 < r < 1e-3 for r in radii) and abs(min(lats) - 40) < 1e-4 and abs(max(lats) - 42) < 1e-4

    places = tools.search_offline("Champaign")
    assert places and abs(places[0][2][0] - 40.1) < 0.5, places[:1]

    # Globe imagery objects: just above the sphere everywhere (face centers
    # included), parented to the globe root, not GeoOverlayRoot.
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    fd, tif = tempfile.mkstemp(suffix=".tif")
    os.close(fd)
    with rasterio.open(tif, "w", driver="GTiff", width=4, height=4, count=3, dtype="uint8",
                       crs="EPSG:4326", transform=from_bounds(-95, 35, -85, 42, 4, 4)) as dst:
        dst.write(np.full((3, 4, 4), 100, np.uint8))
    assert bpy.ops.object.import_geotiff_extent(filepath=tif) == {"FINISHED"}
    obj = bpy.context.active_object
    assert obj.parent is root, obj.parent
    mesh = obj.data
    centers_r = [p.center.length / R - 1 for p in mesh.polygons]
    lift = max(v.co.length / R - 1 for v in mesh.vertices)
    assert min(centers_r) > 0 and lift < 2e-5, (min(centers_r), lift)
    return (f"camera box centered right on a turned globe; imagery {lift * 6371e3:.0f} m above "
            f"the globe (Overlay Offset is {cs.overlay_offset * 6371e3:.0f} m); "
            f"offline search found {places[0][0]}")


def place_search_network():
    tools = sys.modules[PKG + ".area_tools"]
    results = tools.search_nominatim("Champaign, Illinois")
    s, w, n, e = results[0][2]
    assert s < 40.11 < n and w < -88.24 < e, results[0]
    return f"{results[0][0][:50]}..."


def area_imagery_network():
    tiles = sys.modules[PKG + ".geo_tiles"]
    usgs = tiles.SOURCES_BY_KEY["USGS_IMAGERY"]
    assert tiles.has_tile_at(usgs, 40.1075, -88.2272, 16), "no USGS tile at UIUC"
    assert not tiles.has_tile_at(usgs, 48.8584, 2.2945, 16), "USGS tile in Paris"
    landsat = tiles.SOURCES_BY_KEY["LANDSAT_2000"]
    mosaic = tiles.fetch(landsat, (48.85, 2.28, 48.87, 2.31), 11)
    assert mosaic.missing == 0 and mosaic.rgba[..., :3].std() > 5, "blank Landsat imagery"
    return f"USGS covers UIUC, not Paris; Landsat 2000 over Paris {mosaic.rgba.shape[1]}x{mosaic.rgba.shape[0]}"


def natural_earth():
    assert bpy.ops.object.refresh_geo_country_list() == {"FINISHED"}
    return f"{len(bpy.context.scene.country_wireframe_settings.country_items)} countries"


check("extension enabled", enabled)
check("bundled dependencies import", imports)
check("GeoJSON import", geojson_import)
check("GPX import", gpx_import)
check("constant-mass emission generator", emission_generator)
check("pair-names node group (nodebpy)", pair_names_node_group)
check("globe/flat node groups", globe_flat_node_groups)
check("GeoTIFF layer", geotiff_layer)
check("Fill Polygons, GeoTIFF extent, Globe Segments", fill_extent_segments)
check("Area Imagery (fake tiles)", area_imagery)
check("Area Imagery patches (fake tiles)", area_imagery_patches)
check("Area Imagery in the background, cancelled (fake tiles)", area_imagery_background)
check("Area tools: from camera, outline, offline search, globe imagery lift", area_tools)
if NETWORK:
    check("Natural Earth country list (network)", natural_earth)
    check("Area Imagery sources (network)", area_imagery_network)
    check("Place search (network)", place_search_network)

print(f"\n{len(failures)} failure(s)" + (f": {', '.join(failures)}" if failures else ""))
sys.exit(1 if failures else 0)

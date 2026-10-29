import datetime
import gzip
import math
import os
import tempfile

import bpy
from bpy_extras.io_utils import ImportHelper

from . import geo_coord
from .geo_coord import project_point

try:
    from gpx import read_gpx
except ImportError:
    read_gpx = None


# ---------------------------------------------------------------------------
#  GPX attribute extraction
# ---------------------------------------------------------------------------

_POINT_ATTRS = (
    ("lat", "FLOAT"),
    ("lon", "FLOAT"),
    ("ele", "FLOAT"),
    ("time", "STRING"),
    ("name", "STRING"),
    ("cmt", "STRING"),
    ("desc", "STRING"),
    ("src", "STRING"),
    ("sym", "STRING"),
    ("type", "STRING"),
    ("fix", "STRING"),
    ("sat", "INT"),
    ("hdop", "FLOAT"),
    ("vdop", "FLOAT"),
    ("pdop", "FLOAT"),
    ("ageofdgpsdata", "FLOAT"),
    ("dgpsid", "INT"),
    ("magvar", "FLOAT"),
    ("geoidheight", "FLOAT"),
)

_TRACK_ATTRS = ("name", "cmt", "desc", "src", "number", "type")

_POINT_ATTR_TYPES = dict(_POINT_ATTRS)


def _extract_point_attrs(waypoint):
    attrs = {}
    for attr_name, _ in _POINT_ATTRS:
        value = getattr(waypoint, attr_name, None)
        if value is None:
            continue
        if attr_name == "time":
            if isinstance(value, datetime.datetime):
                attrs[attr_name] = value.isoformat()
            else:
                attrs[attr_name] = str(value)
        elif attr_name == "fix":
            attrs[attr_name] = str(value) if value is not None else ""
        elif _POINT_ATTR_TYPES.get(attr_name) == "STRING":
            attrs[attr_name] = str(value)
        else:
            try:
                attrs[attr_name] = float(value)
            except (TypeError, ValueError):
                attrs[attr_name] = str(value)
    return attrs


def _extract_extension_attrs(waypoint):
    ext = getattr(waypoint, "extensions", None)
    if ext is None:
        return {}
    attrs = {}
    for elem in ext.elements:
        tag = elem.tag
        local = tag.split("}")[-1] if "}" in tag else tag
        text = (elem.text or "").strip()
        if not text:
            for child in elem:
                child_local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                child_text = (child.text or "").strip()
                if child_text:
                    key = f"ext_{local}_{child_local}"
                    try:
                        attrs[key] = int(child_text)
                    except ValueError:
                        try:
                            attrs[key] = float(child_text)
                        except ValueError:
                            attrs[key] = child_text
            continue
        key = f"ext_{local}"
        try:
            attrs[key] = int(text)
        except ValueError:
            try:
                attrs[key] = float(text)
            except ValueError:
                attrs[key] = text
    return attrs


# ---------------------------------------------------------------------------
#  File reading
# ---------------------------------------------------------------------------

def _read_gpx_file(filepath):
    lower = filepath.lower()
    if lower.endswith(".gz"):
        fd, tmp = tempfile.mkstemp(prefix="gpx_", suffix=".gpx")
        os.close(fd)
        try:
            with gzip.open(filepath, "rb") as src:
                data = src.read()
            with open(tmp, "wb") as dst:
                dst.write(data)
            return read_gpx(tmp)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return read_gpx(filepath)


# ---------------------------------------------------------------------------
#  Mesh creation
# ---------------------------------------------------------------------------

def _collect_all_attrs(segments, attr_extractor):
    all_keys = []
    seen = set()
    for seg in segments:
        for pt in seg:
            for key in attr_extractor(pt):
                if key not in seen:
                    all_keys.append(key)
                    seen.add(key)
    return all_keys


def _track_mesh(name, track, coord_settings):
    """Wire mesh for one track (or route), one edge chain per segment, with
    every GPX point field as a POINT attribute. None if it has no points."""
    segments = [seg.trkpt for seg in track.trkseg if seg.trkpt]
    if not segments:
        return None

    all_keys = _collect_all_attrs(segments, _extract_point_attrs)
    ext_keys = _collect_all_attrs(segments, _extract_extension_attrs)
    all_keys.extend(ext_keys)

    vertices = []
    edges = []
    vert_data = []
    offset = 0

    for seg_pts in segments:
        seg_start = offset
        for pt in seg_pts:
            lat = float(pt.lat)
            lon = float(pt.lon)
            ele = float(pt.ele) if pt.ele is not None else 0.0

            x, y, z = project_point(lat, lon, ele, coord_settings)

            vertices.append((x, y, z))

            row = {}
            pattrs = _extract_point_attrs(pt)
            eattrs = _extract_extension_attrs(pt)
            combined = {**pattrs, **eattrs}
            for key in all_keys:
                row[key] = combined.get(key)
            vert_data.append(row)
            offset += 1

        for i in range(seg_start, offset - 1):
            edges.append((i, i + 1))

    if not vertices:
        return None

    mesh = bpy.data.meshes.new(f"{name}_Mesh")
    mesh.from_pydata(vertices, edges, [])
    mesh.update()

    for key in all_keys:
        sample = vert_data[0].get(key)
        if isinstance(sample, float):
            attr = mesh.attributes.new(key, "FLOAT", "POINT")
            for i, row in enumerate(vert_data):
                v = row.get(key)
                attr.data[i].value = float(v) if v is not None else 0.0
        elif isinstance(sample, int):
            attr = mesh.attributes.new(key, "INT", "POINT")
            for i, row in enumerate(vert_data):
                v = row.get(key)
                attr.data[i].value = int(v) if v is not None else 0
        else:
            attr = mesh.attributes.new(key, "STRING", "POINT")
            for i, row in enumerate(vert_data):
                v = row.get(key)
                attr.data[i].value = str(v).encode("utf-8") if v is not None else b""

    return mesh


def _track_props(track):
    props = {}
    for attr_name in _TRACK_ATTRS:
        val = getattr(track, attr_name, None)
        if val is not None:
            props[f"gpx_{attr_name}"] = val
    return props


def _wpt_mesh(name, waypoints, coord_settings):
    """Point-only mesh, one vertex per waypoint. None if there are none."""
    if not waypoints:
        return None

    all_keys = _collect_all_attrs([waypoints], _extract_point_attrs)
    ext_keys = _collect_all_attrs([waypoints], _extract_extension_attrs)
    all_keys.extend(ext_keys)

    vertices = []
    vert_data = []

    for pt in waypoints:
        lat = float(pt.lat)
        lon = float(pt.lon)
        ele = float(pt.ele) if pt.ele is not None else 0.0

        x, y, z = project_point(lat, lon, ele, coord_settings)

        vertices.append((x, y, z))

        row = {}
        pattrs = _extract_point_attrs(pt)
        eattrs = _extract_extension_attrs(pt)
        combined = {**pattrs, **eattrs}
        for key in all_keys:
            row[key] = combined.get(key)
        vert_data.append(row)

    if not vertices:
        return None

    mesh = bpy.data.meshes.new(f"{name}_Mesh")
    mesh.from_pydata(vertices, [], [])
    mesh.update()

    for key in all_keys:
        sample = vert_data[0].get(key)
        if isinstance(sample, float):
            attr = mesh.attributes.new(key, "FLOAT", "POINT")
            for i, row in enumerate(vert_data):
                v = row.get(key)
                attr.data[i].value = float(v) if v is not None else 0.0
        elif isinstance(sample, int):
            attr = mesh.attributes.new(key, "INT", "POINT")
            for i, row in enumerate(vert_data):
                v = row.get(key)
                attr.data[i].value = int(v) if v is not None else 0
        else:
            attr = mesh.attributes.new(key, "STRING", "POINT")
            for i, row in enumerate(vert_data):
                v = row.get(key)
                attr.data[i].value = str(v).encode("utf-8") if v is not None else b""

    return mesh


def _child_collection(parent, name):
    """Get or create `name` among parent's own children (not a global
    lookup, which could grab a same-named collection from elsewhere)."""
    coll = parent.children.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        parent.children.link(coll)
    return coll


def _legacy_key(obj):
    """Key for an object imported before keyed joins, from the index
    properties every import has always stamped."""
    if "gpx_track_index" in obj:
        return f"trk:{obj['gpx_track_index']}"
    if "gpx_route_index" in obj:
        return f"rte:{obj['gpx_route_index']}"
    if obj.name.startswith("GPX_Waypoints_"):
        return "wpt"
    return None


def _reconcile(collection, items):
    """One object per item in `collection`, via d3blender's data().join()
    keyed by item["key"] ("trk:<n>", "rte:<n>" or "wpt", the item's position
    in the file). Re-importing the same file updates these objects in place,
    so references to them and modifiers added by hand survive; objects for
    tracks no longer in the file are removed."""
    from d3blender import select
    from d3blender.blender_adapter import get_adapter

    for obj in collection.objects:
        if "d3_key" not in obj:
            key = _legacy_key(obj)
            if key is not None:
                obj["d3_key"] = key

    def enter(item):
        obj = bpy.data.objects.new(item["name"], item["mesh"])
        collection.objects.link(obj)
        return obj

    def remove(obj):
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)

    joined = select(get_adapter(), list(collection.objects)).data(
        items, key=lambda i, item: item["key"]
    ).join(enter, exit=remove)

    for obj, item in zip(joined.elements, joined.datum):
        old_mesh = obj.data
        obj.data = item["mesh"]
        if old_mesh is not None and old_mesh is not item["mesh"] and old_mesh.users == 0:
            bpy.data.meshes.remove(old_mesh)
        obj.display_type = item["display_type"]
        for key in [k for k in obj.keys() if k.startswith("gpx_")]:
            del obj[key]
        for key, value in item["props"].items():
            obj[key] = value
    return joined


# ---------------------------------------------------------------------------
#  Import orchestration
# ---------------------------------------------------------------------------

def _do_import(context, filepath, settings):
    gpx_data = _read_gpx_file(filepath)
    coord_settings = context.scene.geo_coord_settings

    gpx_coll = _child_collection(context.scene.collection, "GPX Imports")

    # Re-importing a file reuses its own two collections and updates their
    # objects in place (see _reconcile); Clear Existing removes every OTHER
    # file's imports.
    file_label = os.path.splitext(os.path.basename(filepath))[0]
    tracks_coll = _child_collection(gpx_coll, f"GPX Tracks {file_label}")
    wpts_coll = _child_collection(gpx_coll, f"GPX Waypoints {file_label}")

    if settings.gpx_clear_existing:
        for child in list(gpx_coll.children):
            if child in (tracks_coll, wpts_coll):
                continue
            for obj in list(child.objects):
                bpy.data.objects.remove(obj, do_unlink=True)
            bpy.data.collections.remove(child)
        for obj in list(gpx_coll.objects):
            bpy.data.objects.remove(obj, do_unlink=True)

    track_items = []

    for idx, track in enumerate(gpx_data.trk, start=1):
        tname = track.name or f"Track_{idx}"
        safe = tname.replace(" ", "_").replace("/", "_")[:60]
        mesh = _track_mesh(f"GPX_{safe}", track, coord_settings)
        if mesh is not None:
            track_items.append({
                "key": f"trk:{idx}",
                "name": f"GPX_{safe}",
                "mesh": mesh,
                "display_type": "WIRE",
                "props": {
                    **_track_props(track),
                    "gpx_source_file": filepath,
                    "gpx_track_index": idx,
                },
            })

    for idx, rte in enumerate(gpx_data.rte, start=1):
        rname = rte.name or f"Route_{idx}"
        safe = rname.replace(" ", "_").replace("/", "_")[:60]
        if rte.rtept:
            seg = type("Seg", (), {"trkpt": list(rte.rtept)})()
            proxy = type("Rte", (), {
                "name": rte.name,
                "cmt": rte.cmt,
                "desc": rte.desc,
                "src": rte.src,
                "number": rte.number,
                "type": rte.type,
                "trkseg": [seg],
            })()
            mesh = _track_mesh(f"GPX_{safe}", proxy, coord_settings)
            if mesh is not None:
                track_items.append({
                    "key": f"rte:{idx}",
                    "name": f"GPX_{safe}",
                    "mesh": mesh,
                    "display_type": "WIRE",
                    "props": {
                        **_track_props(proxy),
                        "gpx_source_file": filepath,
                        "gpx_route_index": idx,
                    },
                })

    wpt_items = []
    mesh = _wpt_mesh(f"GPX_Waypoints_{file_label}", gpx_data.wpt, coord_settings)
    if mesh is not None:
        wpt_items.append({
            "key": "wpt",
            "name": f"GPX_Waypoints_{file_label}",
            "mesh": mesh,
            "display_type": "SOLID",
            "props": {"gpx_source_file": filepath},
        })

    track_count = len(_reconcile(tracks_coll, track_items))
    _reconcile(wpts_coll, wpt_items)
    wpt_count = len(gpx_data.wpt) if wpt_items else 0
    return track_count, wpt_count


# ---------------------------------------------------------------------------
#  Settings
# ---------------------------------------------------------------------------

class GpxImportSettings(bpy.types.PropertyGroup):
    gpx_clear_existing: bpy.props.BoolProperty(
        name="Clear Existing",
        description=(
            "Remove other files' GPX imports first. Re-importing the same "
            "file always updates its objects in place"
        ),
        default=True,
    )


# ---------------------------------------------------------------------------
#  Operator
# ---------------------------------------------------------------------------

class OBJECT_OT_import_gpx(bpy.types.Operator, ImportHelper):
    bl_idname = "object.import_gpx"
    bl_label = "Import GPX"
    bl_description = "Import GPX/GPX.GZ files as wireframes with per-point attributes"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".gpx"
    filter_glob: bpy.props.StringProperty(
        default="*.gpx;*.gpx.gz",
        options={"HIDDEN"},
    )

    def execute(self, context):
        if read_gpx is None:
            self.report(
                {"ERROR"},
                "The 'gpx' package is not installed. "
                "Install it in the Blender Python environment.",
            )
            return {"CANCELLED"}

        filepath = self.filepath
        if not filepath:
            self.report({"ERROR"}, "No file selected.")
            return {"CANCELLED"}

        settings = context.scene.gpx_import_settings

        try:
            track_count, wpt_count = _do_import(context, filepath, settings)
        except Exception as exc:
            self.report({"ERROR"}, f"GPX import failed: {exc}")
            return {"CANCELLED"}

        if track_count == 0 and wpt_count == 0:
            self.report({"WARNING"}, "GPX file contained no importable data.")
            return {"CANCELLED"}

        parts = []
        if track_count:
            parts.append(f"{track_count} track/route(s)")
        if wpt_count:
            parts.append(f"{wpt_count} waypoint(s)")
        self.report({"INFO"}, f"Imported {', '.join(parts)}.")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
#  Panel
# ---------------------------------------------------------------------------

class VIEW3D_PT_gpx_import(bpy.types.Panel):
    bl_label = "GPX Import"
    bl_idname = "VIEW3D_PT_gpx_import"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AVL GIS"
    # Placed through the same Placement settings as the other globe/map
    # importers, which the parent panel shows.
    bl_parent_id = "VIEW3D_PT_country_wireframes"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 4

    def draw(self, context):
        layout = self.layout
        settings = context.scene.gpx_import_settings

        layout.prop(settings, "gpx_clear_existing")

        col = layout.column(align=True)
        col.scale_y = 1.3
        col.operator("object.import_gpx", icon="IMPORT")


# ---------------------------------------------------------------------------
#  Registration
# ---------------------------------------------------------------------------

CLASSES = (
    GpxImportSettings,
    OBJECT_OT_import_gpx,
    VIEW3D_PT_gpx_import,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.gpx_import_settings = bpy.props.PointerProperty(
        type=GpxImportSettings,
    )


def unregister():
    if hasattr(bpy.types.Scene, "gpx_import_settings"):
        del bpy.types.Scene.gpx_import_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()

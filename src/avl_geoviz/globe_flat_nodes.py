"""Geometry-nodes groups that move points between globe space and a flat
working space, so curve/mesh operations that need local Z = 0 (Fill Curve,
2D booleans, ...) can be run on globe imports and the result put back on
the globe.

Globe space is the frame every globe import is built in: a sphere of
``Radius`` centered on the object origin (see geo_coord.latlon_to_xyz).

Flat space is an equirectangular map centered on (Center Latitude, Center
Longitude), in the same units as the globe:

    x = longitude offset (radians) * Radius
    y = latitude offset (radians)  * Radius
    z = distance from the globe center - Radius   (0 on the surface)

so lengths near the center match the globe. The globe is first rotated so
the center point sits at latitude 0, longitude 0, which keeps a feature
near the center out of the high-distortion polar rows and away from the
+-180 degree seam. Set the center to (roughly) the middle of the feature
you're working on, and use the same values on both groups.

Only point positions are moved; edges and faces made in flat space are
straight lines between converted points. Subdivide or resample in flat
space before converting back if large faces need to follow the curvature.
"""

import bpy

GLOBE_TO_FLAT_NAME = "Geo Globe To Flat"
FLAT_TO_GLOBE_NAME = "Geo Flat To Globe"

# Bump when the node layout or interface changes so existing files get the
# groups rebuilt. While it matches, existing groups are left as they are
# (only the Radius default is updated), so rebuilding never resets inputs
# on modifiers that already use them.
_GROUP_VERSION = 1
_VERSION_KEY = "avl_geoviz_globe_flat_version"


def _new_socket(group, name, in_out, socket_type, default=None, subtype=None,
                description=""):
    item = group.interface.new_socket(
        name=name, in_out=in_out, socket_type=socket_type, description=description,
    )
    if subtype is not None:
        item.subtype = subtype
    if default is not None:
        item.default_value = default
    return item


def _math(nodes, operation, location, label=None):
    node = nodes.new("ShaderNodeMath")
    node.operation = operation
    node.location = location
    if label:
        node.label = label
    return node


def _rotate(nodes, axis, location, label):
    node = nodes.new("ShaderNodeVectorRotate")
    node.rotation_type = axis
    node.location = location
    node.label = label
    return node


def _reset_group(name):
    group = bpy.data.node_groups.get(name)
    if group is None or group.bl_idname != "GeometryNodeTree":
        group = bpy.data.node_groups.new(name, "GeometryNodeTree")
    group.nodes.clear()
    group.interface.clear()
    # Usable both as a node inside another tree and directly as a modifier
    # (e.g. Globe To Flat -> your modifier -> Flat To Globe).
    group.is_modifier = True
    group[_VERSION_KEY] = _GROUP_VERSION
    return group


def _common_inputs(group, radius):
    _new_socket(group, "Geometry", "INPUT", "NodeSocketGeometry")
    _new_socket(group, "Selection", "INPUT", "NodeSocketBool", default=True)
    group.interface.items_tree["Selection"].hide_value = True
    _new_socket(
        group, "Radius", "INPUT", "NodeSocketFloat", default=radius,
        subtype="DISTANCE", description="Globe radius in the object's local space",
    )
    _new_socket(
        group, "Center Latitude", "INPUT", "NodeSocketFloat", default=0.0,
        subtype="ANGLE",
        description="Latitude that maps to the flat origin; use the same value on both groups",
    )
    _new_socket(
        group, "Center Longitude", "INPUT", "NodeSocketFloat", default=0.0,
        subtype="ANGLE",
        description="Longitude that maps to the flat origin; use the same value on both groups",
    )
    _new_socket(group, "Geometry", "OUTPUT", "NodeSocketGeometry")


def _build_globe_to_flat(radius):
    group = _reset_group(GLOBE_TO_FLAT_NAME)
    group.description = (
        "Move globe-space points to a flat equirectangular map centered on "
        "the center latitude/longitude (Z = height above the globe)"
    )
    _common_inputs(group, radius)
    nodes, links = group.nodes, group.links

    gin = nodes.new("NodeGroupInput")
    gin.location = (-1400, 0)
    gout = nodes.new("NodeGroupOutput")
    gout.location = (900, 0)
    position = nodes.new("GeometryNodeInputPosition")
    position.location = (-1400, -260)

    # Rotate the center point to lat 0, lon 0: about Z by -lon0, then about
    # Y by +lat0 (a right-handed rotation about +Y takes +Z toward +X).
    neg_lon = _math(nodes, "MULTIPLY", (-1200, -420), "-Center Longitude")
    neg_lon.inputs[1].default_value = -1.0
    links.new(gin.outputs["Center Longitude"], neg_lon.inputs[0])
    rot_z = _rotate(nodes, "Z_AXIS", (-1000, -260), "Undo Center Longitude")
    links.new(position.outputs["Position"], rot_z.inputs["Vector"])
    links.new(neg_lon.outputs[0], rot_z.inputs["Angle"])
    rot_y = _rotate(nodes, "Y_AXIS", (-800, -260), "Undo Center Latitude")
    links.new(rot_z.outputs["Vector"], rot_y.inputs["Vector"])
    links.new(gin.outputs["Center Latitude"], rot_y.inputs["Angle"])

    sep = nodes.new("ShaderNodeSeparateXYZ")
    sep.location = (-600, -260)
    links.new(rot_y.outputs["Vector"], sep.inputs[0])

    lon = _math(nodes, "ARCTAN2", (-400, -100), "Longitude")
    links.new(sep.outputs["Y"], lon.inputs[0])
    links.new(sep.outputs["X"], lon.inputs[1])

    xy = nodes.new("ShaderNodeCombineXYZ")
    xy.location = (-400, -300)
    links.new(sep.outputs["X"], xy.inputs["X"])
    links.new(sep.outputs["Y"], xy.inputs["Y"])
    xy_len = nodes.new("ShaderNodeVectorMath")
    xy_len.operation = "LENGTH"
    xy_len.location = (-200, -300)
    links.new(xy.outputs[0], xy_len.inputs[0])
    lat = _math(nodes, "ARCTAN2", (0, -260), "Latitude")
    links.new(sep.outputs["Z"], lat.inputs[0])
    links.new(xy_len.outputs["Value"], lat.inputs[1])

    dist = nodes.new("ShaderNodeVectorMath")
    dist.operation = "LENGTH"
    dist.location = (-400, -500)
    links.new(rot_y.outputs["Vector"], dist.inputs[0])
    height = _math(nodes, "SUBTRACT", (0, -500), "Height")
    links.new(dist.outputs["Value"], height.inputs[0])
    links.new(gin.outputs["Radius"], height.inputs[1])

    x = _math(nodes, "MULTIPLY", (200, -100), "X")
    links.new(lon.outputs[0], x.inputs[0])
    links.new(gin.outputs["Radius"], x.inputs[1])
    y = _math(nodes, "MULTIPLY", (200, -260), "Y")
    links.new(lat.outputs[0], y.inputs[0])
    links.new(gin.outputs["Radius"], y.inputs[1])

    flat = nodes.new("ShaderNodeCombineXYZ")
    flat.location = (400, -260)
    links.new(x.outputs[0], flat.inputs["X"])
    links.new(y.outputs[0], flat.inputs["Y"])
    links.new(height.outputs[0], flat.inputs["Z"])

    set_pos = nodes.new("GeometryNodeSetPosition")
    set_pos.location = (650, 0)
    links.new(gin.outputs["Geometry"], set_pos.inputs["Geometry"])
    links.new(gin.outputs["Selection"], set_pos.inputs["Selection"])
    links.new(flat.outputs[0], set_pos.inputs["Position"])
    links.new(set_pos.outputs["Geometry"], gout.inputs["Geometry"])
    return group


def _build_flat_to_globe(radius):
    group = _reset_group(FLAT_TO_GLOBE_NAME)
    group.description = (
        "Move flat-map points made by Geo Globe To Flat back onto the globe "
        "(Z = height above the globe)"
    )
    _common_inputs(group, radius)
    nodes, links = group.nodes, group.links

    gin = nodes.new("NodeGroupInput")
    gin.location = (-1400, 0)
    gout = nodes.new("NodeGroupOutput")
    gout.location = (1100, 0)
    position = nodes.new("GeometryNodeInputPosition")
    position.location = (-1400, -260)

    sep = nodes.new("ShaderNodeSeparateXYZ")
    sep.location = (-1200, -260)
    links.new(position.outputs["Position"], sep.inputs[0])

    lon = _math(nodes, "DIVIDE", (-1000, -100), "Longitude")
    links.new(sep.outputs["X"], lon.inputs[0])
    links.new(gin.outputs["Radius"], lon.inputs[1])
    lat = _math(nodes, "DIVIDE", (-1000, -260), "Latitude")
    links.new(sep.outputs["Y"], lat.inputs[0])
    links.new(gin.outputs["Radius"], lat.inputs[1])
    dist = _math(nodes, "ADD", (-1000, -420), "Distance")
    links.new(sep.outputs["Z"], dist.inputs[0])
    links.new(gin.outputs["Radius"], dist.inputs[1])

    cos_lat = _math(nodes, "COSINE", (-800, -200), "cos(lat)")
    links.new(lat.outputs[0], cos_lat.inputs[0])
    sin_lat = _math(nodes, "SINE", (-800, -360), "sin(lat)")
    links.new(lat.outputs[0], sin_lat.inputs[0])
    cos_lon = _math(nodes, "COSINE", (-800, -40), "cos(lon)")
    links.new(lon.outputs[0], cos_lon.inputs[0])
    sin_lon = _math(nodes, "SINE", (-800, 120), "sin(lon)")
    links.new(lon.outputs[0], sin_lon.inputs[0])

    ux = _math(nodes, "MULTIPLY", (-600, 0), "cos(lat)cos(lon)")
    links.new(cos_lat.outputs[0], ux.inputs[0])
    links.new(cos_lon.outputs[0], ux.inputs[1])
    uy = _math(nodes, "MULTIPLY", (-600, -160), "cos(lat)sin(lon)")
    links.new(cos_lat.outputs[0], uy.inputs[0])
    links.new(sin_lon.outputs[0], uy.inputs[1])

    unit = nodes.new("ShaderNodeCombineXYZ")
    unit.location = (-400, -200)
    links.new(ux.outputs[0], unit.inputs["X"])
    links.new(uy.outputs[0], unit.inputs["Y"])
    links.new(sin_lat.outputs[0], unit.inputs["Z"])

    scaled = nodes.new("ShaderNodeVectorMath")
    scaled.operation = "SCALE"
    scaled.location = (-200, -200)
    links.new(unit.outputs[0], scaled.inputs[0])
    links.new(dist.outputs[0], scaled.inputs["Scale"])

    # Inverse of Globe To Flat's rotation: about Y by -lat0, then Z by +lon0.
    neg_lat = _math(nodes, "MULTIPLY", (-200, -420), "-Center Latitude")
    neg_lat.inputs[1].default_value = -1.0
    links.new(gin.outputs["Center Latitude"], neg_lat.inputs[0])
    rot_y = _rotate(nodes, "Y_AXIS", (0, -200), "Apply Center Latitude")
    links.new(scaled.outputs["Vector"], rot_y.inputs["Vector"])
    links.new(neg_lat.outputs[0], rot_y.inputs["Angle"])
    rot_z = _rotate(nodes, "Z_AXIS", (200, -200), "Apply Center Longitude")
    links.new(rot_y.outputs["Vector"], rot_z.inputs["Vector"])
    links.new(gin.outputs["Center Longitude"], rot_z.inputs["Angle"])

    set_pos = nodes.new("GeometryNodeSetPosition")
    set_pos.location = (850, 0)
    links.new(gin.outputs["Geometry"], set_pos.inputs["Geometry"])
    links.new(gin.outputs["Selection"], set_pos.inputs["Selection"])
    links.new(rot_z.outputs["Vector"], set_pos.inputs["Position"])
    links.new(set_pos.outputs["Geometry"], gout.inputs["Geometry"])
    return group


def ensure_globe_flat_node_groups(radius):
    """Create the Globe To Flat / Flat To Globe groups if they're missing or
    out of date, and set their Radius default to *radius*. Returns the two
    groups as (globe_to_flat, flat_to_globe).
    """
    groups = []
    for name, build in (
        (GLOBE_TO_FLAT_NAME, _build_globe_to_flat),
        (FLAT_TO_GLOBE_NAME, _build_flat_to_globe),
    ):
        group = bpy.data.node_groups.get(name)
        if (
            group is None
            or group.bl_idname != "GeometryNodeTree"
            or group.get(_VERSION_KEY) != _GROUP_VERSION
        ):
            group = build(radius)
        else:
            group.interface.items_tree["Radius"].default_value = radius
        groups.append(group)
    return tuple(groups)


# ---------------------------------------------------------------------------
#  Fill Polygons
# ---------------------------------------------------------------------------

FILL_GROUP_NAME = "Geo Fill Polygons"
FILL_MODIFIER_NAME = "Geo Fill Polygons"
_FILL_VERSION = 1

# Flat-space subdivision targets edges of about this many degrees, so filled
# globe polygons follow the sphere instead of cutting through it.
_FILL_TARGET_DEG = 2.0
_FILL_MAX_LEVEL = 6


def ensure_fill_group(radius):
    """Geo Fill Polygons: outline curves (or wire meshes) in, faces out.

    Globe: Globe To Flat -> Fill Curve -> Subdivide Mesh -> Flat To Globe.
    Flat: Fill Curve. Then Set Material, since Fill Curve's output has no
    material otherwise.
    """
    to_flat, to_globe = ensure_globe_flat_node_groups(radius)
    group = bpy.data.node_groups.get(FILL_GROUP_NAME)
    if (
        group is not None
        and group.bl_idname == "GeometryNodeTree"
        and group.get(_VERSION_KEY) == _FILL_VERSION
    ):
        return group
    group = _reset_group(FILL_GROUP_NAME)
    group[_VERSION_KEY] = _FILL_VERSION
    group.description = "Fill imported polygon outlines with faces, on the globe or a flat map"
    _new_socket(group, "Geometry", "INPUT", "NodeSocketGeometry")
    _new_socket(group, "Globe", "INPUT", "NodeSocketBool", default=True,
                description="Fill in flat space around the center and put the faces back on the globe")
    _new_socket(group, "Radius", "INPUT", "NodeSocketFloat", default=radius, subtype="DISTANCE")
    _new_socket(group, "Center Latitude", "INPUT", "NodeSocketFloat", default=0.0, subtype="ANGLE")
    _new_socket(group, "Center Longitude", "INPUT", "NodeSocketFloat", default=0.0, subtype="ANGLE")
    sub = _new_socket(group, "Subdivisions", "INPUT", "NodeSocketInt", default=0,
                      description="Subdivide the faces in flat space so large ones follow the globe")
    sub.min_value, sub.max_value = 0, 8
    _new_socket(group, "Material", "INPUT", "NodeSocketMaterial")
    _new_socket(group, "Keep Outline", "INPUT", "NodeSocketBool", default=False)
    _new_socket(group, "Geometry", "OUTPUT", "NodeSocketGeometry")
    nodes, links = group.nodes, group.links

    gin = nodes.new("NodeGroupInput")
    gin.location = (-1400, 0)
    # Wire meshes become curves; curves pass through.
    to_curve = nodes.new("GeometryNodeMeshToCurve")
    to_curve.location = (-1200, 100)
    links.new(gin.outputs["Geometry"], to_curve.inputs["Mesh"])
    outlines = nodes.new("GeometryNodeJoinGeometry")
    outlines.location = (-1000, 0)
    links.new(gin.outputs["Geometry"], outlines.inputs[0])
    links.new(to_curve.outputs["Curve"], outlines.inputs[0])

    flat = nodes.new("GeometryNodeGroup")
    flat.node_tree = to_flat
    flat.location = (-800, 200)
    back = nodes.new("GeometryNodeGroup")
    back.node_tree = to_globe
    back.location = (-200, 200)
    for node in (flat, back):
        for name in ("Radius", "Center Latitude", "Center Longitude"):
            links.new(gin.outputs[name], node.inputs[name])
    links.new(outlines.outputs[0], flat.inputs["Geometry"])
    fill_globe = nodes.new("GeometryNodeFillCurve")
    fill_globe.location = (-600, 200)
    links.new(flat.outputs["Geometry"], fill_globe.inputs["Curve"])
    subdivide = nodes.new("GeometryNodeSubdivideMesh")
    subdivide.location = (-400, 200)
    links.new(fill_globe.outputs["Mesh"], subdivide.inputs["Mesh"])
    links.new(gin.outputs["Subdivisions"], subdivide.inputs["Level"])
    links.new(subdivide.outputs["Mesh"], back.inputs["Geometry"])

    fill_flat = nodes.new("GeometryNodeFillCurve")
    fill_flat.location = (-400, -100)
    links.new(outlines.outputs[0], fill_flat.inputs["Curve"])

    space = nodes.new("GeometryNodeSwitch")
    space.input_type = "GEOMETRY"
    space.location = (0, 0)
    links.new(gin.outputs["Globe"], space.inputs["Switch"])
    links.new(fill_flat.outputs["Mesh"], space.inputs["False"])
    links.new(back.outputs["Geometry"], space.inputs["True"])

    material = nodes.new("GeometryNodeSetMaterial")
    material.location = (200, 0)
    links.new(space.outputs[0], material.inputs["Geometry"])
    links.new(gin.outputs["Material"], material.inputs["Material"])

    with_outline = nodes.new("GeometryNodeJoinGeometry")
    with_outline.location = (400, -150)
    links.new(material.outputs["Geometry"], with_outline.inputs[0])
    links.new(gin.outputs["Geometry"], with_outline.inputs[0])
    keep = nodes.new("GeometryNodeSwitch")
    keep.input_type = "GEOMETRY"
    keep.location = (600, 0)
    links.new(gin.outputs["Keep Outline"], keep.inputs["Switch"])
    links.new(material.outputs["Geometry"], keep.inputs["False"])
    links.new(with_outline.outputs[0], keep.inputs["True"])

    gout = nodes.new("NodeGroupOutput")
    gout.location = (800, 0)
    links.new(keep.outputs[0], gout.inputs["Geometry"])
    return group


def _outline_points(obj):
    """Object-local points of a mesh's vertices or a curve's control points."""
    import numpy as np

    if obj.type == "MESH":
        co = np.empty(len(obj.data.vertices) * 3)
        obj.data.vertices.foreach_get("co", co)
        return co.reshape(-1, 3)
    points = []
    for spline in obj.data.splines:
        if spline.type == "BEZIER":
            points.extend(tuple(p.co) for p in spline.bezier_points)
        else:
            points.extend(tuple(p.co)[:3] for p in spline.points)
    return np.array(points, dtype=float).reshape(-1, 3)


def fill_settings(obj, scene):
    """Modifier inputs for filling *obj*: whether it's on the globe, and
    for globe objects the center (radians) and a subdivision level from its
    angular size."""
    import math

    import numpy as np

    flat = obj.get("geo_lonlat_flat")
    is_globe = (not flat) if flat is not None else scene.geo_coord_settings.coordinate_space == "GLOBE"
    values = {"Globe": is_globe, "Radius": scene.geo_coord_settings.globe_radius,
              "Center Latitude": 0.0, "Center Longitude": 0.0, "Subdivisions": 0}
    points = _outline_points(obj)
    if not is_globe or len(points) == 0:
        return values
    units = points / np.maximum(np.linalg.norm(points, axis=1, keepdims=True), 1e-12)
    center = units.mean(axis=0)
    norm = np.linalg.norm(center)
    if norm < 1e-9:
        return values
    center /= norm
    values["Center Latitude"] = math.asin(max(-1.0, min(1.0, center[2])))
    values["Center Longitude"] = math.atan2(center[1], center[0])
    radius_deg = math.degrees(math.acos(max(-1.0, min(1.0, float((units @ center).min())))))
    span = 2.0 * radius_deg
    if span > _FILL_TARGET_DEG:
        values["Subdivisions"] = min(_FILL_MAX_LEVEL, math.ceil(math.log2(span / _FILL_TARGET_DEG)))
    return values


def _first_material(obj):
    data = getattr(obj, "data", None)
    for material in getattr(data, "materials", None) or []:
        if material is not None:
            return material
    return None


def sync_fill_material(obj):
    """Point an object's Fill Polygons modifier at its first material (e.g.
    after Add GeoTIFF Layer gave it one)."""
    from .country_wireframes import _apply_node_group_input_defaults

    material = _first_material(obj)
    if material is None:
        return
    for mod in obj.modifiers:
        if mod.type == "NODES" and mod.node_group is not None and mod.node_group.name == FILL_GROUP_NAME:
            _apply_node_group_input_defaults(mod, mod.node_group, {"Material": material})


class OBJECT_OT_fill_geo_polygons(bpy.types.Operator):
    bl_idname = "object.fill_geo_polygons"
    bl_label = "Fill Polygons"
    bl_description = (
        "Give the selected polygon outlines (imported curves or wireframes) "
        "faces, on the globe or a flat map, with a Geo Fill Polygons modifier"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(o.type in {"CURVE", "MESH"} for o in context.selected_objects)

    def execute(self, context):
        from .country_wireframes import _apply_node_group_input_defaults

        group = ensure_fill_group(context.scene.geo_coord_settings.globe_radius)
        count = 0
        for obj in context.selected_objects:
            if obj.type not in {"CURVE", "MESH"}:
                continue
            mod = obj.modifiers.get(FILL_MODIFIER_NAME)
            if mod is None or mod.type != "NODES":
                mod = obj.modifiers.new(FILL_MODIFIER_NAME, "NODES")
            mod.node_group = group
            values = fill_settings(obj, context.scene)
            values["Material"] = _first_material(obj)
            _apply_node_group_input_defaults(mod, group, values)
            obj.update_tag()
            count += 1
        self.report({"INFO"}, f"Filled {count} object(s)")
        return {"FINISHED"}


CLASSES = (OBJECT_OT_fill_geo_polygons,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)

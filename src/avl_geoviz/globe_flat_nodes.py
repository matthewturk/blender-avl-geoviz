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

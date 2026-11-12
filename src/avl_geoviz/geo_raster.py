"""Drape GeoTIFFs over geo objects as material layers.

A GeoTIFF has to be in longitude/latitude (a geographic CRS such as
EPSG:4326) and north-up. Anything else is either reprojected outside
Blender first (``gdalwarp -t_srs EPSG:4326 in.tif out.tif``) or, with the
operator's opt-in Reproject to Lon/Lat, warped to EPSG:4326 in memory.
Its bounds then place it with a plain linear map, so no UVs are stored on
the objects:

* Geo LonLat (shader group) gets longitude/latitude back from the shaded
  point's object-local position: the direction from the center for globe
  objects, or the per-object affine mapping importers stamp on flat objects
  (geo_coord.stamp_lonlat_mapping).
* Geo Raster UV (shader group) turns lon/lat and a raster's bounds into a
  UV plus an inside-the-raster mask.
* Each GeoTIFF becomes a "GeoTIFF: <name>" layer group (Color in -> Color
  out) inserted in front of the Base Color of the objects' materials, so
  several rasters stack and areas outside a raster or under its nodata show
  what's below.
"""

import os

import bpy
from bpy_extras.io_utils import ImportHelper

from .country_wireframes import _AVLGISPanel

LONLAT_GROUP_NAME = "Geo LonLat"
RASTER_UV_GROUP_NAME = "Geo Raster UV"
LAYER_GROUP_PREFIX = "GeoTIFF: "
SHARED_MATERIAL_NAME = "GeoTIFF Layers"

# Bump when a shared group's layout or interface changes so existing files
# get it rebuilt.
_GROUP_VERSION = 1
_VERSION_KEY = "avl_geoviz_raster_version"

# Viridis, for single-band (data) rasters. Edit the layer's Color Ramp to
# change it.
_VIRIDIS = (
    (0.0, (0.267, 0.005, 0.329)),
    (0.25, (0.229, 0.322, 0.546)),
    (0.5, (0.128, 0.567, 0.551)),
    (0.75, (0.369, 0.789, 0.383)),
    (1.0, (0.993, 0.906, 0.144)),
)


class GeoTiffError(Exception):
    pass


# ---------------------------------------------------------------------------
#  Node-building helpers
# ---------------------------------------------------------------------------

def _socket(sockets, identifier):
    for socket in sockets:
        if socket.identifier == identifier:
            return socket
    raise KeyError(identifier)


def _math(nodes, operation, location, a=None, b=None):
    node = nodes.new("ShaderNodeMath")
    node.operation = operation
    node.location = location
    if a is not None:
        node.inputs[0].default_value = a
    if b is not None:
        node.inputs[1].default_value = b
    return node


def _object_attribute(nodes, name, location):
    node = nodes.new("ShaderNodeAttribute")
    node.attribute_type = "OBJECT"
    node.attribute_name = name
    node.location = location
    return node


def _new_group(name, version=_GROUP_VERSION):
    group = bpy.data.node_groups.get(name)
    if group is None or group.bl_idname != "ShaderNodeTree":
        group = bpy.data.node_groups.new(name, "ShaderNodeTree")
    group.nodes.clear()
    group.interface.clear()
    group[_VERSION_KEY] = version
    return group


def _current(name):
    group = bpy.data.node_groups.get(name)
    if (
        group is not None
        and group.bl_idname == "ShaderNodeTree"
        and group.get(_VERSION_KEY) == _GROUP_VERSION
    ):
        return group
    return None


# ---------------------------------------------------------------------------
#  Shared shader groups
# ---------------------------------------------------------------------------

def ensure_lonlat_group():
    """Geo LonLat: (longitude, latitude, 0) in degrees for the shaded point."""
    group = _current(LONLAT_GROUP_NAME)
    if group is not None:
        return group
    group = _new_group(LONLAT_GROUP_NAME)
    group.description = (
        "Longitude/latitude (degrees) of the shaded point, from its object-local "
        "position: globe objects by direction, flat objects by the mapping "
        "their importer stored on them"
    )
    group.interface.new_socket(name="Lon Lat", in_out="OUTPUT", socket_type="NodeSocketVector")
    nodes, links = group.nodes, group.links

    coords = nodes.new("ShaderNodeTexCoord")
    coords.location = (-1000, 0)
    sep = nodes.new("ShaderNodeSeparateXYZ")
    sep.location = (-800, 100)
    links.new(coords.outputs["Object"], sep.inputs[0])

    # Globe: direction from the center.
    lon = _math(nodes, "ARCTAN2", (-600, 250))
    links.new(sep.outputs["Y"], lon.inputs[0])
    links.new(sep.outputs["X"], lon.inputs[1])
    xy = nodes.new("ShaderNodeCombineXYZ")
    xy.location = (-600, 50)
    links.new(sep.outputs["X"], xy.inputs["X"])
    links.new(sep.outputs["Y"], xy.inputs["Y"])
    xy_len = nodes.new("ShaderNodeVectorMath")
    xy_len.operation = "LENGTH"
    xy_len.location = (-400, 50)
    links.new(xy.outputs[0], xy_len.inputs[0])
    lat = _math(nodes, "ARCTAN2", (-200, 100))
    links.new(sep.outputs["Z"], lat.inputs[0])
    links.new(xy_len.outputs["Value"], lat.inputs[1])
    lon_deg = _math(nodes, "DEGREES", (0, 250))
    links.new(lon.outputs[0], lon_deg.inputs[0])
    lat_deg = _math(nodes, "DEGREES", (0, 100))
    links.new(lat.outputs[0], lat_deg.inputs[0])
    globe = nodes.new("ShaderNodeCombineXYZ")
    globe.location = (200, 200)
    links.new(lon_deg.outputs[0], globe.inputs["X"])
    links.new(lat_deg.outputs[0], globe.inputs["Y"])

    # Flat: lon/lat = (local - offset) / scale. Vector Math Divide is a safe
    # divide, so an object without the mapping gives 0 instead of NaN.
    offset = _object_attribute(nodes, "geo_lonlat_offset", (-600, -250))
    scale = _object_attribute(nodes, "geo_lonlat_scale", (-600, -450))
    shifted = nodes.new("ShaderNodeVectorMath")
    shifted.operation = "SUBTRACT"
    shifted.location = (-200, -200)
    links.new(coords.outputs["Object"], shifted.inputs[0])
    links.new(offset.outputs["Vector"], shifted.inputs[1])
    flat = nodes.new("ShaderNodeVectorMath")
    flat.operation = "DIVIDE"
    flat.location = (0, -250)
    links.new(shifted.outputs["Vector"], flat.inputs[0])
    links.new(scale.outputs["Vector"], flat.inputs[1])
    flat_z = nodes.new("ShaderNodeVectorMath")
    flat_z.operation = "MULTIPLY"
    flat_z.location = (200, -250)
    flat_z.inputs[1].default_value = (1.0, 1.0, 0.0)
    links.new(flat.outputs["Vector"], flat_z.inputs[0])

    is_flat = _object_attribute(nodes, "geo_lonlat_flat", (200, 0))
    mix = nodes.new("ShaderNodeMix")
    mix.data_type = "VECTOR"
    mix.location = (450, 0)
    links.new(is_flat.outputs["Fac"], _socket(mix.inputs, "Factor_Float"))
    links.new(globe.outputs[0], _socket(mix.inputs, "A_Vector"))
    links.new(flat_z.outputs["Vector"], _socket(mix.inputs, "B_Vector"))

    out = nodes.new("NodeGroupOutput")
    out.location = (650, 0)
    links.new(_socket(mix.outputs, "Result_Vector"), out.inputs["Lon Lat"])
    return group


def ensure_raster_uv_group():
    """Geo Raster UV: lon/lat + raster bounds -> UV and inside mask."""
    group = _current(RASTER_UV_GROUP_NAME)
    if group is not None:
        return group
    group = _new_group(RASTER_UV_GROUP_NAME)
    group.description = "UV of a longitude/latitude raster, and 1 inside its bounds"
    iface = group.interface
    iface.new_socket(name="Lon Lat", in_out="INPUT", socket_type="NodeSocketVector")
    for name, default in (("West", -180.0), ("South", -90.0), ("East", 180.0), ("North", 90.0)):
        iface.new_socket(name=name, in_out="INPUT", socket_type="NodeSocketFloat").default_value = default
    iface.new_socket(name="UV", in_out="OUTPUT", socket_type="NodeSocketVector")
    iface.new_socket(name="Inside", in_out="OUTPUT", socket_type="NodeSocketFloat")
    nodes, links = group.nodes, group.links

    gin = nodes.new("NodeGroupInput")
    gin.location = (-900, 0)
    sep = nodes.new("ShaderNodeSeparateXYZ")
    sep.location = (-700, 150)
    links.new(gin.outputs["Lon Lat"], sep.inputs[0])

    # u: longitude east of West, wrapped into [0, 360) so a raster that
    # crosses the antimeridian (East > 180) still lines up.
    d_lon = _math(nodes, "SUBTRACT", (-500, 250))
    links.new(sep.outputs["X"], d_lon.inputs[0])
    links.new(gin.outputs["West"], d_lon.inputs[1])
    wrapped = _math(nodes, "WRAP", (-300, 250))
    links.new(d_lon.outputs[0], wrapped.inputs[0])
    wrapped.inputs[1].default_value = 360.0
    wrapped.inputs[2].default_value = 0.0
    width = _math(nodes, "SUBTRACT", (-500, 50))
    links.new(gin.outputs["East"], width.inputs[0])
    links.new(gin.outputs["West"], width.inputs[1])
    u = _math(nodes, "DIVIDE", (-100, 200))
    links.new(wrapped.outputs[0], u.inputs[0])
    links.new(width.outputs[0], u.inputs[1])

    d_lat = _math(nodes, "SUBTRACT", (-500, -150))
    links.new(sep.outputs["Y"], d_lat.inputs[0])
    links.new(gin.outputs["South"], d_lat.inputs[1])
    height = _math(nodes, "SUBTRACT", (-500, -350))
    links.new(gin.outputs["North"], height.inputs[0])
    links.new(gin.outputs["South"], height.inputs[1])
    v = _math(nodes, "DIVIDE", (-100, -200))
    links.new(d_lat.outputs[0], v.inputs[0])
    links.new(height.outputs[0], v.inputs[1])

    uv = nodes.new("ShaderNodeCombineXYZ")
    uv.location = (150, 100)
    links.new(u.outputs[0], uv.inputs["X"])
    links.new(v.outputs[0], uv.inputs["Y"])

    # u >= 0 always holds after the wrap.
    u_in = _math(nodes, "LESS_THAN", (100, -100), b=1.0)
    links.new(u.outputs[0], u_in.inputs[0])
    v_lo = _math(nodes, "GREATER_THAN", (100, -250), b=0.0)
    links.new(v.outputs[0], v_lo.inputs[0])
    v_hi = _math(nodes, "LESS_THAN", (100, -400), b=1.0)
    links.new(v.outputs[0], v_hi.inputs[0])
    both_v = _math(nodes, "MULTIPLY", (300, -300))
    links.new(v_lo.outputs[0], both_v.inputs[0])
    links.new(v_hi.outputs[0], both_v.inputs[1])
    inside = _math(nodes, "MULTIPLY", (450, -200))
    links.new(u_in.outputs[0], inside.inputs[0])
    links.new(both_v.outputs[0], inside.inputs[1])

    out = nodes.new("NodeGroupOutput")
    out.location = (650, 0)
    links.new(uv.outputs[0], out.inputs["UV"])
    links.new(inside.outputs[0], out.inputs["Inside"])
    return group


# ---------------------------------------------------------------------------
#  GeoTIFF -> Blender image
# ---------------------------------------------------------------------------

def _placement_problem(src, name):
    """Why *src* can't be placed as is (None if it can)."""
    if not src.crs.is_geographic:
        return f"{name} is in {src.crs.to_string()}, not longitude/latitude."
    t = src.transform
    if t.b != 0 or t.d != 0:
        return f"{name} is rotated or sheared."
    return None


def read_geotiff(path, max_size=4096, reproject=False):
    """Read a north-up, longitude/latitude GeoTIFF.

    With *reproject*, a projected or rotated GeoTIFF is first warped to
    north-up EPSG:4326 in memory (bilinear; the file isn't changed). Without
    it, such files are refused.

    Returns a dict with "pixels" (h x w x 4 float32 RGBA, bottom row first,
    alpha 0 at nodata), "bounds" (west, south, east, north), "is_data"
    (single-band rasters are kept as raw values in R, G and B), "value_range",
    "crs" and "reprojected_from" (the file's CRS if it was warped, else
    None). Raises GeoTiffError for files it can't place.
    """
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT

    name = os.path.basename(path)
    with rasterio.open(path) as file:
        if file.crs is None:
            raise GeoTiffError(f"{name} has no coordinate reference system.")
        if file.count < 1:
            raise GeoTiffError(f"{name} has no bands.")
        is_data = file.count < 3
        reprojected_from = None
        src = file
        problem = _placement_problem(file, name)
        if problem:
            if not reproject:
                raise GeoTiffError(
                    f"{problem} Turn on Reproject to Lon/Lat, or reproject it first, "
                    f"e.g. gdalwarp -t_srs EPSG:4326 in.tif out.tif"
                )
            reprojected_from = file.crs.to_string()
            # Without a nodata value the area outside the warped footprint
            # would be opaque black; an alpha band marks it as nodata.
            src = WarpedVRT(
                file, crs="EPSG:4326", resampling=Resampling.bilinear,
                add_alpha=file.nodata is None,
            )
        try:
            scale = min(1.0, max_size / float(max(src.width, src.height)))
            w = max(1, int(round(src.width * scale)))
            h = max(1, int(round(src.height * scale)))
            indexes = [1] if is_data else [1, 2, 3]
            data = src.read(
                indexes=indexes,
                out_shape=(len(indexes), h, w),
                resampling=Resampling.bilinear,
                masked=True,
            )
            valid = src.dataset_mask(out_shape=(h, w)) > 0
            bounds = tuple(src.bounds)  # left, bottom, right, top
            north_up = src.transform.e < 0
            crs = src.crs.to_string()
        finally:
            if src is not file:
                src.close()

    # A pixel is nodata where the dataset mask says so (for per-band nodata
    # values, where every band is nodata), not where any one band is: a pure
    # red pixel has nodata-valued green and blue under nodata=0.
    values = np.ma.filled(data.astype("float32"), np.nan if is_data else 0.0)
    valid &= np.isfinite(values).all(axis=0)
    if not valid.any():
        raise GeoTiffError(f"{name} has no valid pixels.")

    rgba = np.zeros((h, w, 4), dtype="float32")
    value_range = None
    if is_data:
        band = values[0]
        value_range = (float(band[valid].min()), float(band[valid].max()))
        rgba[..., 0] = rgba[..., 1] = rgba[..., 2] = np.where(valid, band, 0.0)
    else:
        rgb = np.moveaxis(values, 0, -1)
        if data.dtype == np.uint8:
            rgb = rgb / 255.0
        else:
            # Stretch other types (uint16 imagery, ...) by their 2-98% range.
            lo, hi = np.percentile(rgb[valid], (2.0, 98.0))
            rgb = (rgb - lo) / max(hi - lo, 1e-12)
        rgba[..., :3] = np.clip(np.nan_to_num(rgb), 0.0, 1.0)
    rgba[..., 3] = valid

    # Blender images start at the bottom row; north-up GeoTIFFs at the top.
    if north_up:
        rgba = rgba[::-1]
    return {
        "pixels": np.ascontiguousarray(rgba),
        "bounds": bounds,
        "is_data": is_data,
        "value_range": value_range,
        "crs": crs,
        "reprojected_from": reprojected_from,
        "size": (w, h),
    }


def _image_from_raster(name, path, raster):
    w, h = raster["size"]
    image = bpy.data.images.get(name)
    if (
        image is None
        or image.get("geo_source_file") != path
        or bool(image.get("geo_is_data")) != raster["is_data"]
    ):
        image = bpy.data.images.new(
            name, w, h, alpha=True, float_buffer=raster["is_data"],
        )
    elif tuple(image.size) != (w, h):
        image.scale(w, h)
    if raster["is_data"]:
        image.colorspace_settings.name = "Non-Color"
    image.alpha_mode = "STRAIGHT"
    image.pixels.foreach_set(raster["pixels"].ravel())
    image.update()
    # Pack the pixels so the .blend keeps them; generated images aren't
    # saved anywhere otherwise.
    image.file_format = "OPEN_EXR" if raster["is_data"] else "PNG"
    image.pack()
    image["geo_source_file"] = path
    image["geo_bounds"] = raster["bounds"]
    image["geo_crs"] = raster["crs"]
    image["geo_is_data"] = raster["is_data"]
    if raster["reprojected_from"]:
        image["geo_reprojected_from"] = raster["reprojected_from"]
    elif "geo_reprojected_from" in image:
        del image["geo_reprojected_from"]
    if raster["value_range"] is not None:
        image["geo_value_range"] = raster["value_range"]
    return image


# ---------------------------------------------------------------------------
#  Layer group and materials
# ---------------------------------------------------------------------------

def build_layer_group(image):
    """(Re)build the "GeoTIFF: <image>" layer group: Color + Opacity in,
    the raster mixed over Color out, plus the raw Value and the Mask."""
    lonlat_group = ensure_lonlat_group()
    uv_group = ensure_raster_uv_group()
    group = _current(LAYER_GROUP_PREFIX + image.name)
    if group is not None:
        # Re-adding the same file: rebuild only the inside. Clearing the
        # interface would disconnect this layer in every material using it.
        group.nodes.clear()
    else:
        group = _new_group(LAYER_GROUP_PREFIX + image.name)
        iface = group.interface
        color_in = iface.new_socket(name="Color", in_out="INPUT", socket_type="NodeSocketColor")
        color_in.default_value = (0.8, 0.8, 0.8, 1.0)
        opacity = iface.new_socket(name="Opacity", in_out="INPUT", socket_type="NodeSocketFloat")
        opacity.default_value = 1.0
        opacity.min_value = 0.0
        opacity.max_value = 1.0
        opacity.subtype = "FACTOR"
        iface.new_socket(name="Color", in_out="OUTPUT", socket_type="NodeSocketColor")
        iface.new_socket(name="Value", in_out="OUTPUT", socket_type="NodeSocketFloat")
        iface.new_socket(name="Mask", in_out="OUTPUT", socket_type="NodeSocketFloat")
    group.description = f"{image.name} draped by longitude/latitude"
    nodes, links = group.nodes, group.links

    gin = nodes.new("NodeGroupInput")
    gin.location = (-200, -350)
    lonlat = nodes.new("ShaderNodeGroup")
    lonlat.node_tree = lonlat_group
    lonlat.location = (-800, 100)
    uv = nodes.new("ShaderNodeGroup")
    uv.node_tree = uv_group
    uv.location = (-600, 100)
    west, south, east, north = image["geo_bounds"]
    for socket_name, value in (("West", west), ("South", south), ("East", east), ("North", north)):
        uv.inputs[socket_name].default_value = value
    links.new(lonlat.outputs["Lon Lat"], uv.inputs["Lon Lat"])

    tex = nodes.new("ShaderNodeTexImage")
    tex.image = image
    tex.extension = "CLIP"
    tex.interpolation = "Linear"
    tex.location = (-400, 150)
    links.new(uv.outputs["UV"], tex.inputs["Vector"])

    mask = _math(nodes, "MULTIPLY", (-100, -100))
    links.new(uv.outputs["Inside"], mask.inputs[0])
    links.new(tex.outputs["Alpha"], mask.inputs[1])
    factor = _math(nodes, "MULTIPLY", (100, -200))
    links.new(mask.outputs[0], factor.inputs[0])
    links.new(gin.outputs["Opacity"], factor.inputs[1])

    value = nodes.new("ShaderNodeSeparateColor")
    value.location = (-100, 100)
    links.new(tex.outputs["Color"], value.inputs["Color"])
    raster_color = tex.outputs["Color"]
    if image.get("geo_is_data"):
        lo, hi = image.get("geo_value_range", (0.0, 1.0))
        remap = nodes.new("ShaderNodeMapRange")
        remap.name = remap.label = "Value Range"
        remap.location = (100, 300)
        remap.inputs["From Min"].default_value = lo
        remap.inputs["From Max"].default_value = hi if hi > lo else lo + 1.0
        links.new(value.outputs["Red"], remap.inputs["Value"])
        ramp = nodes.new("ShaderNodeValToRGB")
        ramp.location = (300, 300)
        elements = ramp.color_ramp.elements
        while len(elements) > 1:
            elements.remove(elements[-1])
        elements[0].position, elements[0].color = _VIRIDIS[0][0], (*_VIRIDIS[0][1], 1.0)
        for position, rgb in _VIRIDIS[1:]:
            elements.new(position).color = (*rgb, 1.0)
        links.new(remap.outputs["Result"], ramp.inputs["Fac"])
        raster_color = ramp.outputs["Color"]

    mix = nodes.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    mix.location = (600, 0)
    links.new(factor.outputs[0], _socket(mix.inputs, "Factor_Float"))
    links.new(gin.outputs["Color"], _socket(mix.inputs, "A_Color"))
    links.new(raster_color, _socket(mix.inputs, "B_Color"))

    out = nodes.new("NodeGroupOutput")
    out.location = (850, 0)
    links.new(_socket(mix.outputs, "Result_Color"), out.inputs["Color"])
    links.new(value.outputs["Red"], out.inputs["Value"])
    links.new(mask.outputs[0], out.inputs["Mask"])
    return group


def _shared_material():
    material = bpy.data.materials.get(SHARED_MATERIAL_NAME)
    if material is None:
        material = bpy.data.materials.new(SHARED_MATERIAL_NAME)
        if material.node_tree is None:
            material.use_nodes = True
    return material


def _principled(material):
    if material.node_tree is None:
        return None
    for node in material.node_tree.nodes:
        if node.bl_idname == "ShaderNodeBsdfPrincipled":
            return node
    return None


def is_layer_node(node):
    return (
        node.bl_idname == "ShaderNodeGroup"
        and node.node_tree is not None
        and node.node_tree.name.startswith(LAYER_GROUP_PREFIX)
    )


def layer_chain(material):
    """The material's GeoTIFF layers feeding its Principled BSDF's Base
    Color, top (nearest the BSDF) first, plus what's below the bottom one:
    (layers, below_socket, below_color). below_socket is None when the
    bottom layer's Color input is a plain color, below_color.
    Returns None if the material has no Principled BSDF."""
    bsdf = _principled(material)
    if bsdf is None:
        return None
    base = bsdf.inputs["Base Color"]
    layers = []
    socket = base.links[0].from_socket if base.is_linked else None
    below_color = tuple(base.default_value)
    while socket is not None and is_layer_node(socket.node) and socket.identifier == socket.node.outputs["Color"].identifier:
        node = socket.node
        layers.append(node)
        color_in = node.inputs["Color"]
        below_color = tuple(color_in.default_value)
        socket = color_in.links[0].from_socket if color_in.is_linked else None
    return layers, socket, below_color


def _relink(material, layers, below_socket, below_color):
    """Chain *layers* (top first) between below_socket/below_color and the
    Principled BSDF's Base Color, and lay them out in a column."""
    bsdf = _principled(material)
    base = bsdf.inputs["Base Color"]
    links = material.node_tree.links
    for socket in [base] + [n.inputs["Color"] for n in layers]:
        for link in list(socket.links):
            links.remove(link)
    previous = below_socket
    for index, node in enumerate(reversed(layers)):
        if previous is None:
            node.inputs["Color"].default_value = below_color
        else:
            links.new(previous, node.inputs["Color"])
        previous = node.outputs["Color"]
        node.location = (bsdf.location.x - 300, bsdf.location.y + 220 * index)
    if previous is None:
        base.default_value = below_color
    else:
        links.new(previous, base)


def add_layer_to_material(material, layer_group):
    """Put the layer on top of the material's GeoTIFF layers, in front of
    its Principled BSDF Base Color. Returns False if the material has no
    Principled BSDF; a material that already has the layer is left as is."""
    chain = layer_chain(material)
    if chain is None:
        return False
    layers, below_socket, below_color = chain
    if any(n.node_tree is layer_group for n in material.node_tree.nodes if is_layer_node(n)):
        return True
    node = material.node_tree.nodes.new("ShaderNodeGroup")
    node.node_tree = layer_group
    node.label = layer_group.name
    _relink(material, [node] + layers, below_socket, below_color)
    return True


def move_layer(material, node, direction):
    """Move a layer one step "UP" (toward the BSDF, drawn over more layers)
    or "DOWN" in its material's stack."""
    layers, below_socket, below_color = layer_chain(material)
    index = layers.index(node)
    other = index - 1 if direction == "UP" else index + 1
    if 0 <= other < len(layers):
        layers[index], layers[other] = layers[other], layers[index]
        _relink(material, layers, below_socket, below_color)


def remove_layer(material, node):
    layers, below_socket, below_color = layer_chain(material)
    layers.remove(node)
    material.node_tree.nodes.remove(node)
    _relink(material, layers, below_socket, below_color)


def apply_layer(objects, layer_group):
    """Add the layer to each object's materials, giving objects with no
    material the shared GeoTIFF Layers material. Returns (done, skipped)
    object counts; objects whose materials have no Principled BSDF are
    skipped."""
    done = skipped = 0
    for obj in objects:
        data = obj.data
        if data is None or not hasattr(data, "materials"):
            skipped += 1
            continue
        materials = [m for m in data.materials if m is not None]
        if not materials:
            material = _shared_material()
            if data.materials:
                data.materials[0] = material
            else:
                data.materials.append(material)
            materials = [material]
        if any([add_layer_to_material(m, layer_group) for m in materials]):
            done += 1
        else:
            skipped += 1
    return done, skipped


# ---------------------------------------------------------------------------
#  Operator
# ---------------------------------------------------------------------------

def _faces_problems(context, objects):
    """Objects a layer won't show on: (no faces, faces without a material).
    Checks the evaluated geometry, so faces made by modifiers count."""
    depsgraph = context.evaluated_depsgraph_get()
    no_faces, no_material = [], []
    for obj in objects:
        evaluated = obj.evaluated_get(depsgraph)
        try:
            mesh = evaluated.to_mesh()
        except RuntimeError:
            mesh = None
        try:
            if mesh is None or len(mesh.polygons) == 0:
                no_faces.append(obj)
            elif not any(mesh.materials):
                # e.g. Fill Curve output from a curve object, which doesn't
                # pick up the object's material without a Set Material node.
                no_material.append(obj)
        finally:
            evaluated.to_mesh_clear()
    return no_faces, no_material


class OBJECT_OT_add_geotiff_layer(bpy.types.Operator, ImportHelper):
    bl_idname = "object.add_geotiff_layer"
    bl_label = "Add GeoTIFF Layer"
    bl_description = (
        "Drape a longitude/latitude GeoTIFF over the selected objects (the "
        "reference globe if none are selected) as a material layer"
    )
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".tif"
    filter_glob: bpy.props.StringProperty(default="*.tif;*.tiff", options={"HIDDEN"})
    reproject: bpy.props.BoolProperty(
        name="Reproject to Lon/Lat",
        description=(
            "Warp a projected or rotated GeoTIFF to longitude/latitude "
            "(EPSG:4326) in memory. For anything that needs care (datum "
            "shifts, antimeridian or polar data, choosing resampling), "
            "reproject it outside Blender instead"
        ),
        default=False,
    )
    max_size: bpy.props.IntProperty(
        name="Max Size",
        description="Downsample the raster so neither side is larger than this many pixels",
        default=4096,
        min=64,
        max=16384,
    )

    def execute(self, context):
        targets = [o for o in context.selected_objects if o.type in {"MESH", "CURVE"}]
        if not targets:
            from .country_wireframes import _ensure_reference_globe

            targets = [_ensure_reference_globe(context, context.scene.country_wireframe_settings)]

        try:
            raster = read_geotiff(self.filepath, self.max_size, self.reproject)
        except GeoTiffError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Couldn't read {os.path.basename(self.filepath)}: {exc}")
            return {"CANCELLED"}

        name = os.path.splitext(os.path.basename(self.filepath))[0]
        image = _image_from_raster(name, self.filepath, raster)
        layer = build_layer_group(image)
        done, skipped = apply_layer(targets, layer)

        from .globe_flat_nodes import sync_fill_material

        for obj in targets:
            sync_fill_material(obj)
        no_faces, no_material = _faces_problems(context, targets)
        unmapped = sum(1 for o in targets if "geo_lonlat_flat" not in o)
        w, h = raster["size"]
        kind = "data" if raster["is_data"] else "color"
        message = f"Added {image.name} ({w}x{h} {kind}) to {done} object(s)"
        if raster["reprojected_from"]:
            message += f", reprojected from {raster['reprojected_from']}"
        if skipped:
            message += f"; skipped {skipped} whose materials have no Principled BSDF"
        if unmapped and context.scene.geo_coord_settings.coordinate_space == "FLAT":
            message += (
                f"; {unmapped} object(s) weren't made by a GeoViz importer and are "
                "placed as if on the globe"
            )
        if no_faces:
            message += (
                f"; {len(no_faces)} object(s) have no faces, so it won't show on them "
                "until they're filled: use Fill Polygons"
            )
        if no_material:
            message += (
                f"; {len(no_material)} object(s) have faces with no material, e.g. "
                "made by Fill Curve; use Fill Polygons or add a Set Material node"
            )
        problems = skipped or unmapped or no_faces or no_material
        self.report({"WARNING"} if problems else {"INFO"}, message)
        return {"FINISHED"}


def build_extent_object(context, name, path, bounds):
    """A mesh covering the raster's bounds in the scene's space: a lat/lon
    grid on the globe (as fine as the reference globe's Globe Segments) or a
    single quad on a flat map (every flat projection is affine). Re-importing
    the same file replaces the mesh of the object it made before."""
    import math

    import numpy as np

    from .country_wireframes import (
        _ensure_globe_overlay_root_empty,
        _get_or_create_collection,
        _parent_to_globe_root,
    )
    from .geo_coord import (
        lonlat_mapping_props,
        project_point,
        stamp_lonlat_mapping,
        store_lonlat_attributes,
    )

    cs = context.scene.geo_coord_settings
    west, south, east, north = bounds
    is_globe = cs.coordinate_space == "GLOBE"
    if is_globe:
        step = 360.0 / max(8, context.scene.country_wireframe_settings.globe_segments)
        nx = max(1, min(2048, math.ceil((east - west) / step)))
        ny = max(1, min(1024, math.ceil((north - south) / step)))
    else:
        nx = ny = 1
    lons = np.linspace(west, east, nx + 1)
    lats = np.linspace(south, north, ny + 1)
    verts = [
        project_point(lat, lon, 0.0, cs, bounds=bounds, radius=cs.globe_radius)
        for lat in lats for lon in lons
    ]
    row = nx + 1
    # West-to-east, south-to-north: counter-clockwise seen from outside the
    # globe or from above a flat map, so normals face the viewer.
    faces = [
        (j * row + i, j * row + i + 1, (j + 1) * row + i + 1, (j + 1) * row + i)
        for j in range(ny) for i in range(nx)
    ]
    mesh = bpy.data.meshes.new(f"GeoTIFF_{name}_Mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    if is_globe:
        mesh.polygons.foreach_set("use_smooth", [True] * len(mesh.polygons))
    store_lonlat_attributes(mesh, lonlat_mapping_props(cs, bounds))

    obj = next((o for o in bpy.data.objects
                if o.get("geo_kind") == "geotiff_extent" and o.get("geo_source_file") == path), None)
    if obj is None:
        obj = bpy.data.objects.new(f"GeoTIFF_{name}", mesh)
        root = _get_or_create_collection(context.scene.collection, "Geo Wireframes")
        _get_or_create_collection(root, "GeoTIFF Extents").objects.link(obj)
    else:
        old = obj.data
        for material in old.materials:
            mesh.materials.append(material)
        obj.data = mesh
        if old.users == 0:
            bpy.data.meshes.remove(old)
    obj["geo_kind"] = "geotiff_extent"
    obj["geo_name"] = name
    obj["geo_source_file"] = path
    stamp_lonlat_mapping(obj, cs, bounds)
    _parent_to_globe_root(obj, _ensure_globe_overlay_root_empty(context))
    return obj


class OBJECT_OT_import_geotiff_extent(bpy.types.Operator, ImportHelper):
    bl_idname = "object.import_geotiff_extent"
    bl_label = "Import GeoTIFF as Object"
    bl_description = (
        "Make a new object covering a longitude/latitude GeoTIFF's extent, on "
        "the globe or flat map, with the GeoTIFF as its layer"
    )
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".tif"
    filter_glob: bpy.props.StringProperty(default="*.tif;*.tiff", options={"HIDDEN"})
    reproject: OBJECT_OT_add_geotiff_layer.__annotations__["reproject"]
    max_size: OBJECT_OT_add_geotiff_layer.__annotations__["max_size"]

    def execute(self, context):
        try:
            raster = read_geotiff(self.filepath, self.max_size, self.reproject)
        except GeoTiffError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Couldn't read {os.path.basename(self.filepath)}: {exc}")
            return {"CANCELLED"}

        name = os.path.splitext(os.path.basename(self.filepath))[0]
        image = _image_from_raster(name, self.filepath, raster)
        layer = build_layer_group(image)
        obj = build_extent_object(context, name, self.filepath, raster["bounds"])
        if not any(obj.data.materials):
            material = bpy.data.materials.get(f"GeoTIFF {name}") or bpy.data.materials.new(f"GeoTIFF {name}")
            if material.node_tree is None:
                material.use_nodes = True
            obj.data.materials.clear()
            obj.data.materials.append(material)
        for material in obj.data.materials:
            add_layer_to_material(material, layer)

        for other in context.selected_objects:
            other.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        west, south, east, north = raster["bounds"]
        message = (
            f"Made {obj.name} covering {west:.4f}..{east:.4f} E, {south:.4f}..{north:.4f} N"
        )
        if raster["reprojected_from"]:
            message += f" (reprojected from {raster['reprojected_from']})"
        self.report({"INFO"}, message)
        return {"FINISHED"}


def _find_layer(material_name, node_name):
    material = bpy.data.materials.get(material_name)
    node = material.node_tree.nodes.get(node_name) if material and material.node_tree else None
    if node is None or not is_layer_node(node):
        return None, None
    return material, node


class _LayerOperator:
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}
    material: bpy.props.StringProperty()
    node: bpy.props.StringProperty()


class OBJECT_OT_geotiff_layer_move(_LayerOperator, bpy.types.Operator):
    bl_idname = "object.geotiff_layer_move"
    bl_label = "Move GeoTIFF Layer"
    bl_description = "Move the layer up (drawn over the layers below it) or down"
    direction: bpy.props.EnumProperty(items=[("UP", "Up", ""), ("DOWN", "Down", "")])

    def execute(self, context):
        material, node = _find_layer(self.material, self.node)
        if node is None:
            return {"CANCELLED"}
        move_layer(material, node, self.direction)
        return {"FINISHED"}


class OBJECT_OT_geotiff_layer_remove(_LayerOperator, bpy.types.Operator):
    bl_idname = "object.geotiff_layer_remove"
    bl_label = "Remove GeoTIFF Layer"
    bl_description = "Remove the layer from this material (the image stays in the file)"

    def execute(self, context):
        material, node = _find_layer(self.material, self.node)
        if node is None:
            return {"CANCELLED"}
        remove_layer(material, node)
        return {"FINISHED"}


def _layer_object(context):
    """The object whose layers the panel shows: the active object if it has
    any, otherwise the reference globe."""
    def has_layers(obj):
        data = getattr(obj, "data", None)
        materials = getattr(data, "materials", None) or []
        return any(m and m.node_tree and any(is_layer_node(n) for n in m.node_tree.nodes)
                   for m in materials)

    obj = context.active_object
    if obj is not None and has_layers(obj):
        return obj
    globe = bpy.data.objects.get("GeoReferenceGlobe")
    if globe is not None and has_layers(globe):
        return globe
    return obj


class VIEW3D_PT_geotiff_layers(_AVLGISPanel, bpy.types.Panel):
    bl_label = "GeoTIFF Layers"
    bl_idname = "VIEW3D_PT_geotiff_layers"
    bl_parent_id = "VIEW3D_PT_country_wireframes_imagery"
    bl_order = -1

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.operator("object.add_geotiff_layer", icon="IMAGE_RGB_ALPHA")
        col.operator("object.import_geotiff_extent", icon="MESH_GRID")
        col.operator("object.fill_geo_polygons", icon="MOD_FLUIDSIM")
        obj = _layer_object(context)
        materials = [m for m in getattr(getattr(obj, "data", None), "materials", None) or [] if m]
        chains = [(m, layer_chain(m)) for m in materials]
        chains = [(m, c[0]) for m, c in chains if c and c[0]]
        if not chains:
            layout.label(text="No layers on the active object or the globe")
            return
        layout.label(text=f"On {obj.name}, top first:", icon="OBJECT_DATA")
        for material, layers in chains:
            box = layout.box()
            if len(chains) > 1:
                box.label(text=material.name, icon="MATERIAL")
            for index, node in enumerate(layers):
                col = box.column(align=True)
                row = col.row(align=True)
                row.prop(node, "mute", text="", icon="HIDE_ON" if node.mute else "HIDE_OFF", emboss=False)
                row.label(text=node.node_tree.name[len(LAYER_GROUP_PREFIX):])
                for direction, icon, enabled in (
                    ("UP", "TRIA_UP", index > 0),
                    ("DOWN", "TRIA_DOWN", index < len(layers) - 1),
                ):
                    sub = row.row(align=True)
                    sub.enabled = enabled
                    op = sub.operator("object.geotiff_layer_move", text="", icon=icon)
                    op.material, op.node, op.direction = material.name, node.name, direction
                op = row.operator("object.geotiff_layer_remove", text="", icon="X")
                op.material, op.node = material.name, node.name
                col.prop(node.inputs["Opacity"], "default_value", text="Opacity", slider=True)
                remap = node.node_tree.nodes.get("Value Range")
                if remap is not None:
                    row = col.row(align=True)
                    row.prop(remap.inputs["From Min"], "default_value", text="Min")
                    row.prop(remap.inputs["From Max"], "default_value", text="Max")


CLASSES = (
    OBJECT_OT_add_geotiff_layer,
    OBJECT_OT_import_geotiff_extent,
    OBJECT_OT_geotiff_layer_move,
    OBJECT_OT_geotiff_layer_remove,
    VIEW3D_PT_geotiff_layers,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)

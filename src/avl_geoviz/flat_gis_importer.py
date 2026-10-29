import os
import math
import json
import re
import urllib.request
import urllib.parse
from io import BytesIO

import bpy

from . import _osm_safety_shared, osm_area

# --- Library Check Helpers ---

def has_osm2geojson():
    try:
        import osm2geojson
        return True
    except ImportError:
        return False

def has_pillow():
    try:
        from PIL import Image
        return True
    except ImportError:
        return False


# --- Help Functions for Coordinate Projection ---

def get_projection_factors(ref_lat):
    """
    Computes precise WGS84 meters-per-degree factors for local flat projection.
    Ensures an exact 1:1 metric scale between Latitude and Longitude to prevent geometric squishing.
    """
    lat_rad = math.radians(ref_lat)
    
    # Precise WGS84 Ellipsoid Constants
    a = 6378137.0           # Semi-major axis (meters)
    e_sq = 0.00669437999014 # Eccentricity squared
    
    # Curvature radius in the prime vertical (East-West)
    sin_lat = math.sin(lat_rad)
    n = a / math.sqrt(1.0 - e_sq * sin_lat * sin_lat)
    
    # Curvature radius in the meridian (North-South)
    m = a * (1.0 - e_sq) / math.pow(1.0 - e_sq * sin_lat * sin_lat, 1.5)
    
    # Scaling factors (converting radians of curvature to meters per degree)
    lat_to_meters = math.radians(m)
    lon_to_meters = math.radians(n) * math.cos(lat_rad)
    
    return lat_to_meters, lon_to_meters

# --- Satellite Imagery Utilities ---

def latlon_to_tile(lat, lon, zoom):
    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0 * n)
    return x, y

def tile_to_latlon(x, y, zoom):
    n = 2.0 ** zoom
    lon = x / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n)))
    lat = math.degrees(lat_rad)
    return lat, lon

_MAX_SATELLITE_TILES = 100


def download_and_stitch_satellite(min_lat, min_lon, max_lat, max_lon, zoom, output_path):
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("Pillow library (PIL) is not available to stitch satellite imagery.")

    x1, y1 = latlon_to_tile(max_lat, min_lon, zoom)
    x2, y2 = latlon_to_tile(min_lat, max_lon, zoom)
    
    x_min, x_max = min(x1, x2), max(x1, x2)
    y_min, y_max = min(y1, y2), max(y1, y2)
    
    num_x = x_max - x_min + 1
    num_y = y_max - y_min + 1
    
    if num_x * num_y > _MAX_SATELLITE_TILES:
        raise RuntimeError(
            f"Requested area requires {num_x}x{num_y}={num_x*num_y} satellite tiles, which exceeds the safety limit of {_MAX_SATELLITE_TILES}. "
            "Please decrease Zoom Level or decrease Bounding Box size."
        )

    tile_w = 256
    tile_h = 256
    stitched = Image.new('RGB', (num_x * tile_w, num_y * tile_h))

    total_tiles = num_x * num_y
    print(f"[FlatGIS] Downloading {total_tiles} satellite tile(s) (zoom {zoom})...")
    tiles_done = 0

    for ty_idx, y in enumerate(range(y_min, y_max + 1)):
        for tx_idx, x in enumerate(range(x_min, x_max + 1)):
            url = f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{zoom}/{y}/{x}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    img_data = response.read()
                    tile_img = Image.open(BytesIO(img_data))
                    stitched.paste(tile_img, (tx_idx * tile_w, ty_idx * tile_h))
            except Exception as e:
                print(f"[FlatGIS] Failed tile {zoom}/{x}/{y}: {e}")
                placeholder = Image.new('RGB', (tile_w, tile_h), color=(120, 130, 120))
                stitched.paste(placeholder, (tx_idx * tile_w, ty_idx * tile_h))

            tiles_done += 1
            if tiles_done % 10 == 0 or tiles_done == total_tiles:
                print(f"[FlatGIS] Satellite tiles: {tiles_done}/{total_tiles} done.")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    stitched.save(output_path)
    print(f"[FlatGIS] Satellite imagery stitched and saved to {output_path}")

    img_max_lat, img_min_lon = tile_to_latlon(x_min, y_min, zoom)
    img_min_lat, img_max_lon = tile_to_latlon(x_max + 1, y_max + 1, zoom)

    return img_min_lat, img_min_lon, img_max_lat, img_max_lon


# --- Elevation API Utilities ---

def fetch_elevations_open_elevation(coords_list):
    url = "https://api.open-elevation.com/api/v1/lookup"
    chunk_size = 400
    all_results = []

    total_chunks = max(1, (len(coords_list) + chunk_size - 1) // chunk_size)
    print(f"[FlatGIS] Querying elevation API for {len(coords_list)} point(s) in {total_chunks} chunk(s)...")

    for i in range(0, len(coords_list), chunk_size):
        chunk = coords_list[i : i + chunk_size]
        payload = json.dumps({"locations": [{"latitude": lat, "longitude": lon} for lat, lon in chunk]})
        req = urllib.request.Request(
            url,
            data=payload.encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                results = res_data.get("results", [])
                all_results.extend(results)
            print(f"[FlatGIS] Elevation chunk {i // chunk_size + 1}/{total_chunks} done.")
        except Exception as e:
            print(f"[FlatGIS] Elevation chunk query failed: {e}")
            return None

    return [item.get("elevation", 0.0) for item in all_results]


# --- Geometry Nodes Compatibility Utilities ---

def create_input_socket(tree, name, socket_type, default_val=None):
    if hasattr(tree, "interface"):
        socket = tree.interface.new_socket(name=name, in_out='INPUT', socket_type=socket_type)
        if default_val is not None:
            socket.default_value = default_val
        return socket
    else:
        socket = tree.inputs.new(socket_type, name)
        if default_val is not None:
            socket.default_value = default_val
        return socket

def create_output_socket(tree, name, socket_type):
    if hasattr(tree, "interface"):
        return tree.interface.new_socket(name=name, in_out='OUTPUT', socket_type=socket_type)
    else:
        return tree.outputs.new(socket_type, name)

def get_socket_identifier(tree, name, in_out='INPUT'):
    if hasattr(tree, "interface"):
        for item in tree.interface.items_tree:
            if item.item_type == 'SOCKET' and item.in_out == in_out and item.name == name:
                return item.identifier
    else:
        sockets = tree.inputs if in_out == 'INPUT' else tree.outputs
        for socket in sockets:
            if socket.name == name:
                return socket.identifier
    return None

def set_geonode_modifier_input(mod, tree, socket_name, value):
    identifier = get_socket_identifier(tree, socket_name, 'INPUT')
    if not identifier:
        return
    # Blender 5.2+ moved GN modifier inputs off ID properties (mod[identifier]
    # now raises TypeError) onto modifier.properties.inputs, where each input
    # is a property group holding the value under "value".
    properties = getattr(mod, "properties", None)
    if properties is not None:
        properties.inputs[identifier]["value"] = value
    else:
        mod[identifier] = value


# --- Geometry Nodes Trees Creation ---

def add_bounding_box_cropping_nodes(tree, nodes, links, entry_geometry_output, x_min, x_max, y_min, y_max):
    """
    Appends a non-destructive mathematical bounding-box mask inside Geometry Nodes.
    Separates geometry cleanly by filtering components lying outside the imported meter bounds.
    """
    pos_node = nodes.new('GeometryNodeInputPosition')
    separate_xyz = nodes.new('ShaderNodeSeparateXYZ')
    links.new(pos_node.outputs['Position'], separate_xyz.inputs['Vector'])
    
    # Check X bounds
    cmp_x_min = nodes.new('ShaderNodeMath')
    cmp_x_min.operation = 'GREATER_THAN'
    cmp_x_min.inputs[1].default_value = x_min
    links.new(separate_xyz.outputs['X'], cmp_x_min.inputs[0])
    
    cmp_x_max = nodes.new('ShaderNodeMath')
    cmp_x_max.operation = 'LESS_THAN'
    cmp_x_max.inputs[1].default_value = x_max
    links.new(separate_xyz.outputs['X'], cmp_x_max.inputs[0])
    
    and_x = nodes.new('ShaderNodeMath')
    and_x.operation = 'MULTIPLY'
    links.new(cmp_x_min.outputs['Value'], and_x.inputs[0])
    links.new(cmp_x_max.outputs['Value'], and_x.inputs[1])
    
    # Check Y bounds
    cmp_y_min = nodes.new('ShaderNodeMath')
    cmp_y_min.operation = 'GREATER_THAN'
    cmp_y_min.inputs[1].default_value = y_min
    links.new(separate_xyz.outputs['Y'], cmp_y_min.inputs[0])
    
    cmp_y_max = nodes.new('ShaderNodeMath')
    cmp_y_max.operation = 'LESS_THAN'
    cmp_y_max.inputs[1].default_value = y_max
    links.new(separate_xyz.outputs['Y'], cmp_y_max.inputs[0])
    
    and_y = nodes.new('ShaderNodeMath')
    and_y.operation = 'MULTIPLY'
    links.new(cmp_y_min.outputs['Value'], and_y.inputs[0])
    links.new(cmp_y_max.outputs['Value'], and_y.inputs[1])
    
    # Final Mask
    bbox_mask = nodes.new('ShaderNodeMath')
    bbox_mask.operation = 'MULTIPLY'
    links.new(and_x.outputs['Value'], bbox_mask.inputs[0])
    links.new(and_y.outputs['Value'], bbox_mask.inputs[1])
    
    # Separate Geometry Node to cleanly filter
    sep_geom = nodes.new('GeometryNodeSeparateGeometry')
    sep_geom.domain = 'POINT'
    links.new(entry_geometry_output, sep_geom.inputs['Geometry'])
    links.new(bbox_mask.outputs['Value'], sep_geom.inputs['Selection'])
    
    return sep_geom.outputs['Selection']


def create_building_geonodes_tree(x_min=None, x_max=None, y_min=None, y_max=None):
    group_name = "FlatGIS_Buildings"
    if group_name in bpy.data.node_groups:
        bpy.data.node_groups.remove(bpy.data.node_groups[group_name])
        
    tree = bpy.data.node_groups.new(group_name, 'GeometryNodeTree')
    
    create_output_socket(tree, "Geometry", 'NodeSocketGeometry')
    create_input_socket(tree, "Geometry", 'NodeSocketGeometry')
    create_input_socket(tree, "Shadow Object", 'NodeSocketObject')
    create_input_socket(tree, "Material", 'NodeSocketMaterial')
    
    nodes = tree.nodes
    links = tree.links
    
    input_node = nodes.new('NodeGroupInput')
    output_node = nodes.new('NodeGroupOutput')
    input_node.location = (-400, 0)
    output_node.location = (900, 0)
    
    obj_info = nodes.new('GeometryNodeObjectInfo')
    obj_info.location = (-200, 0)
    obj_info.transform_space = 'RELATIVE'
    links.new(input_node.outputs['Shadow Object'], obj_info.inputs['Object'])
    
    current_geometry_output = obj_info.outputs['Geometry']
    
    # Implement non-destructive bounding box clip if constraints are valid
    if x_min is not None and x_max is not None and y_min is not None and y_max is not None:
        current_geometry_output = add_bounding_box_cropping_nodes(
            tree, nodes, links, current_geometry_output, x_min, x_max, y_min, y_max
        )
    
    # Read Height Named Attribute
    attr_node = nodes.new('GeometryNodeInputNamedAttribute')
    attr_node.location = (-200, -200)
    attr_node.data_type = 'FLOAT'
    attr_node.inputs['Name'].default_value = 'height'
    
    # Extrude Mesh
    extrude = nodes.new('GeometryNodeExtrudeMesh')
    extrude.location = (200, 0)
    extrude.mode = 'FACES'
    links.new(current_geometry_output, extrude.inputs['Mesh'])
    links.new(attr_node.outputs['Attribute'], extrude.inputs['Offset Scale'])
    
    # Flip bottom faces
    flip = nodes.new('GeometryNodeFlipFaces')
    flip.location = (200, -200)
    links.new(current_geometry_output, flip.inputs['Mesh'])
    
    # Join bottom + sides/top
    join = nodes.new('GeometryNodeJoinGeometry')
    join.location = (450, 0)
    links.new(extrude.outputs['Mesh'], join.inputs['Geometry'])
    links.new(flip.outputs['Mesh'], join.inputs['Geometry'])
    
    # Set Material (Removed destructive merge operators)
    set_mat = nodes.new('GeometryNodeSetMaterial')
    set_mat.location = (650, 0)
    links.new(join.outputs['Geometry'], set_mat.inputs['Geometry'])
    links.new(input_node.outputs['Material'], set_mat.inputs['Material'])
    
    links.new(set_mat.outputs['Geometry'], output_node.inputs['Geometry'])
    
    return tree

def create_road_geonodes_tree(x_min=None, x_max=None, y_min=None, y_max=None):
    group_name = "FlatGIS_Roads"
    if group_name in bpy.data.node_groups:
        bpy.data.node_groups.remove(bpy.data.node_groups[group_name])
        
    tree = bpy.data.node_groups.new(group_name, 'GeometryNodeTree')
    
    create_output_socket(tree, "Geometry", 'NodeSocketGeometry')
    create_input_socket(tree, "Geometry", 'NodeSocketGeometry')
    create_input_socket(tree, "Shadow Object", 'NodeSocketObject')
    create_input_socket(tree, "Material", 'NodeSocketMaterial')
    create_input_socket(tree, "Default Width", 'NodeSocketFloat', 4.0)
    
    nodes = tree.nodes
    links = tree.links
    
    input_node = nodes.new('NodeGroupInput')
    output_node = nodes.new('NodeGroupOutput')
    input_node.location = (-400, 0)
    output_node.location = (900, 0)
    
    obj_info = nodes.new('GeometryNodeObjectInfo')
    obj_info.location = (-200, 0)
    obj_info.transform_space = 'RELATIVE'
    links.new(input_node.outputs['Shadow Object'], obj_info.inputs['Object'])
    
    current_geometry_output = obj_info.outputs['Geometry']
    
    # Implement non-destructive bounding box clip if constraints are valid
    if x_min is not None and x_max is not None and y_min is not None and y_max is not None:
        current_geometry_output = add_bounding_box_cropping_nodes(
            tree, nodes, links, current_geometry_output, x_min, x_max, y_min, y_max
        )
    
    # Read width attribute
    attr_node = nodes.new('GeometryNodeInputNamedAttribute')
    attr_node.location = (-200, -200)
    attr_node.data_type = 'FLOAT'
    attr_node.inputs['Name'].default_value = 'width'
    
    # Check if width > 0.01
    compare = nodes.new('ShaderNodeMath')
    compare.operation = 'GREATER_THAN'
    compare.inputs[1].default_value = 0.01
    compare.location = (0, -200)
    links.new(attr_node.outputs['Attribute'], compare.inputs[0])
    
    # Fallback to default width if not valid
    switch = nodes.new('GeometryNodeSwitch')
    switch.location = (150, -200)
    switch.input_type = 'FLOAT'
    links.new(compare.outputs['Value'], switch.inputs['Switch'])
    links.new(input_node.outputs['Default Width'], switch.inputs['False'])
    links.new(attr_node.outputs['Attribute'], switch.inputs['True'])
    
    # Profile Line coordinates Vector Generation
    mult_neg = nodes.new('ShaderNodeMath')
    mult_neg.operation = 'MULTIPLY'
    mult_neg.inputs[1].default_value = -0.5
    mult_neg.location = (150, -400)
    links.new(switch.outputs['Output'], mult_neg.inputs[0])
    
    mult_pos = nodes.new('ShaderNodeMath')
    mult_pos.operation = 'MULTIPLY'
    mult_pos.inputs[1].default_value = 0.5
    mult_pos.location = (150, -550)
    links.new(switch.outputs['Output'], mult_pos.inputs[0])
    
    combine_start = nodes.new('ShaderNodeCombineXYZ')
    combine_start.location = (300, -400)
    links.new(mult_neg.outputs['Value'], combine_start.inputs['X'])
    
    combine_end = nodes.new('ShaderNodeCombineXYZ')
    combine_end.location = (300, -550)
    links.new(mult_pos.outputs['Value'], combine_end.inputs['X'])
    
    profile_line = nodes.new('GeometryNodeCurvePrimitiveLine')
    profile_line.location = (450, -400)
    links.new(combine_start.outputs['Vector'], profile_line.inputs['Start'])
    links.new(combine_end.outputs['Vector'], profile_line.inputs['End'])
    
    # Curve to Mesh Conversion
    curve_to_mesh = nodes.new('GeometryNodeCurveToMesh')
    curve_to_mesh.location = (450, 0)
    links.new(current_geometry_output, curve_to_mesh.inputs['Curve'])
    links.new(profile_line.outputs['Curve'], curve_to_mesh.inputs['Profile Curve'])
    
    # Set Material
    set_mat = nodes.new('GeometryNodeSetMaterial')
    set_mat.location = (650, 0)
    links.new(curve_to_mesh.outputs['Mesh'], set_mat.inputs['Geometry'])
    links.new(input_node.outputs['Material'], set_mat.inputs['Material'])
    
    links.new(set_mat.outputs['Geometry'], output_node.inputs['Geometry'])
    
    return tree

def create_marker_geonodes_tree(x_min=None, x_max=None, y_min=None, y_max=None):
    group_name = "FlatGIS_Markers"
    if group_name in bpy.data.node_groups:
        bpy.data.node_groups.remove(bpy.data.node_groups[group_name])
        
    tree = bpy.data.node_groups.new(group_name, 'GeometryNodeTree')
    
    create_output_socket(tree, "Geometry", 'NodeSocketGeometry')
    create_input_socket(tree, "Geometry", 'NodeSocketGeometry')
    create_input_socket(tree, "Shadow Object", 'NodeSocketObject')
    create_input_socket(tree, "Instance Object", 'NodeSocketObject')
    create_input_socket(tree, "Scale", 'NodeSocketFloat', 1.0)
    
    nodes = tree.nodes
    links = tree.links
    
    input_node = nodes.new('NodeGroupInput')
    output_node = nodes.new('NodeGroupOutput')
    input_node.location = (-400, 0)
    output_node.location = (800, 0)
    
    obj_info = nodes.new('GeometryNodeObjectInfo')
    obj_info.location = (-200, 0)
    obj_info.transform_space = 'RELATIVE'
    links.new(input_node.outputs['Shadow Object'], obj_info.inputs['Object'])
    
    current_geometry_output = obj_info.outputs['Geometry']
    
    if x_min is not None and x_max is not None and y_min is not None and y_max is not None:
        current_geometry_output = add_bounding_box_cropping_nodes(
            tree, nodes, links, current_geometry_output, x_min, x_max, y_min, y_max
        )
        
    inst_info = nodes.new('GeometryNodeObjectInfo')
    inst_info.location = (-200, -200)
    inst_info.transform_space = 'ORIGINAL'
    links.new(input_node.outputs['Instance Object'], inst_info.inputs['Object'])
    
    # Instance on Points
    inst_points = nodes.new('GeometryNodeInstanceOnPoints')
    inst_points.location = (400, 0)
    links.new(current_geometry_output, inst_points.inputs['Points'])
    links.new(inst_info.outputs['Geometry'], inst_points.inputs['Instance'])
    
    combine_scale = nodes.new('ShaderNodeCombineXYZ')
    combine_scale.location = (150, -350)
    links.new(input_node.outputs['Scale'], combine_scale.inputs['X'])
    links.new(input_node.outputs['Scale'], combine_scale.inputs['Y'])
    links.new(input_node.outputs['Scale'], combine_scale.inputs['Z'])
    links.new(combine_scale.outputs['Vector'], inst_points.inputs['Scale'])
    
    links.new(inst_points.outputs['Instances'], output_node.inputs['Geometry'])
    
    return tree

def create_terrain_geonodes_tree():
    group_name = "FlatGIS_Terrain"
    if group_name in bpy.data.node_groups:
        return bpy.data.node_groups[group_name]
        
    tree = bpy.data.node_groups.new(group_name, 'GeometryNodeTree')
    
    create_output_socket(tree, "Geometry", 'NodeSocketGeometry')
    create_input_socket(tree, "Geometry", 'NodeSocketGeometry')
    create_input_socket(tree, "Shadow Object", 'NodeSocketObject')
    create_input_socket(tree, "Material", 'NodeSocketMaterial')
    
    nodes = tree.nodes
    links = tree.links
    
    input_node = nodes.new('NodeGroupInput')
    output_node = nodes.new('NodeGroupOutput')
    input_node.location = (-400, 0)
    output_node.location = (600, 0)
    
    obj_info = nodes.new('GeometryNodeObjectInfo')
    obj_info.location = (-200, 0)
    obj_info.transform_space = 'RELATIVE'
    links.new(input_node.outputs['Shadow Object'], obj_info.inputs['Object'])
    
    set_mat = nodes.new('GeometryNodeSetMaterial')
    set_mat.location = (200, 0)
    links.new(obj_info.outputs['Geometry'], set_mat.inputs['Geometry'])
    links.new(input_node.outputs['Material'], set_mat.inputs['Material'])
    
    links.new(set_mat.outputs['Geometry'], output_node.inputs['Geometry'])
    
    return tree


# --- Materials Creation ---

def create_material(name, color=(0.8, 0.8, 0.8, 1.0), roughness=0.5):
    if name in bpy.data.materials:
        return bpy.data.materials[name]
        
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    nodes.clear()
    
    bsdf = nodes.new('ShaderNodeBsdfPrincipled')
    if 'Base Color' in bsdf.inputs:
        bsdf.inputs['Base Color'].default_value = color
    if 'Roughness' in bsdf.inputs:
        bsdf.inputs['Roughness'].default_value = roughness
        
    output = nodes.new('ShaderNodeOutputMaterial')
    mat.node_tree.links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    
    return mat

def create_satellite_material(image_path):
    mat_name = "FlatGIS_Satellite_Material"
    if mat_name in bpy.data.materials:
        mat = bpy.data.materials[mat_name]
    else:
        mat = bpy.data.materials.new(name=mat_name)
        
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    
    bsdf = nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 0)
    if 'Roughness' in bsdf.inputs:
        bsdf.inputs['Roughness'].default_value = 1.0
        
    output = nodes.new('ShaderNodeOutputMaterial')
    output.location = (400, 0)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    
    tex = nodes.new('ShaderNodeTexImage')
    tex.location = (-100, 0)
    
    img_name = os.path.basename(image_path)
    if img_name in bpy.data.images:
        img = bpy.data.images[img_name]
        img.filepath = image_path
        img.reload()
    else:
        img = bpy.data.images.load(image_path)
        
    tex.image = img
    links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
    
    return mat


# --- Helper Object Creation (Default Marker Pin) ---

def create_default_marker_object():
    obj_name = "FlatGIS_Default_Marker"
    if obj_name in bpy.data.objects:
        return bpy.data.objects[obj_name]
        
    mesh = bpy.data.meshes.new(obj_name)
    obj = bpy.data.objects.new(obj_name, mesh)
    
    col_name = "FlatGIS_Helpers"
    if col_name in bpy.data.collections:
        coll = bpy.data.collections[col_name]
    else:
        coll = bpy.data.collections.new(col_name)
        bpy.context.scene.collection.children.link(coll)
    coll.objects.link(obj)
    
    # Create cone - built via the primitive_cone_add operator rather than
    # bmesh (no compiled bmesh extension in the pip-installed `bpy` wheel,
    # unlike a full Blender install - see cli/HEADLESS.md). The operator
    # always creates its own new object+mesh rather than filling an
    # existing one, so it's built into a throwaway object here and its
    # mesh swapped onto `obj` in place of the empty placeholder mesh
    # created above (which is then discarded).
    bpy.ops.mesh.primitive_cone_add(
        vertices=8,
        radius1=0.2,
        radius2=0.0,
        depth=1.0,
        end_fill_type='TRIFAN',  # matches the original bmesh call's cap_tris=True
    )
    temp_obj = bpy.context.active_object
    cone_mesh = temp_obj.data
    for v in cone_mesh.vertices:
        v.co.z += 0.5  # Origin at the bottom tip
    cone_mesh.update()
    obj.data = cone_mesh
    bpy.data.objects.remove(temp_obj, do_unlink=True)
    bpy.data.meshes.remove(mesh)  # the now-unused placeholder
    mesh = cone_mesh
    
    # Red material
    mat = create_material("FlatGIS_Marker_Material", color=(1.0, 0.05, 0.05, 1.0), roughness=0.5)
    obj.data.materials.append(mat)
    
    obj.hide_render = True
    return obj


# --- Collection Setup ---

def setup_collections(context, clear_existing=False):
    parent_name = "Flat GIS Import"
    if parent_name in bpy.data.collections:
        parent_coll = bpy.data.collections[parent_name]
    else:
        parent_coll = bpy.data.collections.new(parent_name)
        context.scene.collection.children.link(parent_coll)
        
    shadow_name = "Shadow_Objects"
    if shadow_name in bpy.data.collections:
        shadow_coll = bpy.data.collections[shadow_name]
    else:
        shadow_coll = bpy.data.collections.new(shadow_name)
        parent_coll.children.link(shadow_coll)
        
    # Hide shadow collection in active view layer
    def exclude_collection_recursive(layer_coll, coll):
        if layer_coll.collection == coll:
            layer_coll.exclude = True
            return True
        for child in layer_coll.children:
            if exclude_collection_recursive(child, coll):
                return True
        return False
        
    exclude_collection_recursive(context.view_layer.layer_collection, shadow_coll)
    
    visible_name = "Visible_Objects"
    if visible_name in bpy.data.collections:
        visible_coll = bpy.data.collections[visible_name]
    else:
        visible_coll = bpy.data.collections.new(visible_name)
        parent_coll.children.link(visible_coll)
        
    if clear_existing:
        def delete_objects_in_coll(coll):
            for obj in list(coll.objects):
                data = obj.data
                bpy.data.objects.remove(obj, do_unlink=True)
                if data and data.users == 0:
                    if isinstance(data, bpy.types.Mesh):
                        bpy.data.meshes.remove(data)
                    elif isinstance(data, bpy.types.Curve):
                        bpy.data.curves.remove(data)
        delete_objects_in_coll(shadow_coll)
        delete_objects_in_coll(visible_coll)
        
    return shadow_coll, visible_coll


# --- Flat Grid Mesh Generation ---

def create_grid_mesh(x_min, y_min, x_max, y_max, resolution, name):
    mesh = bpy.data.meshes.new(name)
    
    # Vertices
    verts = []
    dx = (x_max - x_min) / (resolution - 1)
    dy = (y_max - y_min) / (resolution - 1)
    
    for i in range(resolution):
        y = y_min + i * dy
        for j in range(resolution):
            x = x_min + j * dx
            verts.append((x, y, 0.0))
            
    # Faces
    faces = []
    for i in range(resolution - 1):
        for j in range(resolution - 1):
            v0 = i * resolution + j
            v1 = v0 + 1
            v2 = (i + 1) * resolution + j + 1
            v3 = (i + 1) * resolution + j
            faces.append((v0, v1, v2, v3))
            
    mesh.from_pydata(verts, [], faces)
    
    # Generate UV Map
    mesh.uv_layers.new(name="UVMap")
    uv_layer = mesh.uv_layers.active.data
    for face in mesh.polygons:
        for loop_idx in face.loop_indices:
            vert_idx = mesh.loops[loop_idx].vertex_index
            v_co = mesh.vertices[vert_idx].co
            u = (v_co.x - x_min) / (x_max - x_min)
            v = (v_co.y - y_min) / (y_max - y_min)
            uv_layer[loop_idx].uv = (u, v)
            
    mesh.update()
    return mesh


# --- Polygon to Mesh Generation Helpers (Point In Polygon) ---

def point_in_polygon(x, y, poly_rings):
    def ring_contains(ring, px, py):
        n = len(ring)
        inside = False
        p1x, p1y = ring[0][:2]
        # Defensive init only: when p1y == p2y (a horizontal edge), the
        # `py > min(p1y, p2y)` and `py <= max(p1y, p2y)` guards above
        # collapse to `py > p1y and py <= p1y`, which can never both hold -
        # so `xinters` is never actually read on that path. Still seeded
        # here so the variable can't raise UnboundLocalError if this logic
        # is ever touched again.
        xinters = None
        for i in range(n + 1):
            p2x, p2y = ring[i % n][:2]
            if py > min(p1y, p2y):
                if py <= max(p1y, p2y):
                    if px <= max(p1x, p2x):
                        if p1y != p2y:
                            xinters = (py - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                        if p1x == p2x or px <= xinters:
                            inside = not inside
            p1x, p1y = p2x, p2y
        return inside

    if not poly_rings:
        return False
    if not ring_contains(poly_rings[0], x, y):
        return False
    for hole in poly_rings[1:]:
        if ring_contains(hole, x, y):
            return False
    return True

def create_mesh_from_polygons(polygons_list, name):
    curve_data = bpy.data.curves.new(name=f"temp_curve_{name}", type='CURVE')
    curve_data.dimensions = '2D'
    curve_data.fill_mode = 'BOTH'
    
    temp_obj = bpy.data.objects.new(name=f"temp_obj_{name}", object_data=curve_data)
    bpy.context.scene.collection.objects.link(temp_obj)
    
    for rings, height in polygons_list:
        for ring in rings:
            spline = curve_data.splines.new(type='POLY')
            spline.points.add(len(ring) - 1)
            for i, (x, y) in enumerate(ring):
                spline.points[i].co = (x, y, 0.0, 1.0)
            spline.use_cyclic_u = True
            
    bpy.context.view_layer.update()
    
    depsgraph = bpy.context.evaluated_depsgraph_get()
    mesh = bpy.data.meshes.new_from_object(
        temp_obj.evaluated_get(depsgraph),
        preserve_all_data_layers=True,
        depsgraph=depsgraph
    )
    
    bpy.data.objects.remove(temp_obj, do_unlink=True)
    bpy.data.curves.remove(curve_data, do_unlink=True)
    
    return mesh


# --- Unified Core Import Pipeline ---

def import_flat_gis_geojson_data(context, geojson_data, settings, bbox_bounds=None, warnings=None):
    # Determine reference bounds
    if bbox_bounds:
        min_lat, min_lon, max_lat, max_lon = bbox_bounds
    else:
        # Calculate bounding box of GeoJSON coordinates
        min_lat = 90.0
        max_lat = -90.0
        min_lon = 180.0
        max_lon = -180.0
        has_coords = False
        
        def parse_coords(coords):
            nonlocal min_lat, max_lat, min_lon, max_lon, has_coords
            if isinstance(coords, list):
                if len(coords) == 2 and isinstance(coords[0], (int, float)) and isinstance(coords[1], (int, float)):
                    lon, lat = coords
                    if lat < min_lat: min_lat = lat
                    if lat > max_lat: max_lat = lat
                    if lon < min_lon: min_lon = lon
                    if lon > max_lon: max_lon = lon
                    has_coords = True
                else:
                    for item in coords:
                        parse_coords(item)
                        
        for feat in geojson_data.get('features', []):
            geom = feat.get('geometry')
            if geom:
                parse_coords(geom.get('coordinates'))
                
        if not has_coords:
            raise RuntimeError("GeoJSON file contains no coordinate features.")
        bbox_bounds = (min_lat, min_lon, max_lat, max_lon)

    ref_lat = (min_lat + max_lat) / 2.0
    ref_lon = (min_lon + max_lon) / 2.0
    
    # --- AUTO-DETECT AND FIX SWAPPED LAT/LON FLIPS ---
    # A ref_lat outside [-90, 90] is the only case a swap can be confidently
    # inferred from magnitude alone - a valid latitude is never out of that
    # range on its own axis. (A magnitude-based southern-latitude/eastern-
    # longitude heuristic used to live here too, but real data centered
    # south of -45 deg between 0-90 deg east - e.g. the Prince Edward
    # Islands, ~-46.9,37.8 - would get wrongly swapped by it, so it was
    # dropped rather than risk a false-positive swap.)
    if abs(ref_lat) > 90.0:
        warning_msg = (
            f"Detected out-of-range latitude (ref_lat={ref_lat:.3f}); "
            "Lat/Lon order looked flipped, so it was swapped automatically. "
            "Double-check the imported geometry's location."
        )
        print(f"[FlatGIS] WARNING: {warning_msg}")
        if warnings is not None:
            warnings.append(warning_msg)
        min_lat, min_lon = min_lon, min_lat
        max_lat, max_lon = max_lon, max_lat
        ref_lat, ref_lon = ref_lon, ref_lat
    # -------------------------------------------------

    lat_to_meters, lon_to_meters = get_projection_factors(ref_lat)
    
    # Calculate boundary metrics up front before parsing features
    bound_x_min = (min_lon - ref_lon) * lon_to_meters
    bound_x_max = (max_lon - ref_lon) * lon_to_meters
    bound_y_min = (min_lat - ref_lat) * lat_to_meters
    bound_y_max = (max_lat - ref_lat) * lat_to_meters
    
    print(f"[FlatGIS] Bounds: Lat({min_lat} to {max_lat}), Lon({min_lon} to {max_lon})")
    print(f"[FlatGIS] Metric Bounding Box Limits: X({bound_x_min} to {bound_x_max}), Y({bound_y_min} to {bound_y_max})")
    
    # 1. Setup collections
    shadow_coll, visible_coll = setup_collections(context, clear_existing=settings.clear_existing)
    
    # 2. Materials
    building_mat = create_material("FlatGIS_Building_Material", color=(0.85, 0.85, 0.82, 1.0), roughness=0.6)
    road_mat = create_material("FlatGIS_Road_Material", color=(0.18, 0.18, 0.20, 1.0), roughness=0.9)
    terrain_mat = create_material("FlatGIS_Terrain_Material", color=(0.15, 0.40, 0.15, 1.0), roughness=0.8)
    
    # 3. Import Terrain & Satellite Imagery
    terrain_x_min = bound_x_min
    terrain_y_min = bound_y_min
    terrain_x_max = bound_x_max
    terrain_y_max = bound_y_max
    
    satellite_ready = False
    satellite_img_path = ""
    
    if settings.import_satellite:
        # Next to the saved .blend, so the image travels with the project;
        # an unsaved file uses the extension's own user folder.
        if bpy.data.filepath:
            assets_dir = os.path.dirname(bpy.data.filepath)
        else:
            assets_dir = bpy.utils.extension_path_user(__package__, path="satellite", create=True)
        
        satellite_img_path = os.path.join(assets_dir, "flat_gis_satellite.png")
        
        try:
            print("[FlatGIS] Fetching satellite imagery...")
            img_min_lat, img_min_lon, img_max_lat, img_max_lon = download_and_stitch_satellite(
                min_lat, min_lon, max_lat, max_lon, settings.satellite_zoom, satellite_img_path
            )
            
            # Recalculate terrain dimensions to match stitched image bounds
            terrain_x_min = (img_min_lon - ref_lon) * lon_to_meters
            terrain_y_min = (img_min_lat - ref_lat) * lat_to_meters
            terrain_x_max = (img_max_lon - ref_lon) * lon_to_meters
            terrain_y_max = (img_max_lat - ref_lat) * lat_to_meters
            
            terrain_mat = create_satellite_material(satellite_img_path)
            satellite_ready = True
            print("[FlatGIS] Satellite image loaded successfully.")
        except Exception as e:
            print(f"[FlatGIS] Satellite download failed: {e}")
            
    # Generate Terrain Grid
    if settings.import_terrain or satellite_ready:
        print("[FlatGIS] Building terrain grid...")
        res = settings.terrain_resolution if settings.import_terrain else 4
        grid_mesh = create_grid_mesh(terrain_x_min, terrain_y_min, terrain_x_max, terrain_y_max, res, "Shadow_Terrain")
        
        # Fetch elevations if checked and grid size > 4
        if settings.import_terrain and settings.fetch_elevation:
            print("[FlatGIS] Fetching terrain elevations...")
            coords_to_query = []
            for v in grid_mesh.vertices:
                lon = ref_lon + v.co.x / lon_to_meters
                lat = ref_lat + v.co.y / lat_to_meters
                coords_to_query.append((lat, lon))
                
            elevations = fetch_elevations_open_elevation(coords_to_query)
            if elevations:
                min_el = min(elevations)
                for idx, elev in enumerate(elevations):
                    grid_mesh.vertices[idx].co.z = elev - min_el
                grid_mesh.update()
                print("[FlatGIS] Terrain elevations applied.")
            else:
                print("[FlatGIS] Terrain elevations fetch failed. Using flat terrain.")
                
        shadow_terrain_obj = bpy.data.objects.new("Shadow_Terrain", grid_mesh)
        shadow_coll.objects.link(shadow_terrain_obj)
        
        visible_terrain_mesh = bpy.data.meshes.new("OSM_Terrain")
        visible_terrain_obj = bpy.data.objects.new("OSM_Terrain", visible_terrain_mesh)
        visible_coll.objects.link(visible_terrain_obj)
        
        # Connect through Geometry Nodes
        mod = visible_terrain_obj.modifiers.new(name="FlatGIS_Terrain", type='NODES')
        geonode_tree = create_terrain_geonodes_tree()
        mod.node_group = geonode_tree
        
    # 4. Parse Features
    print("[FlatGIS] Parsing GeoJSON features...")
    buildings_list = []
    roads_list = []
    markers_dict = {}
    
    for feat in geojson_data.get('features', []):
        geom = feat.get('geometry')
        if not geom:
            continue
        geom_type = geom.get('type')
        props = feat.get('properties', {})
        # osm2geojson nests OSM tags under "tags"; local GeoJSON files
        # usually have them at the top level.
        if isinstance(props.get('tags'), dict):
            props = {**props, **props['tags']}
        
        is_building = 'building' in props or props.get('building') is not None
        height = 10.0
        if is_building:
            if 'height' in props:
                try:
                    height = float(props['height'])
                except:
                    pass
            elif 'building:levels' in props:
                try:
                    height = float(props['building:levels']) * 3.0
                except:
                    pass
                    
        is_road = 'highway' in props or props.get('highway') is not None
        width = 4.0
        if is_road:
            if 'width' in props:
                try:
                    width = float(props['width'])
                except:
                    pass
            elif 'lanes' in props:
                try:
                    width = float(props['lanes']) * 3.0
                except:
                    pass
                    
        coords = geom.get('coordinates')
        if not coords:
            continue
            
        # ALL geographic paths are pulled directly without simplification algorithms or vertex cleanup loops.
        if geom_type == 'Polygon' and is_building:
            rings = []
            for ring in coords:
                ring_projected = []
                for lon, lat in ring:
                    # Scaling applied explicitly inside localized origin window *before* mapping vertex features
                    x = (lon - ref_lon) * lon_to_meters
                    y = (lat - ref_lat) * lat_to_meters
                    ring_projected.append((x, y))
                if len(ring_projected) >= 3:
                    rings.append(ring_projected)
            if rings:
                buildings_list.append((rings, height))
                
        elif geom_type == 'MultiPolygon' and is_building:
            for poly in coords:
                rings = []
                for ring in poly:
                    ring_projected = []
                    for lon, lat in ring:
                        x = (lon - ref_lon) * lon_to_meters
                        y = (lat - ref_lat) * lat_to_meters
                        ring_projected.append((x, y))
                    if len(ring_projected) >= 3:
                        rings.append(ring_projected)
                if rings:
                    buildings_list.append((rings, height))
                    
        elif geom_type == 'LineString' and is_road:
            line_projected = []
            for lon, lat in coords:
                x = (lon - ref_lon) * lon_to_meters
                y = (lat - ref_lat) * lat_to_meters
                line_projected.append((x, y))
            if len(line_projected) >= 2:
                roads_list.append((line_projected, width))
                
        elif geom_type == 'MultiLineString' and is_road:
            for line in coords:
                line_projected = []
                for lon, lat in line:
                    x = (lon - ref_lon) * lon_to_meters
                    y = (lat - ref_lat) * lat_to_meters
                    line_projected.append((x, y))
                if len(line_projected) >= 2:
                    roads_list.append((line_projected, width))
                    
        elif geom_type == 'Point':
            lon, lat = coords
            x = (lon - ref_lon) * lon_to_meters
            y = (lat - ref_lat) * lat_to_meters
            
            m_type = 'Other'
            if 'natural' in props and props['natural'] == 'tree':
                m_type = 'Tree'
            elif 'amenity' in props:
                m_type = 'Amenity'
            elif 'highway' in props:
                m_type = 'Transit'
                
            if m_type not in markers_dict:
                markers_dict[m_type] = []
            markers_dict[m_type].append((x, y))
            
        elif geom_type == 'MultiPoint':
            for pt in coords:
                lon, lat = pt
                x = (lon - ref_lon) * lon_to_meters
                y = (lat - ref_lat) * lat_to_meters
                
                m_type = 'Other'
                if 'natural' in props and props['natural'] == 'tree':
                    m_type = 'Tree'
                elif 'amenity' in props:
                    m_type = 'Amenity'
                    
                if m_type not in markers_dict:
                    markers_dict[m_type] = []
                markers_dict[m_type].append((x, y))

    # 5. Build Buildings Mesh & Object Info
    if buildings_list:
        print(f"[FlatGIS] Building {len(buildings_list)} footprints...")
        shadow_mesh = create_mesh_from_polygons(buildings_list, "Buildings")
        shadow_obj = bpy.data.objects.new("Shadow_Buildings", shadow_mesh)
        shadow_coll.objects.link(shadow_obj)
        
        # Add Height attributes
        height_attr = shadow_mesh.attributes.new(name="height", type="FLOAT", domain="FACE")
        for face in shadow_mesh.polygons:
            cx = sum(shadow_mesh.vertices[v].co.x for v in face.vertices) / len(face.vertices)
            cy = sum(shadow_mesh.vertices[v].co.y for v in face.vertices) / len(face.vertices)
            
            found = False
            for rings, height in buildings_list:
                if point_in_polygon(cx, cy, rings):
                    height_attr.data[face.index].value = height
                    found = True
                    break
            if not found:
                height_attr.data[face.index].value = 10.0
                
        visible_mesh = bpy.data.meshes.new("OSM_Buildings")
        visible_obj = bpy.data.objects.new("OSM_Buildings", visible_mesh)
        visible_coll.objects.link(visible_obj)
        
        mod = visible_obj.modifiers.new(name="FlatGIS_Buildings", type='NODES')
        geonode_tree = create_building_geonodes_tree(bound_x_min, bound_x_max, bound_y_min, bound_y_max)
        mod.node_group = geonode_tree
        
        set_geonode_modifier_input(mod, geonode_tree, "Shadow Object", shadow_obj)
        set_geonode_modifier_input(mod, geonode_tree, "Material", building_mat)
        
    # 6. Build Roads Curve & Object Info
    if roads_list:
        print(f"[FlatGIS] Building {len(roads_list)} roads...")
        # A Curves (not legacy Curve) datablock: only it carries the
        # per-point "width" attribute the road node tree reads.
        curve_data = bpy.data.hair_curves.new(name="Shadow_Roads_Curve")
        curve_data.add_curves([len(coords) for coords, _width in roads_list])
        curve_data.set_types(type='POLY')

        positions = []
        points_widths = []
        for coords, width in roads_list:
            for x, y in coords:
                positions.extend((x, y, 0.0))
                points_widths.append(width)

        curve_data.attributes["position"].data.foreach_set("vector", positions)
        width_attr = curve_data.attributes.new(name="width", type="FLOAT", domain="POINT")
        width_attr.data.foreach_set("value", points_widths)
            
        shadow_obj = bpy.data.objects.new("Shadow_Roads", curve_data)
        shadow_coll.objects.link(shadow_obj)
        
        visible_mesh = bpy.data.meshes.new("OSM_Roads")
        visible_obj = bpy.data.objects.new("OSM_Roads", visible_mesh)
        visible_coll.objects.link(visible_obj)
        
        mod = visible_obj.modifiers.new(name="FlatGIS_Roads", type='NODES')
        geonode_tree = create_road_geonodes_tree(bound_x_min, bound_x_max, bound_y_min, bound_y_max)
        mod.node_group = geonode_tree
        
        set_geonode_modifier_input(mod, geonode_tree, "Shadow Object", shadow_obj)
        set_geonode_modifier_input(mod, geonode_tree, "Material", road_mat)
        set_geonode_modifier_input(mod, geonode_tree, "Default Width", 4.0)

    # 7. Build Markers Vertices & Object Info
    if markers_dict:
        print("[FlatGIS] Building markers...")
        default_marker_obj = create_default_marker_object()
        
        for m_type, coords_list in markers_dict.items():
            if not coords_list:
                continue
                
            mesh = bpy.data.meshes.new(f"Shadow_Markers_{m_type}")
            verts = [(x, y, 0.0) for x, y in coords_list]
            mesh.from_pydata(verts, [], [])
            mesh.update()
            
            shadow_obj = bpy.data.objects.new(f"Shadow_Markers_{m_type}", mesh)
            shadow_coll.objects.link(shadow_obj)
            
            visible_mesh = bpy.data.meshes.new(f"OSM_Markers_{m_type}")
            visible_obj = bpy.data.objects.new(f"OSM_Markers_{m_type}", visible_mesh)
            visible_coll.objects.link(visible_obj)
            
            mod = visible_obj.modifiers.new(name=f"FlatGIS_Markers_{m_type}", type='NODES')
            geonode_tree = create_marker_geonodes_tree(bound_x_min, bound_x_max, bound_y_min, bound_y_max)
            mod.node_group = geonode_tree
            
            set_geonode_modifier_input(mod, geonode_tree, "Shadow Object", shadow_obj)
            set_geonode_modifier_input(mod, geonode_tree, "Instance Object", default_marker_obj)
            set_geonode_modifier_input(mod, geonode_tree, "Scale", 1.0)
            
    # Force viewport update and redraw the UI region immediately
    bpy.context.view_layer.update()
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
                
    print("[FlatGIS] Import pipeline complete!")


# --- Property Group ---

class FlatGISSettings(bpy.types.PropertyGroup):
    # The area, feature preset and request limits are shared with the
    # globe / world-map importer, in osm_area.AVLOSMSettings.
    import_osm: bpy.props.BoolProperty(
        name="OSM Features",
        description=(
            "Build the buildings, roads and points of the preset chosen under "
            "Area & Features. Off builds only terrain and satellite imagery"
        ),
        default=True,
    )

    import_terrain: bpy.props.BoolProperty(
        name="Terrain Grid",
        description="Import terrain grid mesh",
        default=True
    )
    terrain_resolution: bpy.props.IntProperty(
        name="Resolution",
        description="Grid resolution (subdivisions)",
        default=30,
        min=4,
        max=200
    )
    fetch_elevation: bpy.props.BoolProperty(
        name="Fetch Elevation (API)",
        description="Fetch terrain elevations from public API",
        default=True
    )
    
    import_satellite: bpy.props.BoolProperty(
        name="Satellite Texture",
        description="Fetch and map satellite texture",
        default=True
    )
    satellite_zoom: bpy.props.IntProperty(
        name="Zoom Level",
        description="Satellite tile zoom level (higher is more detailed)",
        default=16,
        min=10,
        max=19
    )
    
    clear_existing: bpy.props.BoolProperty(
        name="Clear Existing Data",
        description="Clear Flat GIS collections before importing",
        default=True
    )
    
    geojson_filepath: bpy.props.StringProperty(
        name="GeoJSON Path",
        description="Select local GeoJSON file to import",
        default="",
        subtype='FILE_PATH'
    )


# --- Operators ---

# What Local Flat Map can build from each OSM key - extruded buildings, roads
# with widths, and point markers (sorted into Tree / Amenity / Transit /
# Other) - and the element types to ask Overpass for. Filters on any other
# key (including building:part, which the builder doesn't treat as a
# building) are left out of its query, so a broad preset like Everything
# only fetches what gets built. A bare "highway" also asks for highway nodes
# (bus stops, crossings, signals) as Transit markers; highway=<road type>
# values only exist on ways.
_BUILDABLE_KEYS = {
    "building": ("buildings", ("way", "relation")),
    "highway": ("roads", ("way",)),
    "amenity": ("points", ("node",)),
    "shop": ("points", ("node",)),
    "tourism": ("points", ("node",)),
    "natural": ("points", ("node",)),  # trees only; see _buildable_selectors
}

# Open-Elevation takes 400 points per request; past this many requests a
# terrain grid gets slow and impolite.
_MAX_ELEVATION_REQUESTS = 25


def _quote(text):
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _buildable_selectors(osm):
    """(Overpass selectors without the bbox, kinds of things they build)
    for the shared tag filters, one selector per key and element type."""
    values_by_key = {}  # key -> set of values, or None for any value
    for key, value in osm_area.tag_filter_pairs(osm.tag_filters):
        if key == "natural":
            if value not in (None, "tree"):
                continue
            value = "tree"
        if key not in _BUILDABLE_KEYS:
            continue
        if value is None or values_by_key.get(key, set()) is None:
            values_by_key[key] = None
        else:
            values_by_key.setdefault(key, set()).add(value)

    selectors = []
    kinds = []
    for key, values in values_by_key.items():
        kind, elements = _BUILDABLE_KEYS[key]
        if values is None:
            test = f"[{_quote(key)}]"
            if key == "highway":
                elements = ("way", "node")
        elif len(values) == 1:
            test = f"[{_quote(key)}={_quote(next(iter(values)))}]"
        else:
            pattern = "^(" + "|".join(re.escape(v) for v in sorted(values)) + ")$"
            test = f"[{_quote(key)}~{_quote(pattern)}]"
        selectors.extend(element + test for element in elements)
        if kind not in kinds:
            kinds.append(kind)
    return selectors, kinds


def _join_words(words):
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _satellite_tile_count(settings, bbox):
    south, west, north, east = bbox
    x1, y1 = latlon_to_tile(north, west, settings.satellite_zoom)
    x2, y2 = latlon_to_tile(south, east, settings.satellite_zoom)
    return (abs(x2 - x1) + 1) * (abs(y2 - y1) + 1)


def _flat_preflight(settings, osm):
    """Check everything an import would fetch before fetching any of it.
    Raises RuntimeError for anything that would fail or that the hard
    limits refuse; returns warnings worth confirming otherwise."""
    bbox = osm_area.parse_bbox(osm)
    messages = _osm_safety_shared.bbox_hints(*bbox)

    if settings.import_osm:
        selectors, kinds = _buildable_selectors(osm)
        if not selectors:
            raise RuntimeError(
                f"Nothing in {osm_area.request_label(osm)} that Local Flat Map "
                "can build (buildings, roads and points of interest). Pick "
                "another preset or turn off OSM Features."
            )
        messages = osm_area.preflight(osm, what=f"{osm_area.request_label(osm)} ({_join_words(kinds)})")
    elif not settings.import_terrain and not settings.import_satellite:
        raise RuntimeError("Nothing to build: enable OSM Features, terrain or satellite.")

    if settings.import_satellite:
        tiles = _satellite_tile_count(settings, bbox)
        if tiles > _MAX_SATELLITE_TILES:
            raise RuntimeError(
                f"Satellite imagery at zoom {settings.satellite_zoom} needs {tiles} "
                f"tiles (limit {_MAX_SATELLITE_TILES}). Lower Zoom Level or shrink the area."
            )

    if settings.import_terrain and settings.fetch_elevation:
        points = (settings.terrain_resolution + 1) ** 2
        requests = -(-points // 400)
        if requests > _MAX_ELEVATION_REQUESTS:
            messages.append(
                f"Terrain resolution {settings.terrain_resolution} needs {requests} "
                "elevation API requests; lower Resolution if that's more than you need."
            )
    return messages


def _build_flat_overpass_query(osm):
    bbox = "(" + ",".join(f"{v:.6f}" for v in osm_area.parse_bbox(osm)) + ")"
    selectors = [f"  {sel}{bbox};" for sel in _buildable_selectors(osm)[0]]
    timeout_s = osm_area.timeout_seconds(osm)
    return "\n".join([f"[out:json][timeout:{timeout_s}];", "(", *selectors, ");", "out body geom;"])


class OBJECT_OT_flat_gis_import_osm(bpy.types.Operator):
    bl_idname = "object.flat_gis_import_osm"
    bl_label = "Build Local Scene"
    bl_description = (
        "Build a metric local scene of the area under Area & Features: "
        "buildings, roads and points from OpenStreetMap, terrain and satellite imagery"
    )
    bl_options = {"REGISTER", "UNDO"}

    preflight_warning: bpy.props.StringProperty(default="", options={"HIDDEN"})

    def invoke(self, context, _event):
        settings = context.scene.flat_gis_settings
        osm = osm_area.get(context)

        try:
            warnings = _flat_preflight(settings, osm)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        if osm.require_confirmation and warnings:
            self.preflight_warning = "\n".join(warnings)
            return context.window_manager.invoke_props_dialog(self, width=560)

        self.preflight_warning = ""
        return self.execute(context)

    def draw(self, _context):
        layout = self.layout
        layout.label(text="Potentially heavy OSM request", icon="ERROR")
        for line in self.preflight_warning.split("\n"):
            line = line.strip()
            if line:
                layout.label(text=line)
        layout.separator()
        layout.label(text="Press OK to continue, or Cancel to adjust limits.")

    def execute(self, context):
        settings = context.scene.flat_gis_settings
        osm = osm_area.get(context)

        # Again here, not just in invoke: scripts and redo skip invoke, and
        # the hard limits shouldn't depend on how it was called.
        try:
            _flat_preflight(settings, osm)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        if not settings.import_osm:
            # Terrain and/or satellite only.
            geojson_data = {"type": "FeatureCollection", "features": []}
        else:
            if not has_osm2geojson():
                self.report({'ERROR'}, "osm2geojson library is not available in Blender's python path.")
                return {'CANCELLED'}

            import osm2geojson

            # 1. Fetch Overpass Data
            self.report({'INFO'}, "Fetching OpenStreetMap data...")
            try:
                overpass_json = osm_area.fetch_overpass_json(
                    osm,
                    _build_flat_overpass_query(osm),
                    user_agent="BlenderFlatGISImporter/1.0",
                    log_prefix="[FlatGIS]",
                )
            except RuntimeError as e:
                self.report({'ERROR'}, f"Failed to fetch Overpass data: {e}")
                return {'CANCELLED'}

            print("[FlatGIS] Overpass fetch complete. Converting to GeoJSON...")

            # 2. Convert to GeoJSON
            try:
                geojson_data = osm2geojson.json2geojson(overpass_json)
            except Exception as e:
                self.report({'ERROR'}, f"Failed to convert OSM to GeoJSON: {e}")
                return {'CANCELLED'}

            features = geojson_data.get('features', [])
            print(f"[FlatGIS] Converted {len(features)} OSM element(s) to GeoJSON.")
            if osm.max_features > 0 and len(features) > osm.max_features:
                self.report({'WARNING'}, f"Kept the first {osm.max_features} of {len(features)} features (Max Features).")
                geojson_data['features'] = features[:osm.max_features]

        # 3. Import
        try:
            import_warnings = []
            import_flat_gis_geojson_data(
                context, geojson_data, settings, bbox_bounds=osm_area.parse_bbox(osm), warnings=import_warnings
            )
            for warning_msg in import_warnings:
                self.report({'WARNING'}, warning_msg)
            self.report({'INFO'}, "Local scene built.")
        except Exception as e:
            self.report({'ERROR'}, f"Error during import: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}

        return {'FINISHED'}

class OBJECT_OT_flat_gis_import_geojson(bpy.types.Operator):
    bl_idname = "object.flat_gis_import_geojson"
    bl_label = "Import GeoJSON File"
    bl_description = "Import local GeoJSON file as flat data"
    bl_options = {"REGISTER", "UNDO"}
    
    def execute(self, context):
        settings = context.scene.flat_gis_settings
        
        if not settings.geojson_filepath:
            self.report({'ERROR'}, "Please specify a valid GeoJSON file path.")
            return {'CANCELLED'}
            
        if not os.path.exists(settings.geojson_filepath):
            self.report({'ERROR'}, f"File not found: {settings.geojson_filepath}")
            return {'CANCELLED'}
            
        # Load File
        self.report({'INFO'}, "Loading GeoJSON file...")
        try:
            with open(settings.geojson_filepath, 'r') as f:
                geojson_data = json.load(f)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to load GeoJSON file: {e}")
            return {'CANCELLED'}
            
        # Import
        try:
            import_warnings = []
            import_flat_gis_geojson_data(context, geojson_data, settings, warnings=import_warnings)
            for warning_msg in import_warnings:
                self.report({'WARNING'}, warning_msg)
            self.report({'INFO'}, "Flat GIS GeoJSON file imported successfully.")
        except Exception as e:
            self.report({'ERROR'}, f"Error during import: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}
            
        return {'FINISHED'}


# --- UI Panel ---

class _FlatGISPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AVL GIS"


class VIEW3D_PT_flat_gis_importer(_FlatGISPanel, bpy.types.Panel):
    bl_label = "Local Flat Map"
    bl_idname = "VIEW3D_PT_flat_gis_importer"
    bl_order = 2
    
    def draw(self, context):
        layout = self.layout
        settings = context.scene.flat_gis_settings
        osm = osm_area.get(context)
        
        # Library Availability Warnings
        if not has_osm2geojson():
            box_err = layout.box()
            box_err.alert = True
            box_err.label(text="osm2geojson library is missing!", icon="ERROR")
            box_err.label(text="Ensure .venv contains osm2geojson.")
            
        if settings.import_satellite and not has_pillow():
            box_err = layout.box()
            box_err.alert = True
            box_err.label(text="Pillow library is missing!", icon="ERROR")
            box_err.label(text="Cannot import satellite texture.")

        layout.label(text="A metric scene of the area under", icon="INFO")
        layout.label(text="Area & Features, centered on it.", icon="BLANK1")

        layout.prop(settings, "import_osm")
        if settings.import_osm:
            kinds = _buildable_selectors(osm)[1]
            col = layout.column(align=True)
            if kinds:
                col.label(text=f"Builds {_join_words(kinds)}", icon="CHECKMARK")
            else:
                col.alert = True
                name = bpy.types.UILayout.enum_item_name(osm, "preset", osm.preset)
                osm_area.draw_wrapped(
                    col,
                    f"Nothing in {name} it can build; it builds buildings, roads and points.",
                    icon="ERROR",
                )
        
        layout.prop(settings, "clear_existing")
        
        col = layout.column(align=True)
        col.scale_y = 1.3
        col.operator("object.flat_gis_import_osm", icon="URL")


class VIEW3D_PT_flat_gis_terrain(_FlatGISPanel, bpy.types.Panel):
    bl_label = "Terrain & Satellite"
    bl_idname = "VIEW3D_PT_flat_gis_terrain"
    bl_parent_id = "VIEW3D_PT_flat_gis_importer"
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.flat_gis_settings

        layout.prop(settings, "import_terrain")
        col = layout.column()
        col.active = settings.import_terrain
        col.prop(settings, "terrain_resolution")
        col.prop(settings, "fetch_elevation", text="Get Elevation")

        layout.prop(settings, "import_satellite")
        col = layout.column()
        col.active = settings.import_satellite
        col.prop(settings, "satellite_zoom")
        if settings.import_satellite:
            try:
                bbox = osm_area.parse_bbox(osm_area.get(context))
            except RuntimeError:
                return
            tiles = _satellite_tile_count(settings, bbox)
            sub = col.row()
            sub.alert = tiles > _MAX_SATELLITE_TILES
            sub.label(text=f"{tiles} tiles (limit {_MAX_SATELLITE_TILES})")


class VIEW3D_PT_flat_gis_geojson(_FlatGISPanel, bpy.types.Panel):
    bl_label = "Local GeoJSON"
    bl_idname = "VIEW3D_PT_flat_gis_geojson"
    bl_parent_id = "VIEW3D_PT_flat_gis_importer"
    bl_options = {"DEFAULT_CLOSED"}
    bl_order = 1

    def draw(self, context):
        layout = self.layout
        settings = context.scene.flat_gis_settings
        layout.prop(settings, "geojson_filepath", text="")
        layout.operator("object.flat_gis_import_geojson", icon="IMPORT")


# --- Register Functions ---

CLASSES = (
    FlatGISSettings,
    OBJECT_OT_flat_gis_import_osm,
    OBJECT_OT_flat_gis_import_geojson,
    VIEW3D_PT_flat_gis_importer,
    VIEW3D_PT_flat_gis_terrain,
    VIEW3D_PT_flat_gis_geojson,
)

def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.flat_gis_settings = bpy.props.PointerProperty(type=FlatGISSettings)
    print("[FlatGIS] Registered panel and operators.")

def unregister():
    if hasattr(bpy.types.Scene, "flat_gis_settings"):
        del bpy.types.Scene.flat_gis_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    print("[FlatGIS] Unregistered panel and operators.")

if __name__ == "__main__":
    register()

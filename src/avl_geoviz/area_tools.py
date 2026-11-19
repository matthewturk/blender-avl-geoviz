"""Ways to set the shared Area & Features box without typing coordinates:
from what the viewport or camera sees of the globe, or from a place-name
search; and an outline of the box drawn on the globe."""

import json
import math
import urllib.parse
import urllib.request

import bpy

from . import _osm_safety_shared, geo_tiles, osm_area

# The outline sits this far above the globe (fraction of its radius): over
# the reference globe's faces and imagery patches, under GeoOverlayRoot's
# default Overlay Offset.
_OUTLINE_LIFT = 5e-4
_OUTLINE_STEP = 0.25  # degrees between outline points
_OUTLINE_COLOR = (1.0, 0.55, 0.05, 1.0)

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Nominatim returns a point's box for places mapped as points; grow tiny
# boxes to this size (km) around their center.
_MIN_PLACE_KM = 2.0


# ---------------------------------------------------------------------------
#  The globe's frame
# ---------------------------------------------------------------------------


def _globe_matrix():
    """World matrix of the globe's frame (the globe root empty, which every
    globe object hangs under), identity if there's none yet."""
    from mathutils import Matrix

    from .country_wireframes import _GLOBE_ROOT_NAME

    root = bpy.data.objects.get(_GLOBE_ROOT_NAME)
    return root.matrix_world.copy() if root is not None else Matrix.Identity(4)


def _lonlat(point):
    x, y, z = point
    r = math.sqrt(x * x + y * y + z * z)
    return math.degrees(math.atan2(y, x)), math.degrees(math.asin(max(-1.0, min(1.0, z / r))))


def _hit_sphere(origin, direction, radius):
    """Nearest point where the ray hits the sphere at the origin, or None."""
    d = direction.normalized()
    b = origin.dot(d)
    c = origin.dot(origin) - radius * radius
    disc = b * b - c
    if disc < 0.0:
        return None
    t = -b - math.sqrt(disc)
    if t < 0.0:
        t = -b + math.sqrt(disc)  # inside the sphere
    return origin + d * t if t >= 0.0 else None


def bbox_of_lonlats(points):
    """(south, west, north, east) around (lon, lat) points, taking the
    shortest way round in longitude. Raises RuntimeError if that crosses
    the antimeridian."""
    lats = [lat for _lon, lat in points]
    lons = sorted(lon for lon, _lat in points)
    # The widest gap between neighbouring longitudes (going round) is
    # what the box leaves out.
    gaps = [(lons[i + 1] - lons[i], i) for i in range(len(lons) - 1)]
    gaps.append((lons[0] + 360.0 - lons[-1], len(lons) - 1))
    gap, i = max(gaps)
    if gap < 30.0:
        west, east = -180.0, 180.0  # all the way round: a pole is in view
    elif i == len(lons) - 1:
        west, east = lons[0], lons[-1]
    else:
        raise RuntimeError(
            "That view crosses the antimeridian (180 degrees), which an area can't; "
            "turn the globe or zoom in on one side.")
    south, north = min(lats), max(lats)
    if west == -180.0:
        # Longitudes all the way round: the view takes in the nearer pole.
        if north + south > 0:
            north = 90.0
        else:
            south = -90.0
    return (south, west, north, east)


def _set_bbox(context, bbox):
    south, west, north, east = bbox
    _osm_safety_shared.validate_bbox(south, west, north, east)
    osm_area.get(context).bbox = f"{south:.5f}, {west:.5f}, {north:.5f}, {east:.5f}"
    _redraw(context)


def _redraw(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


# ---------------------------------------------------------------------------
#  Area from View / Camera
# ---------------------------------------------------------------------------

_SAMPLES = 17  # rays per side of the view


def _view_rays(context):
    """(origin, direction) rays across the 3D viewport, in world space."""
    from bpy_extras import view3d_utils

    area = context.area if context.area and context.area.type == "VIEW_3D" else None
    if area is None:
        area = next((a for a in context.screen.areas if a.type == "VIEW_3D"), None)
    if area is None:
        raise RuntimeError("No 3D Viewport to take the view from.")
    region = next(r for r in area.regions if r.type == "WINDOW")
    rv3d = area.spaces.active.region_3d
    rays = []
    for j in range(_SAMPLES):
        for i in range(_SAMPLES):
            xy = (region.width * i / (_SAMPLES - 1), region.height * j / (_SAMPLES - 1))
            rays.append((view3d_utils.region_2d_to_origin_3d(region, rv3d, xy),
                         view3d_utils.region_2d_to_vector_3d(region, rv3d, xy)))
    return rays


def _camera_rays(context):
    """(origin, direction) rays across the scene camera's frame."""
    from mathutils import Vector

    scene = context.scene
    cam = scene.camera
    if cam is None or cam.type != "CAMERA":
        raise RuntimeError("The scene has no camera.")
    mw = cam.matrix_world
    # view_frame: top-right, bottom-right, bottom-left, top-left.
    tr, br, bl, tl = (mw @ v for v in cam.data.view_frame(scene=scene))
    forward = (mw.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized()
    rays = []
    for j in range(_SAMPLES):
        v = j / (_SAMPLES - 1)
        left, right = bl.lerp(tl, v), br.lerp(tr, v)
        for i in range(_SAMPLES):
            point = left.lerp(right, i / (_SAMPLES - 1))
            if cam.data.type == "ORTHO":
                rays.append((point, forward))
            else:
                rays.append((mw.translation.copy(), point - mw.translation))
    return rays


def bbox_from_rays(context, rays):
    """The box around where *rays* hit the globe, and how many missed."""
    matrix = _globe_matrix()
    inverse = matrix.inverted()
    radius = context.scene.geo_coord_settings.globe_radius
    points, missed = [], 0
    for origin, direction in rays:
        o = inverse @ origin
        d = inverse.to_3x3() @ direction
        hit = _hit_sphere(o, d, radius)
        if hit is None:
            missed += 1
        else:
            points.append(_lonlat(hit))
    if not points:
        raise RuntimeError("The globe isn't in view.")
    return bbox_of_lonlats(points), missed


class OBJECT_OT_area_from_view(bpy.types.Operator):
    bl_idname = "object.area_from_view"
    bl_label = "Area from View"
    bl_description = "Set the area to the part of the globe the 3D Viewport (or scene camera) shows"
    bl_options = {"REGISTER", "UNDO"}

    source: bpy.props.EnumProperty(
        name="From",
        items=[
            ("VIEW", "Viewport", "What the 3D Viewport shows"),
            ("CAMERA", "Camera", "What the scene camera sees"),
        ],
        default="VIEW",
    )

    @classmethod
    def poll(cls, context):
        if context.scene.geo_coord_settings.coordinate_space != "GLOBE":
            cls.poll_message_set("Works on the globe (Coordinate Space: Globe)")
            return False
        return True

    def execute(self, context):
        try:
            rays = _view_rays(context) if self.source == "VIEW" else _camera_rays(context)
            bbox, missed = bbox_from_rays(context, rays)
            _set_bbox(context, bbox)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        what = "view" if self.source == "VIEW" else "camera"
        message = f"Area set to what the {what} shows: {_osm_safety_shared.format_dims(*bbox)}"
        if missed:
            message += " (the globe's edge is in view; it covers the visible part)"
        self.report({"INFO"}, message)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
#  Place search
# ---------------------------------------------------------------------------


class GeoPlaceResult(bpy.types.PropertyGroup):
    label: bpy.props.StringProperty()
    kind: bpy.props.StringProperty()
    south: bpy.props.FloatProperty()
    west: bpy.props.FloatProperty()
    north: bpy.props.FloatProperty()
    east: bpy.props.FloatProperty()


class GeoPlaceSettings(bpy.types.PropertyGroup):
    query: bpy.props.StringProperty(
        name="Place",
        description="A place name to find, e.g. Champaign, Illinois or Yosemite Valley",
        default="",
    )
    results: bpy.props.CollectionProperty(type=GeoPlaceResult)
    source: bpy.props.StringProperty(default="")


def _grow(south, west, north, east, min_km=_MIN_PLACE_KM):
    width, height = _osm_safety_shared.bbox_dims_km(south, west, north, east)
    if width >= min_km and height >= min_km:
        return south, west, north, east
    lat, lon = (south + north) / 2.0, (west + east) / 2.0
    return _osm_safety_shared.bbox_around(lat, lon, max(width, min_km), max(height, min_km))


def search_nominatim(query, limit=8):
    """[(label, kind, (south, west, north, east))] from OpenStreetMap's
    Nominatim. Light, user-triggered use only (its usage policy)."""
    url = _NOMINATIM + "?" + urllib.parse.urlencode(
        {"q": query, "format": "jsonv2", "limit": limit})
    request = urllib.request.Request(url, headers={"User-Agent": geo_tiles.USER_AGENT})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))
    results = []
    for item in data:
        south, north, west, east = (float(v) for v in item["boundingbox"])
        kind = (item.get("type") or item.get("category") or "").replace("_", " ")
        results.append((item.get("display_name", query), kind, _grow(south, west, north, east)))
    return results


def search_offline(query, limit=8):
    """Cities from the bundled GeoNames list (population 15,000+), with a
    box sized roughly by population. For when online access is off."""
    from geonamescache import GeonamesCache

    q = query.strip().lower().split(",")[0].strip()
    if not q:
        return []
    gc = GeonamesCache()
    matches = [c for c in gc.get_cities().values()
               if c["name"].lower().startswith(q) or q in [a.lower() for a in c.get("alternatenames", [])]]
    matches.sort(key=lambda c: -c.get("population", 0))
    results = []
    for city in matches[:limit]:
        size = max(5.0, min(60.0, math.sqrt(city.get("population", 0)) / 20.0))
        country = city.get("countrycode", "")
        bbox = _osm_safety_shared.bbox_around(city["latitude"], city["longitude"], size, size)
        region = city.get("admin1code", "")
        region = f", {region}" if region and not region.isdigit() else ""
        pop = city.get("population", 0)
        results.append((f"{city['name']}{region}, {country} (pop. {pop:,})",
                        f"city, ~{size:.0f} km box", bbox))
    return results


class OBJECT_OT_find_place(bpy.types.Operator):
    bl_idname = "object.find_place"
    bl_label = "Find Place"
    bl_description = (
        "Search for a place by name: OpenStreetMap's Nominatim when online access is on, "
        "otherwise the bundled list of cities"
    )

    def execute(self, context):
        settings = context.scene.geo_place_settings
        query = settings.query.strip()
        settings.results.clear()
        if not query:
            self.report({"ERROR"}, "Type a place name first")
            return {"CANCELLED"}
        try:
            if geo_tiles.online_access():
                results, settings.source = search_nominatim(query), "OpenStreetMap Nominatim"
            else:
                results, settings.source = search_offline(query), "bundled city list (offline)"
        except Exception as exc:
            self.report({"ERROR"}, f"Place search failed: {exc}")
            return {"CANCELLED"}
        for label, kind, bbox in results:
            item = settings.results.add()
            item.label, item.kind = label, kind
            item.south, item.west, item.north, item.east = bbox
        if not results:
            self.report({"WARNING"}, f"Nothing found for {query!r}")
        elif len(results) == 1:
            bpy.ops.object.use_place(index=0)
        return {"FINISHED"}


class OBJECT_OT_use_place(bpy.types.Operator):
    bl_idname = "object.use_place"
    bl_label = "Use Place"
    bl_description = "Set the area to this place's box"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    index: bpy.props.IntProperty()

    def execute(self, context):
        settings = context.scene.geo_place_settings
        if not 0 <= self.index < len(settings.results):
            return {"CANCELLED"}
        r = settings.results[self.index]
        try:
            _set_bbox(context, (r.south, r.west, r.north, r.east))
        except RuntimeError as exc:
            self.report({"ERROR"}, f"{r.label}: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Area set to {r.label}")
        return {"FINISHED"}


class OBJECT_OT_clear_places(bpy.types.Operator):
    bl_idname = "object.clear_places"
    bl_label = "Clear Results"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        context.scene.geo_place_settings.results.clear()
        return {"FINISHED"}


# ---------------------------------------------------------------------------
#  Outline on the globe
# ---------------------------------------------------------------------------


def outline_points(bbox, radius, matrix):
    """World-space points around *bbox*'s edges on the globe, closed."""
    from mathutils import Vector

    from .geo_coord import latlon_to_xyz

    south, west, north, east = bbox

    def edge(lat0, lon0, lat1, lon1):
        n = max(2, min(720, math.ceil(max(abs(lat1 - lat0), abs(lon1 - lon0)) / _OUTLINE_STEP)))
        return [(lat0 + (lat1 - lat0) * k / n, lon0 + (lon1 - lon0) * k / n) for k in range(n)]

    ring = (edge(south, west, south, east) + edge(south, east, north, east)
            + edge(north, east, north, west) + edge(north, west, south, west))
    ring.append(ring[0])
    r = radius * (1.0 + _OUTLINE_LIFT)
    return [tuple(matrix @ Vector(latlon_to_xyz(lat, lon, r))) for lat, lon in ring]


def _draw_outline():
    context = bpy.context
    scene = getattr(context, "scene", None)
    if scene is None or not getattr(scene, "geo_show_area", False):
        return
    cs = scene.geo_coord_settings
    if cs.coordinate_space != "GLOBE":
        return
    try:
        bbox = osm_area.parse_bbox(osm_area.get(context))
    except RuntimeError:
        return
    import gpu
    from gpu_extras.batch import batch_for_shader

    points = outline_points(bbox, cs.globe_radius, _globe_matrix())
    shader = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
    batch = batch_for_shader(shader, "LINE_STRIP", {"pos": points})
    region = context.region
    gpu.state.blend_set("ALPHA")
    shader.uniform_float("viewportSize", (region.width, region.height))
    # Faint everywhere (so it never vanishes into the globe, or behind it),
    # solid where it's in front.
    for depth, width, alpha in (("NONE", 1.5, 0.35), ("LESS_EQUAL", 2.5, 1.0)):
        gpu.state.depth_test_set(depth)
        shader.uniform_float("lineWidth", width)
        shader.uniform_float("color", (*_OUTLINE_COLOR[:3], alpha))
        batch.draw(shader)
    gpu.state.depth_test_set("NONE")
    gpu.state.blend_set("NONE")


def _on_show_area(_self, context):
    _redraw(context)


# ---------------------------------------------------------------------------
#  UI
# ---------------------------------------------------------------------------


def draw_area_tools(layout, context, search=True):
    """Area from View / Camera, Show on Globe and (with *search*) place
    search, for the Area & Features and Area Imagery panels."""
    row = layout.row(align=True)
    row.operator("object.area_from_view", text="From View", icon="HIDE_OFF").source = "VIEW"
    row.operator("object.area_from_view", text="From Camera", icon="CAMERA_DATA").source = "CAMERA"
    row.prop(context.scene, "geo_show_area", text="", icon="SELECT_SET")
    if not search:
        return
    settings = context.scene.geo_place_settings
    row = layout.row(align=True)
    row.prop(settings, "query", text="", icon="VIEWZOOM", placeholder="Find a place")
    row.operator("object.find_place", text="", icon="RIGHTARROW")
    if len(settings.results):
        col = layout.column(align=True)
        for index, item in enumerate(settings.results):
            op = col.operator("object.use_place", text=item.label[:60], icon="WORLD")
            op.index = index
        row = col.row()
        row.label(text=f"From {settings.source}")
        row.operator("object.clear_places", text="", icon="X", emboss=False)


CLASSES = (
    GeoPlaceResult,
    GeoPlaceSettings,
    OBJECT_OT_area_from_view,
    OBJECT_OT_find_place,
    OBJECT_OT_use_place,
    OBJECT_OT_clear_places,
)

_handle = None


def register():
    global _handle
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.geo_place_settings = bpy.props.PointerProperty(type=GeoPlaceSettings)
    bpy.types.Scene.geo_show_area = bpy.props.BoolProperty(
        name="Show Area on Globe",
        description="Draw the Area & Features box on the globe in the 3D Viewport",
        default=True,
        update=_on_show_area,
    )
    if not bpy.app.background:
        _handle = bpy.types.SpaceView3D.draw_handler_add(_draw_outline, (), "WINDOW", "POST_VIEW")


def unregister():
    global _handle
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, "WINDOW")
        _handle = None
    del bpy.types.Scene.geo_show_area
    del bpy.types.Scene.geo_place_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)

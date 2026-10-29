"""The area and the OpenStreetMap features to fetch, shared by the globe /
world-map OSM import (country_wireframes.py) and Local Flat Map
(flat_gis_importer.py): one bbox, one feature preset and one set of request
limits, so an area set up for one is already set up for the other. Each
importer builds its own kind of output from what's fetched.

The bbox and limit checks themselves are in _osm_safety_shared, which has
no bpy dependency."""

import textwrap

import bpy
from bpy.app.handlers import persistent

from . import _osm_safety_shared

_STREET_VALUES = (
    "motorway|trunk|primary|secondary|tertiary|unclassified|residential"
    "|living_street|service|motorway_link|trunk_link|primary_link"
    "|secondary_link|tertiary_link"
)

# OSM presets: (identifier, label, description, tag filters, recommended max
# bbox area in km^2, enum number). The area is roughly how much of a dense
# city those features cover while staying a quick Overpass query and a
# manageable import; sparse features (motorways, rail) get far more room
# than dense ones (buildings). Imports warn above it and refuse above Area
# Limit times it. The enum number is what a .blend stores, so existing
# entries keep theirs; 0-6 are the presets' numbers from before this was
# shared (see _migrate_scene).
_PRESETS = (
    (
        "EVERYTHING",
        "Everything",
        "Every kind of mapped feature: buildings, streets and paths, rail, "
        "water, land cover, POIs, utilities, barriers and boundaries",
        "building,highway,railway,waterway,natural,landuse,leisure,amenity,"
        "shop,tourism,man_made,power,barrier,aeroway,place,boundary",
        1.0,
        7,
    ),
    (
        "ALL",
        "Common Layers",
        "Buildings, roads and paths, water, land use, POIs and administrative "
        "boundaries",
        "building,highway,waterway,natural=water,landuse,natural,amenity,"
        "shop,tourism,boundary=administrative",
        2.0,
        6,
    ),
    (
        "BUILDINGS_STREETS",
        "Buildings + Streets",
        "Building footprints and the drivable street network",
        f"building,highway={_STREET_VALUES}",
        4.0,
        8,
    ),
    (
        "BUILDINGS",
        "Buildings",
        "Building footprints and building parts",
        "building,building:part",
        4.0,
        0,
    ),
    (
        "STREETS",
        "Streets",
        "Drivable streets only, motorways down to service roads; no "
        "footpaths or cycleways",
        f"highway={_STREET_VALUES}",
        10.0,
        9,
    ),
    (
        "ROADS",
        "Streets + Paths",
        "Everything tagged highway: streets, footways, cycleways, tracks, steps",
        "highway",
        5.0,
        1,
    ),
    (
        "MAJOR_ROADS",
        "Major Roads",
        "Motorways, trunk, primary and secondary roads; sized for whole "
        "metro areas",
        "highway=motorway|trunk|primary|secondary|motorway_link|trunk_link",
        500.0,
        10,
    ),
    (
        "PATHS",
        "Paths + Cycleways",
        "Footways, paths, cycleways, pedestrian streets, steps and tracks",
        "highway=footway|path|cycleway|pedestrian|steps|track|bridleway",
        5.0,
        11,
    ),
    (
        "RAIL",
        "Railways",
        "Rail, subway, light rail and tram lines",
        "railway=rail|subway|light_rail|tram|narrow_gauge|monorail",
        250.0,
        12,
    ),
    (
        "WATER",
        "Water",
        "Rivers, streams, canals, lakes and ponds",
        "waterway=river|stream|canal|drain|ditch,natural=water",
        100.0,
        2,
    ),
    (
        "GREEN",
        "Parks + Green Space",
        "Parks, gardens, nature reserves, woods and grassland",
        "leisure=park|garden|nature_reserve,"
        "landuse=grass|forest|meadow|recreation_ground,"
        "natural=wood|scrub|grassland",
        25.0,
        13,
    ),
    (
        "LANDUSE",
        "Land Use",
        "Land use and natural land cover",
        "landuse,natural",
        10.0,
        3,
    ),
    (
        "POI",
        "Points of Interest",
        "Amenities, shops and tourism points",
        "amenity,shop,tourism",
        5.0,
        4,
    ),
    (
        "BOUNDARIES",
        "Boundaries",
        "Administrative and political boundaries (whole outlines, which can "
        "reach far outside the bbox)",
        "boundary=administrative",
        1000.0,
        5,
    ),
    (
        "CUSTOM",
        "Custom",
        "Your own tag filters; editing Tag Filters switches to this",
        None,
        5.0,
        14,
    ),
)
_PRESETS_BY_ID = {preset[0]: preset for preset in _PRESETS}
_PRESETS_BY_NUMBER = {preset[5]: preset for preset in _PRESETS}

# About 1.2 x 1.0 km around the UIUC Main Quad: small enough for any preset.
DEFAULT_BBOX = "40.1029,-88.2342,40.1119,-88.2202"
DEFAULT_PRESET = "BUILDINGS_STREETS"


def presets_map():
    return {preset[0]: preset[3] for preset in _PRESETS if preset[3] is not None}


# --- Reading the settings -----------------------------------------------------


def get(context):
    return context.scene.avl_osm_settings


def parse_bbox(settings):
    """Validated (south, west, north, east); RuntimeError if unusable."""
    bbox = _osm_safety_shared.parse_bbox_text(settings.bbox)
    _osm_safety_shared.validate_bbox(*bbox)
    return bbox


def _preset(settings):
    return _PRESETS_BY_ID.get(settings.preset, _PRESETS_BY_ID["CUSTOM"])


def recommended_area_km2(settings):
    return _preset(settings)[4]


def request_label(settings):
    preset = _preset(settings)
    return f"the {preset[1]} preset" if preset[0] != "CUSTOM" else "custom tag filters"


def tag_filter_pairs(raw_text):
    """[(key, value-or-None)] from "key, key=value, key=a|b" text."""
    normalized = str(raw_text or "")
    for sep in (";", "\n", "\t"):
        normalized = normalized.replace(sep, ",")
    parsed = []
    for token in (t.strip() for t in normalized.split(",")):
        if not token:
            continue
        if "=" not in token:
            parsed.append((token, None))
            continue
        key, value = token.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values = [v.strip() for v in value.split("|") if v.strip()]
        if not values:
            parsed.append((key, None))
        parsed.extend((key, v) for v in values)
    return parsed


def tags_dict(raw_text):
    """Tag filters in osmnx's form: {key: True | value | [values]}."""
    tags = {}
    for key, value in tag_filter_pairs(raw_text):
        if value is None:
            tags[key] = True
            continue

        existing = tags.get(key)
        if existing is None or existing is True:
            tags[key] = value
            continue

        if isinstance(existing, list):
            if value not in existing:
                existing.append(value)
            continue

        if existing != value:
            tags[key] = [existing, value]

    return tags


def preflight(settings, custom_query=None, recommended_km2=None, what=None):
    """Warnings worth confirming before a fetch; RuntimeError for anything
    the checks refuse outright. With custom_query, the bboxes written in
    that Overpass QL are checked instead of the shared one. recommended_km2
    and what override the preset's, for an importer that fetches less than
    the preset names."""
    if custom_query is not None:
        bboxes = _osm_safety_shared.bboxes_in_overpass_query(custom_query)
        for bbox in bboxes:
            _osm_safety_shared.validate_bbox(*bbox)
        area_km2 = max((_osm_safety_shared.bbox_area_km2(*b) for b in bboxes), default=None)
        hints = [h for b in bboxes for h in _osm_safety_shared.bbox_hints(*b)]
        recommended_km2 = _PRESETS_BY_ID["CUSTOM"][4]
        what = "a custom query"
    else:
        bbox = parse_bbox(settings)
        area_km2 = _osm_safety_shared.bbox_area_km2(*bbox)
        hints = _osm_safety_shared.bbox_hints(*bbox)
        if recommended_km2 is None:
            recommended_km2 = recommended_area_km2(settings)
        if what is None:
            what = request_label(settings)

    return hints + _osm_safety_shared.osm_preflight_messages(
        area_km2,
        recommended_km2,
        settings.area_limit_factor,
        settings.max_features,
        custom_query is not None,
        settings.timeout_seconds,
        what=what,
    )


def endpoint(settings):
    return str(settings.endpoint).strip() or _osm_safety_shared.OVERPASS_ENDPOINT


def timeout_seconds(settings):
    return max(5, int(settings.timeout_seconds))


def fetch_overpass_json(settings, query, user_agent, log_prefix):
    return _osm_safety_shared.fetch_overpass_json(
        endpoint(settings),
        query,
        timeout_seconds(settings),
        max(1, int(settings.max_response_mb)) * 1024 * 1024,
        user_agent=user_agent,
        log_prefix=log_prefix,
    )


# --- Settings -----------------------------------------------------------------


def _on_preset_changed(settings, _context):
    tags = presets_map().get(settings.preset)
    if tags is not None and settings.tag_filters != tags:
        settings.tag_filters = tags


def _on_tag_filters_changed(settings, _context):
    # Hand-edited filters no longer match the preset; say so, so the area
    # limits fall back to the conservative Custom ones.
    if presets_map().get(settings.preset) != settings.tag_filters:
        settings.preset = "CUSTOM"


class AVLOSMSettings(bpy.types.PropertyGroup):
    bbox: bpy.props.StringProperty(
        name="BBox",
        description=(
            "south,west,north,east in WGS84 degrees (lat,lon order); paste "
            "with any separators. Defaults to about 1 km² around the UIUC "
            "Main Quad"
        ),
        default=DEFAULT_BBOX,
    )
    preset: bpy.props.EnumProperty(
        name="Preset",
        description="What to fetch; sets Tag Filters and the recommended area",
        items=[(p[0], p[1], p[2], "NONE", p[5]) for p in _PRESETS],
        default=DEFAULT_PRESET,
        update=_on_preset_changed,
    )
    tag_filters: bpy.props.StringProperty(
        name="Tag Filters",
        description=(
            "Comma-separated key or key=value filters; separate alternative "
            "values with |, e.g. building, highway=primary|secondary"
        ),
        default=presets_map()[DEFAULT_PRESET],
        update=_on_tag_filters_changed,
    )
    area_limit_factor: bpy.props.FloatProperty(
        name="Area Limit (x Recommended)",
        description=(
            "Refuse imports whose bbox is more than this many times the "
            "recommended area for what's requested. Between 1x and this, "
            "imports ask first"
        ),
        default=10.0,
        min=1.0,
        max=1000.0,
        precision=1,
    )
    require_confirmation: bpy.props.BoolProperty(
        name="Confirm Large OSM Requests",
        description="Prompt before potentially large OSM imports",
        default=True,
    )
    max_features: bpy.props.IntProperty(
        name="Max Features",
        description="Cap on imported OSM features (0 disables the cap)",
        default=2000,
        min=0,
        max=200000,
    )
    max_response_mb: bpy.props.IntProperty(
        name="Max Response (MB)",
        description="Abort if the Overpass response exceeds this size",
        default=64,
        min=5,
        max=1024,
    )
    timeout_seconds: bpy.props.IntProperty(
        name="Timeout (s)",
        description="Network timeout for the Overpass request",
        default=60,
        min=5,
        max=600,
    )
    endpoint: bpy.props.StringProperty(
        name="Overpass Endpoint",
        description=(
            "Overpass API interpreter URL. If the main server is busy, try "
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter or "
            "another public instance"
        ),
        default=_osm_safety_shared.OVERPASS_ENDPOINT,
    )
    migrated: bpy.props.BoolProperty(default=False, options={"HIDDEN"})


# --- Carrying settings over from before they were shared ----------------------

# Where each setting lived before: (old group, old name) in the order tried;
# the first one the .blend actually stored wins.
_OLD_LOCATIONS = {
    "tag_filters": (("country_wireframe_settings", "osm_tag_filters"),),
    "area_limit_factor": (
        ("country_wireframe_settings", "osm_area_limit_factor"),
        ("flat_gis_settings", "osm_area_limit_factor"),
    ),
    "require_confirmation": (
        ("country_wireframe_settings", "osm_require_confirmation"),
        ("flat_gis_settings", "osm_require_confirmation"),
    ),
    "max_features": (
        ("country_wireframe_settings", "osm_max_features"),
        ("flat_gis_settings", "osm_max_features"),
    ),
    "max_response_mb": (
        ("country_wireframe_settings", "osm_max_response_mb"),
        ("flat_gis_settings", "osm_max_response_mb"),
    ),
    "endpoint": (
        ("country_wireframe_settings", "osm_endpoint"),
        ("flat_gis_settings", "osm_endpoint"),
    ),
}


def _migrate_scene(scene):
    """Copy the area, preset and request limits a .blend saved in the
    importers' own settings (before they were shared) into the shared ones.
    Blender keeps the stored values of properties that are no longer
    registered, which is what's read here. Runs once per scene."""
    settings = scene.avl_osm_settings
    if settings.migrated:
        return
    settings.migrated = True

    stored = scene.bl_system_properties_get()
    if stored is None:
        return
    groups = {
        name: stored.get(name)
        for name in ("country_wireframe_settings", "flat_gis_settings")
    }

    def old_value(group, name):
        values = groups.get(group)
        return values.get(name) if values is not None else None

    globe_bbox = old_value("country_wireframe_settings", "osm_bbox")
    flat_bbox = [
        old_value("flat_gis_settings", name)
        for name in ("bbox_min_lat", "bbox_min_lon", "bbox_max_lat", "bbox_max_lon")
    ]
    if globe_bbox:
        settings.bbox = globe_bbox
    elif any(v is not None for v in flat_bbox):
        defaults = _osm_safety_shared.parse_bbox_text(DEFAULT_BBOX)
        bbox = [d if v is None else float(v) for v, d in zip(flat_bbox, defaults)]
        # Those were single-precision; 5 decimals (~1 m) drops the noise.
        settings.bbox = ",".join(f"{v:.5f}" for v in bbox)

    # The preset first, since setting it rewrites the tag filters.
    preset_number = old_value("country_wireframe_settings", "osm_preset")
    if preset_number is not None and int(preset_number) in _PRESETS_BY_NUMBER:
        settings.preset = _PRESETS_BY_NUMBER[int(preset_number)][0]
    for new_name, locations in _OLD_LOCATIONS.items():
        for group, old_name in locations:
            value = old_value(group, old_name)
            if value is not None:
                setattr(settings, new_name, value)
                break


@persistent
def _migrate_on_load(_dummy):
    for scene in bpy.data.scenes:
        _migrate_scene(scene)


# --- Operators ----------------------------------------------------------------


class OBJECT_OT_apply_osm_preset(bpy.types.Operator):
    bl_idname = "object.apply_osm_preset"
    bl_label = "Apply OSM Preset"
    bl_description = "Reset Tag Filters to the selected preset's"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = get(context)
        tag_filter = presets_map().get(settings.preset)
        if tag_filter is None:
            self.report({"INFO"}, "Custom preset: Tag Filters left as they are.")
            return {"CANCELLED"}
        settings.tag_filters = tag_filter
        self.report({"INFO"}, f"OSM preset applied: {tag_filter}")
        return {"FINISHED"}


class OBJECT_OT_osm_bbox_from_center(bpy.types.Operator):
    bl_idname = "object.osm_bbox_from_center"
    bl_label = "Set BBox From Center"
    bl_description = "Set the area to a box of a given size in km around a point"
    bl_options = {"REGISTER", "UNDO"}

    center_lat: bpy.props.FloatProperty(
        name="Latitude", min=-85.0, max=85.0, precision=5, default=40.1074
    )
    center_lon: bpy.props.FloatProperty(
        name="Longitude", min=-180.0, max=180.0, precision=5, default=-88.2272
    )
    width_km: bpy.props.FloatProperty(
        name="Width (km)", min=0.01, max=500.0, default=1.0, precision=2
    )
    height_km: bpy.props.FloatProperty(
        name="Height (km)", min=0.01, max=500.0, default=1.0, precision=2
    )

    def invoke(self, context, _event):
        try:
            bbox = parse_bbox(get(context))
        except RuntimeError:
            bbox = None
        if bbox is not None:
            south, west, north, east = bbox
            self.center_lat = (south + north) / 2.0
            self.center_lon = (west + east) / 2.0
            self.width_km, self.height_km = (
                max(0.01, round(v, 2)) for v in _osm_safety_shared.bbox_dims_km(*bbox)
            )
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        bbox = _osm_safety_shared.bbox_around(
            self.center_lat, self.center_lon, self.width_km, self.height_km
        )
        try:
            _osm_safety_shared.validate_bbox(*bbox)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        get(context).bbox = ",".join(f"{v:.5f}" for v in bbox)
        return {"FINISHED"}


# --- Panels -------------------------------------------------------------------


def draw_wrapped(layout, text, icon="NONE", width=44):
    for i, line in enumerate(textwrap.wrap(text, width)):
        layout.label(text=line, icon=icon if i == 0 else "BLANK1")


def draw_area_status(layout, settings, recommended_km2=None):
    """Live size readout for the area, flagging problems before a query is
    fired."""
    col = layout.column(align=True)
    try:
        bbox = parse_bbox(settings)
    except RuntimeError as exc:
        col.alert = True
        draw_wrapped(col, str(exc), icon="ERROR")
        return
    area = _osm_safety_shared.bbox_area_km2(*bbox)
    if recommended_km2 is None:
        recommended_km2 = recommended_area_km2(settings)
    limit = recommended_km2 * settings.area_limit_factor
    icon = "CHECKMARK"
    if area > limit:
        col.alert = True
        icon = "CANCEL"
    elif area > recommended_km2:
        icon = "ERROR"
    col.label(text=_osm_safety_shared.format_dims(*bbox), icon=icon)
    col.label(text=f"Recommended ≤ {recommended_km2:g} km², limit {limit:g} km²")
    for hint in _osm_safety_shared.bbox_hints(*bbox):
        draw_wrapped(col, hint, icon="INFO")


class _AreaPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AVL GIS"


class VIEW3D_PT_avl_gis_area(_AreaPanel, bpy.types.Panel):
    bl_label = "Area & Features"
    bl_idname = "VIEW3D_PT_avl_gis_area"
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        settings = get(context)

        box = layout.box()
        row = box.row()
        row.label(text="Area", icon="WORLD")
        row.operator("object.osm_bbox_from_center", text="From Center", icon="PIVOT_CURSOR")
        box.prop(settings, "bbox", text="")
        box.label(text="south, west, north, east", icon="BLANK1")
        draw_area_status(box, settings)

        box = layout.box()
        box.label(text="OpenStreetMap Features", icon="PRESET")
        row = box.row(align=True)
        row.prop(settings, "preset", text="")
        if settings.preset != "CUSTOM":
            row.operator("object.apply_osm_preset", text="", icon="LOOP_BACK")
        box.prop(settings, "tag_filters", text="Tags")


class VIEW3D_PT_avl_gis_area_limits(_AreaPanel, bpy.types.Panel):
    bl_label = "Request Limits"
    bl_idname = "VIEW3D_PT_avl_gis_area_limits"
    bl_parent_id = "VIEW3D_PT_avl_gis_area"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = get(context)

        layout.prop(settings, "area_limit_factor", text="Area Limit (x)")
        layout.prop(settings, "require_confirmation", text="Confirm Large")
        layout.separator()
        layout.prop(settings, "max_features")
        layout.prop(settings, "max_response_mb")
        layout.prop(settings, "timeout_seconds")
        layout.prop(settings, "endpoint", text="Endpoint")


CLASSES = (
    AVLOSMSettings,
    OBJECT_OT_apply_osm_preset,
    OBJECT_OT_osm_bbox_from_center,
    VIEW3D_PT_avl_gis_area,
    VIEW3D_PT_avl_gis_area_limits,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.avl_osm_settings = bpy.props.PointerProperty(type=AVLOSMSettings)
    bpy.app.handlers.load_post.append(_migrate_on_load)
    # Also the file that's already open when the extension is enabled;
    # bpy.data isn't writable during register itself.
    bpy.app.timers.register(lambda: _migrate_on_load(None), first_interval=0.0)


def unregister():
    if _migrate_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_migrate_on_load)
    if hasattr(bpy.types.Scene, "avl_osm_settings"):
        del bpy.types.Scene.avl_osm_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)

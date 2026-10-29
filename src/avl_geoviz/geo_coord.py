"""Shared coordinate-space settings and projection helpers for all geo importers."""

import math

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty

# ---------------------------------------------------------------------------
#  WGS84 ellipsoid constants
# ---------------------------------------------------------------------------

_WGS84_A = 6378137.0
_WGS84_E_SQ = 0.00669437999014


# ---------------------------------------------------------------------------
#  Low-level projection helpers
# ---------------------------------------------------------------------------

def latlon_to_xyz(lat_deg, lon_deg, radius):
    """Project lat/lon onto a sphere of the given radius."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    cos_lat = math.cos(lat)
    return (
        radius * cos_lat * math.cos(lon),
        radius * cos_lat * math.sin(lon),
        radius * math.sin(lat),
    )


def latlon_to_unit(lat_deg, lon_deg):
    """Unit vector on the unit sphere."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    cos_lat = math.cos(lat)
    return (cos_lat * math.cos(lon), cos_lat * math.sin(lon), math.sin(lat))


def latlon_to_xy(lat_deg, lon_deg, scale):
    """Equirectangular flat projection with a fixed scale multiplier."""
    return (lon_deg * scale, lat_deg * scale, 0.0)


def latlon_to_xy_with_bounds(lat_deg, lon_deg, scale, bounds):
    """Equirectangular flat projection with aspect-ratio correction.

    Longitude (X) always spans exactly ``[0, scale]`` across the bounds,
    independent of aspect mode - this matches the STRETCH branch and
    ``latlon_to_xy_normalized`` below. The cos(lat) aspect correction
    therefore has to be applied to Y (dividing it, since 1 degree of
    latitude covers more ground than 1 degree of longitude away from the
    equator) rather than to X, or that invariant would break.
    """
    min_lon, min_lat, max_lon, max_lat = bounds
    lon_span = max_lon - min_lon
    lat_span = max_lat - min_lat
    if lon_span <= 0 or lat_span <= 0:
        return (0.0, 0.0, 0.0)
    center_lat = math.radians((min_lat + max_lat) / 2.0)
    cos_lat = max(math.cos(center_lat), 1e-6)
    x = ((lon_deg - min_lon) / lon_span) * scale
    y = (((lat_deg - min_lat) / lon_span) * scale) / cos_lat
    return (x, y, 0.0)


def latlon_to_xy_normalized(lat_deg, lon_deg, bounds):
    """Normalized flat projection fitting data into [0, 1] preserving aspect.

    Same cos(lat) correction as ``latlon_to_xy_with_bounds`` (divide Y),
    using the bounds' center latitude as the single reference so this
    stays an affine transform.
    """
    min_lon, min_lat, max_lon, max_lat = bounds
    lon_span = max_lon - min_lon
    lat_span = max_lat - min_lat
    if lon_span <= 0 or lat_span <= 0:
        return (0.0, 0.0, 0.0)
    center_lat = math.radians((min_lat + max_lat) / 2.0)
    cos_lat = max(math.cos(center_lat), 1e-6)
    x = (lon_deg - min_lon) / lon_span
    y = ((lat_deg - min_lat) / lon_span) / cos_lat
    return (x, y, 0.0)


def latlon_to_xy_metric(lat_deg, lon_deg, ref_lat=0.0):
    """WGS84 metric flat projection returning meters from the origin."""
    lat_rad = math.radians(ref_lat)
    sin_lat = math.sin(lat_rad)
    n = _WGS84_A / math.sqrt(1.0 - _WGS84_E_SQ * sin_lat * sin_lat)
    m = (
        _WGS84_A
        * (1.0 - _WGS84_E_SQ)
        / math.pow(1.0 - _WGS84_E_SQ * sin_lat * sin_lat, 1.5)
    )
    lat_to_m = math.radians(m)
    lon_to_m = math.radians(n) * math.cos(lat_rad)
    return (lon_deg * lon_to_m, lat_deg * lat_to_m, 0.0)


# ---------------------------------------------------------------------------
#  Dispatch helpers
# ---------------------------------------------------------------------------

def flat_project(lat_deg, lon_deg, settings, bounds=None):
    """Project a single lat/lon point in flat mode according to *settings*."""
    mode = settings.flat_unit_mode
    aspect = getattr(settings, "flat_aspect_mode", "PRESERVE")

    if mode == "NORMALIZED":
        b = bounds if bounds is not None else (-180.0, -90.0, 180.0, 90.0)
        if aspect == "PRESERVE":
            return latlon_to_xy_normalized(lat_deg, lon_deg, b)
        # STRETCH: normalize both axes independently to [0, 1]
        min_lon, min_lat, max_lon, max_lat = b
        lon_span = max_lon - min_lon
        lat_span = max_lat - min_lat
        if lon_span <= 0 or lat_span <= 0:
            return (0.0, 0.0, 0.0)
        return ((lon_deg - min_lon) / lon_span, (lat_deg - min_lat) / lat_span, 0.0)

    if mode == "ACCURATE":
        ref_lat = (bounds[1] + bounds[3]) / 2.0 if bounds else 0.0
        return latlon_to_xy_metric(lat_deg, lon_deg, ref_lat)

    # FIXED
    if settings.flat_fit_to_bbox and bounds is not None:
        if aspect == "PRESERVE":
            return latlon_to_xy_with_bounds(
                lat_deg, lon_deg, settings.flat_scale, bounds,
            )
        # STRETCH: normalize to bounds without aspect correction
        min_lon, min_lat, max_lon, max_lat = bounds
        lon_span = max_lon - min_lon
        lat_span = max_lat - min_lat
        if lon_span <= 0 or lat_span <= 0:
            return (0.0, 0.0, 0.0)
        x = ((lon_deg - min_lon) / lon_span) * settings.flat_scale
        y = ((lat_deg - min_lat) / lat_span) * settings.flat_scale
        return (x, y, 0.0)

    if aspect == "PRESERVE":
        # Apply cos(lat) correction to longitude (X) so 1 deg lon and 1 deg
        # lat have the same visual length: meridians converge away from the
        # equator, so a degree of longitude covers less ground the higher
        # |lat| gets. With no bounds available here, each point corrects
        # against its own latitude rather than one shared reference, so this
        # is only locally affine - it shears slightly across a
        # wide-latitude-range dataset. Pass bounds (flat_fit_to_bbox) for a
        # single reference latitude instead.
        cos_lat = math.cos(math.radians(lat_deg))
        return (lon_deg * settings.flat_scale * cos_lat, lat_deg * settings.flat_scale, 0.0)

    return latlon_to_xy(lat_deg, lon_deg, settings.flat_scale)


def project_point(
    lat_deg,
    lon_deg,
    ele=0.0,
    settings=None,
    bounds=None,
    radius=None,
):
    """Project a single point based on the active coordinate settings.

    *radius* overrides ``settings.globe_radius`` when projecting onto the
    sphere (useful for overlay offsets).
    """
    if settings is None or settings.coordinate_space == "GLOBE":
        r = radius if radius is not None else (settings.globe_radius if settings else 10.0)
        base = latlon_to_xyz(lat_deg, lon_deg, r)
        if ele == 0.0:
            return base
        normal = latlon_to_xyz(lat_deg, lon_deg, 1.0)
        return (
            base[0] + normal[0] * ele * 0.001,
            base[1] + normal[1] * ele * 0.001,
            base[2] + normal[2] * ele * 0.001,
        )

    # FLAT modes
    x, y, _ = flat_project(lat_deg, lon_deg, settings, bounds)

    if settings.flat_unit_mode == "ACCURATE":
        return (x, y, ele)
    if settings.flat_unit_mode == "NORMALIZED":
        return (x, y, ele * 0.001)
    # FIXED
    return (x, y, ele * settings.flat_scale * 0.001)


# ---------------------------------------------------------------------------
#  Blender PropertyGroup
# ---------------------------------------------------------------------------

# Must match _GLOBE_OVERLAY_ROOT_NAME in country_wireframes.py. Not imported
# from there to avoid a circular import (that module imports this one).
_GLOBE_OVERLAY_ROOT_NAME = "GeoOverlayRoot"


def _on_overlay_offset_updated(self, context):
    """Push the slider value live onto the overlay-root empty's scale, so
    Overlay Offset keeps working as a real-time control even though the
    offset is no longer baked into wireframe vertex positions at build
    time (see _ensure_globe_overlay_root_empty in country_wireframes.py).
    A no-op before that empty exists - it just becomes the seed scale the
    next time one is created.
    """
    overlay_root = bpy.data.objects.get(_GLOBE_OVERLAY_ROOT_NAME)
    if overlay_root is None or overlay_root.type != "EMPTY":
        return
    s = 1.0 + self.overlay_offset
    overlay_root.scale = (s, s, s)


class GeoCoordSettings(bpy.types.PropertyGroup):
    coordinate_space: EnumProperty(
        name="Space",
        description="Where to project imported coordinates",
        items=[
            ("GLOBE", "Globe", "Project onto a sphere"),
            ("FLAT", "Flat", "Project in flat space"),
        ],
        default="GLOBE",
    )
    globe_radius: FloatProperty(
        name="Radius",
        description="Radius of the globe for projection",
        default=10.0,
        min=0.001,
        soft_max=1000.0,
    )
    flat_unit_mode: EnumProperty(
        name="Unit Mode",
        description="How to scale flat coordinates",
        items=[
            ("FIXED", "Fixed", "Manual scale multiplier"),
            ("NORMALIZED", "Normalized", "Auto-scale to fit world in [0, 1]"),
            ("ACCURATE", "Accurate", "WGS84 meters-per-degree"),
        ],
        default="FIXED",
    )
    flat_scale: FloatProperty(
        name="Scale",
        description="Multiplier for lon/lat in flat space (Fixed mode)",
        default=0.1,
        min=0.000001,
        soft_max=1.0,
    )
    flat_aspect_mode: EnumProperty(
        name="Aspect",
        description="How to handle latitude-dependent aspect ratio in flat mode",
        items=[
            ("STRETCH", "Stretch", "Simple lon/lat multiplication, no correction"),
            ("PRESERVE", "Preserve", "Correct for latitude so lon and lat have equal visual length"),
        ],
        default="PRESERVE",
    )
    flat_fit_to_bbox: BoolProperty(
        name="Fit to Bounds",
        description="Aspect-correct flat imports within the source bounding box",
        default=False,
    )
    overlay_offset: FloatProperty(
        name="Overlay Offset",
        description=(
            "Push overlays above the reference globe surface, as a "
            "fraction of globe radius. Live - drives GeoOverlayRoot's "
            "scale directly, so it keeps working after the wireframes "
            "have already been created"
        ),
        default=0.001,
        min=0.0,
        max=0.1,
        update=_on_overlay_offset_updated,
    )


# ---------------------------------------------------------------------------
#  Registration
# ---------------------------------------------------------------------------

CLASSES = (GeoCoordSettings,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.geo_coord_settings = bpy.props.PointerProperty(
        type=GeoCoordSettings,
    )


def unregister():
    if hasattr(bpy.types.Scene, "geo_coord_settings"):
        del bpy.types.Scene.geo_coord_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()

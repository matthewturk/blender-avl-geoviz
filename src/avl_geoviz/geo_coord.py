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

    # *bounds* always supplies the reference latitude for aspect correction;
    # it only becomes the fit/normalize target when flat_fit_to_bbox is on.
    fit = bounds is not None and settings.flat_fit_to_bbox

    if mode == "NORMALIZED":
        b = bounds if fit else (-180.0, -90.0, 180.0, 90.0)
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
    if fit:
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
        # Scale longitude (X) by cos(ref_lat) so 1 deg lon and 1 deg lat have
        # the same visual length near the data. This must be ONE shared
        # reference latitude: scaling each point by its own cos(lat) is a
        # sinusoidal projection, which shears shapes in proportion to |lon|
        # (e.g. Colorado's west edge leans ~50 degrees). Without bounds there
        # is no data center, so fall back to the equator (no correction).
        ref_lat = (bounds[1] + bounds[3]) / 2.0 if bounds is not None else 0.0
        cos_lat = math.cos(math.radians(ref_lat))
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


def lonlat_mapping_props(settings, bounds=None):
    """Custom properties telling the Geo LonLat shader group (geo_raster.py)
    how to get longitude/latitude back from an object's local coordinates.

    Globe objects need nothing but the flag: lon/lat come from the direction
    of the local position. Every flat projection is axis-aligned and affine
    in lon/lat (x depends only on lon, y only on lat), so three projected
    points give its scale and offset: local xy = scale * (lon, lat) + offset.
    """
    if settings is None or settings.coordinate_space == "GLOBE":
        return {"geo_lonlat_flat": 0.0}
    x0, y0, _ = flat_project(0.0, 0.0, settings, bounds)
    x1, _, _ = flat_project(0.0, 1.0, settings, bounds)
    _, y1, _ = flat_project(1.0, 0.0, settings, bounds)
    return {
        "geo_lonlat_flat": 1.0,
        "geo_lonlat_scale": (x1 - x0, y1 - y0, 1.0),
        "geo_lonlat_offset": (x0, y0, 0.0),
    }


def store_lonlat_attributes(data, mapping):
    """Give mesh or (hair) curves *data* POINT attributes geo_lon and geo_lat
    (degrees) for every point, computed from its position with *mapping*
    (a lonlat_mapping_props dict), so Geometry Nodes and shaders can read
    lon/lat directly with Named Attribute / Attribute nodes.

    Computed from positions rather than from source coordinates so they're
    right for the points importers add or remove (great-circle densifying,
    simplifying). Float32, like positions: about 1 m near 90 deg longitude.
    """
    import numpy as np

    position = data.attributes.get("position")
    count = len(position.data) if position is not None else 0
    if count == 0:
        return
    pos = np.empty(count * 3, dtype=np.float32)
    position.data.foreach_get("vector", pos)
    x, y, z = pos.reshape(-1, 3).astype(np.float64).T
    if mapping.get("geo_lonlat_flat"):
        sx, sy, _ = mapping["geo_lonlat_scale"]
        ox, oy, _ = mapping["geo_lonlat_offset"]
        lon, lat = (x - ox) / sx, (y - oy) / sy
    else:
        lon = np.degrees(np.arctan2(y, x))
        lat = np.degrees(np.arctan2(z, np.hypot(x, y)))
    for name, values in (("geo_lon", lon), ("geo_lat", lat)):
        attr = data.attributes.get(name)
        if attr is None or attr.data_type != "FLOAT" or attr.domain != "POINT":
            if attr is not None:
                data.attributes.remove(attr)
            attr = data.attributes.new(name, "FLOAT", "POINT")
        attr.data.foreach_set("value", values.astype(np.float32))


def stamp_lonlat_mapping(obj, settings, bounds=None):
    """Write lonlat_mapping_props onto *obj*. A flat object placed by its
    location (point markers) has that location folded into the offset, so
    its local coordinates still map back to the right lon/lat."""
    props = lonlat_mapping_props(settings, bounds)
    if "geo_lonlat_offset" in props:
        ox, oy, oz = props["geo_lonlat_offset"]
        loc = obj.location
        props["geo_lonlat_offset"] = (ox - loc[0], oy - loc[1], oz)
    else:
        for key in ("geo_lonlat_scale", "geo_lonlat_offset"):
            if key in obj:
                del obj[key]
    for key, value in props.items():
        obj[key] = value


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

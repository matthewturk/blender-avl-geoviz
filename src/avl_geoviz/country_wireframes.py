import math
import json
import datetime
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import bpy
from bpy_extras.io_utils import ImportHelper

from . import geo_coord
from . import _osm_safety_shared
from .geo_coord import (
    flat_project,
    latlon_to_unit,
    latlon_to_xy,
    latlon_to_xy_with_bounds,
    latlon_to_xyz,
    project_point,
)

# NASA Visible Earth "Blue Marble Next Generation" (Dec. 2004), cloud-free.
# The imagerecords folder number below was verified live (curl) - NASA
# reorganized their directory numbering at some point, and the folder this
# constant used to point at (74000/74117) now 404s; 73000/73909 serves the
# exact same image. Two resolutions are confirmed to exist at this record:
# 5400x2700 (~2.6 MB) and 21600x10800 (~30 MB, ~700 MB uncompressed in
# VRAM) - no in-between sizes are published for this specific image.
_BLUE_MARBLE_STANDARD_URL = (
    "https://eoimages.gsfc.nasa.gov/images/imagerecords/73000/"
    "73909/world.topo.bathy.200412.3x5400x2700.jpg"
)
_BLUE_MARBLE_FULL_RES_URL = (
    "https://eoimages.gsfc.nasa.gov/images/imagerecords/73000/"
    "73909/world.topo.bathy.200412.3x21600x10800.jpg"
)
_LEGACY_BLUE_MARBLE_URL = _BLUE_MARBLE_STANDARD_URL

# NASA's companion cloud-layer texture (same Blue Marble collection):
# black = clear sky, white = cloud - used as a mix factor, not an alpha
# channel (the file has no transparency). Only a 2048x1024 size is
# published at this record (verified) - which is plenty, since clouds are
# a soft decorative overlay here, not the primary detail source.
_BLUE_MARBLE_CLOUDS_URL = (
    "https://eoimages.gsfc.nasa.gov/images/imagerecords/57000/"
    "57747/cloud_combined_2048.jpg"
)


def _as_text(value):
    if isinstance(value, str):
        return value
    return ""


def _tokenize_values(raw_text):
    separators = [",", ";", "\n", "\t"]
    normalized = _as_text(raw_text)
    for sep in separators[1:]:
        normalized = normalized.replace(sep, separators[0])
    return [token.strip() for token in normalized.split(separators[0]) if token.strip()]


# Natural Earth ships admin-0 country boundaries at three fixed
# generalization scales. 110m is the coarsest (whole-globe views); 10m is
# the finest and the closest coastline match to photographic imagery like
# Blue Marble, at the cost of a much larger download and vertex count.
_NE_ADMIN0_COUNTRIES_URLS = {
    "110M": (
        "https://naturalearth.s3.amazonaws.com/"
        "110m_cultural/ne_110m_admin_0_countries.zip"
    ),
    "50M": (
        "https://naturalearth.s3.amazonaws.com/"
        "50m_cultural/ne_50m_admin_0_countries.zip"
    ),
    "10M": (
        "https://naturalearth.s3.amazonaws.com/"
        "10m_cultural/ne_10m_admin_0_countries.zip"
    ),
}


def _load_world_dataset(gpd, scale="110M"):
    scale = str(scale).upper()
    if scale not in _NE_ADMIN0_COUNTRIES_URLS:
        scale = "110M"

    # geopandas < 1.0 ships a bundled naturalearth_lowres dataset, but only
    # at 110m - there's no bundled 50m/10m, so this shortcut only applies
    # there; other scales always go through the URL fetch below.
    if scale == "110M":
        try:
            return gpd.read_file(gpd.datasets.get_path("naturalearth_lowres"))
        except Exception:
            pass

    url = _NE_ADMIN0_COUNTRIES_URLS[scale]
    print(f"[Geo Wireframes] Fetching world boundaries ({scale} scale) from {url} ...")
    try:
        world = gpd.read_file(url)
    except Exception as exc:
        raise RuntimeError(
            "Could not load world boundaries from geopandas built-ins or "
            f"the Natural Earth {scale} URL. If you are offline, use "
            "geopandas with bundled datasets or pre-download the Natural "
            f"Earth countries ZIP. Last error: {exc}"
        ) from exc
    print(f"[Geo Wireframes] World boundaries fetch complete ({len(world)} feature(s)).")
    return world


def _pick_column(columns, candidates):
    lower_map = {str(col).lower(): col for col in columns}
    for candidate in candidates:
        if candidate in columns:
            return candidate
        lowered = str(candidate).lower()
        if lowered in lower_map:
            return lower_map[lowered]
    return None


def _normalize_world_columns(gpd, world):
    geometry_series = world.geometry

    name_col = _pick_column(
        world.columns,
        ["name", "name_en", "admin", "formal_en", "sovereignt"],
    )
    iso2_col = _pick_column(
        world.columns,
        ["iso_a2", "iso_a2_eh", "wb_a2", "adm0_a2"],
    )
    iso3_col = _pick_column(
        world.columns,
        ["iso_a3", "iso_a3_eh", "adm0_a3", "wb_a3", "gu_a3"],
    )
    continent_col = _pick_column(
        world.columns,
        ["continent", "region_un", "region_wb"],
    )

    if name_col is None:
        name_series = ["Unknown"] * len(world)
    else:
        name_series = world[name_col].fillna("Unknown").astype(str)

    if iso2_col is None:
        iso2_series = [""] * len(world)
    else:
        iso2_series = world[iso2_col].fillna("").astype(str)

    if iso3_col is None:
        iso3_series = [""] * len(world)
    else:
        iso3_series = world[iso3_col].fillna("").astype(str)

    if continent_col is None:
        continent_series = ["Unknown"] * len(world)
    else:
        continent_series = world[continent_col].fillna("Unknown").astype(str)

    normalized = gpd.GeoDataFrame(
        {
            "name": name_series,
            "iso_a2": iso2_series,
            "iso_a3": iso3_series,
            "continent": continent_series,
            "geometry": geometry_series,
        },
        geometry="geometry",
        crs=world.crs,
    )
    return normalized


def _load_geo_data(scale="110M"):
    print(f"[Geo Wireframes] Loading world boundary dataset ({scale} scale)...")
    try:
        import geopandas as gpd
    except Exception as exc:
        raise RuntimeError(f"Could not import geopandas: {exc}") from exc

    try:
        from geonamescache import GeonamesCache
    except Exception as exc:
        raise RuntimeError(f"Could not import geonamescache: {exc}") from exc

    world = _load_world_dataset(gpd, scale)
    world = _normalize_world_columns(gpd, world)
    world = world.dropna(subset=["geometry"])

    # Canonical lookup from Natural Earth names/codes
    countries_by_key = {}
    for _, row in world.iterrows():
        name = str(row["name"])
        iso2 = str(row["iso_a2"]).upper()
        iso3 = str(row["iso_a3"]).upper()
        countries_by_key[name.lower()] = row
        if iso2 and iso2 != "-99":
            countries_by_key[iso2.lower()] = row
        if iso3 and iso3 != "-99":
            countries_by_key[iso3.lower()] = row

    # Alias lookup from geonamescache to map common ISO/name variants.
    gc = GeonamesCache()
    for iso2, info in gc.get_countries().items():
        iso2_key = str(iso2).upper()
        iso3_key = str(info.get("iso3", "")).upper()
        name_key = str(info.get("name", "")).strip().lower()

        row = None
        if iso2_key and iso2_key.lower() in countries_by_key:
            row = countries_by_key[iso2_key.lower()]
        elif iso3_key and iso3_key.lower() in countries_by_key:
            row = countries_by_key[iso3_key.lower()]

        if row is None:
            continue

        if name_key:
            countries_by_key[name_key] = row
        if iso2_key:
            countries_by_key[iso2_key.lower()] = row
        if iso3_key:
            countries_by_key[iso3_key.lower()] = row

    # Build continent centroids from unioned geometries.
    continents = {}
    grouped = world.groupby("continent")
    for continent, subset in grouped:
        continent_name = str(continent)
        if continent_name.lower() == "seven seas (open ocean)":
            continue
        unioned = subset.unary_union
        point = unioned.representative_point()
        continents[continent_name.lower()] = {
            "name": continent_name,
            "point": point,
            "geometry": unioned,
        }

    print(
        f"[Geo Wireframes] World boundary dataset ready: {len(world)} countries, "
        f"{len(continents)} continents."
    )
    return world, countries_by_key, continents


def _m49_lookup_by_iso3():
    """Map ISO3 -> zero-padded UN M49 numeric code, via geonamescache.

    geonamescache's "isonumeric" field is the same numbering as UN M49 for
    actual countries, so this needs no separate M49 data source.
    """
    from geonamescache import GeonamesCache

    lookup = {}
    for info in GeonamesCache().get_countries().values():
        iso3 = str(info.get("iso3", "")).upper()
        m49 = info.get("isonumeric")
        if iso3 and m49 is not None:
            lookup[iso3] = f"{int(m49):03d}"
    return lookup


def _blue_marble_fallback_path():
    return os.path.join(tempfile.gettempdir(), "blender_geo_blue_marble.png")


def _file_url_from_path(path):
    absolute = os.path.abspath(path)
    return urllib.parse.urljoin("file:", urllib.request.pathname2url(absolute))


def _blue_marble_fallback_url():
    return _file_url_from_path(_blue_marble_fallback_path())


def _ensure_blue_marble_fallback_texture():
    target_path = _blue_marble_fallback_path()
    if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
        return target_path

    print("[Geo Wireframes] Generating fallback Blue Marble texture (no cached copy found)...")
    world, _, _ = _load_geo_data()

    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except Exception as exc:
        raise RuntimeError(
            "Could not import matplotlib/numpy for Blue Marble fallback generation."
        ) from exc

    width = 3072
    height = width // 2
    x = np.linspace(-1.0, 1.0, width, dtype=float)[None, :]
    y = np.linspace(-1.0, 1.0, height, dtype=float)[:, None]

    ocean = np.zeros((height, width, 3), dtype=float)
    ocean[..., 0] = 0.02 + 0.04 * (1.0 - y * y)
    ocean[..., 1] = 0.18 + 0.18 * (1.0 - np.abs(y))
    ocean[..., 2] = 0.32 + 0.35 * (1.0 - 0.45 * np.abs(y) + 0.1 * np.cos(np.pi * x))
    ocean = np.clip(ocean, 0.0, 1.0)

    fig = plt.figure(figsize=(12, 6), dpi=256)
    ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
    fig.patch.set_facecolor((0.02, 0.10, 0.20))
    ax.set_facecolor((0.02, 0.10, 0.20))
    ax.imshow(ocean, extent=[-180, 180, -90, 90], origin="lower")
    ax.fill_between([-180, 180], 72, 90, color="#f3f7fb", alpha=0.9)
    ax.fill_between([-180, 180], -90, -72, color="#eef4fa", alpha=0.85)
    world.plot(
        ax=ax,
        color="#6f9f58",
        edgecolor="#d8d3b2",
        linewidth=0.25,
        zorder=3,
    )
    world.boundary.plot(ax=ax, color="#36543a", linewidth=0.2, zorder=4)
    ax.set_xlim(-180, 180)
    ax.set_ylim(-90, 90)
    ax.axis("off")

    fd, temp_path = tempfile.mkstemp(prefix="geo_blue_marble_", suffix=".png")
    os.close(fd)
    try:
        fig.savefig(temp_path, dpi=256, facecolor=fig.get_facecolor())
        os.replace(temp_path, target_path)
    except Exception as exc:
        raise RuntimeError(
            f"Could not generate Blue Marble fallback texture: {exc}"
        ) from exc
    finally:
        plt.close(fig)
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    print(f"[Geo Wireframes] Fallback Blue Marble texture ready: {target_path}")
    return target_path


def _resolve_blue_marble_url(raw_url):
    url = str(raw_url).strip()
    if not url or url == _LEGACY_BLUE_MARBLE_URL or url == _blue_marble_fallback_url():
        return _file_url_from_path(_ensure_blue_marble_fallback_texture())
    return url


def _resolve_blue_marble_quality_url(quality, custom_url):
    if quality == "STANDARD":
        return _BLUE_MARBLE_STANDARD_URL
    if quality == "FULL":
        return _BLUE_MARBLE_FULL_RES_URL
    return _resolve_blue_marble_url(custom_url)


def _normalize_vec3(vec):
    length = math.sqrt(vec[0] * vec[0] + vec[1] * vec[1] + vec[2] * vec[2])
    if length <= 1e-12:
        return (0.0, 0.0, 1.0)
    return (vec[0] / length, vec[1] / length, vec[2] / length)


def _dot3(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross3(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _slerp_unit(a, b, t):
    dot = max(-1.0, min(1.0, a[0] * b[0] + a[1] * b[1] + a[2] * b[2]))

    # For nearly parallel vectors, linear interpolation is more stable.
    if dot > 0.9995:
        blended = (
            (1.0 - t) * a[0] + t * b[0],
            (1.0 - t) * a[1] + t * b[1],
            (1.0 - t) * a[2] + t * b[2],
        )
        return _normalize_vec3(blended)

    omega = math.acos(dot)
    sin_omega = math.sin(omega)
    if abs(sin_omega) <= 1e-12:
        return a

    scale_a = math.sin((1.0 - t) * omega) / sin_omega
    scale_b = math.sin(t * omega) / sin_omega
    return (
        scale_a * a[0] + scale_b * b[0],
        scale_a * a[1] + scale_b * b[1],
        scale_a * a[2] + scale_b * b[2],
    )


def _line_coords_to_xyz(coords, globe_radius, max_segment_deg=0.0):
    if not coords:
        return []

    try:
        lon0, lat0, *_ = coords[0]
    except Exception:
        return []

    points = [latlon_to_xyz(lat0, lon0, globe_radius)]
    prev_unit = latlon_to_unit(lat0, lon0)

    for coord in coords[1:]:
        try:
            lon1, lat1, *_ = coord
        except Exception:
            continue

        curr_unit = latlon_to_unit(lat1, lon1)
        dot = max(
            -1.0,
            min(
                1.0,
                prev_unit[0] * curr_unit[0]
                + prev_unit[1] * curr_unit[1]
                + prev_unit[2] * curr_unit[2],
            ),
        )
        arc_deg = math.degrees(math.acos(dot))

        segments = 1
        if max_segment_deg > 0.0 and arc_deg > max_segment_deg:
            segments = int(math.ceil(arc_deg / max_segment_deg))

        for step in range(1, segments + 1):
            t = step / segments
            unit = _slerp_unit(prev_unit, curr_unit, t)
            points.append(
                (
                    unit[0] * globe_radius,
                    unit[1] * globe_radius,
                    unit[2] * globe_radius,
                )
            )

        prev_unit = curr_unit

    return points


def _line_coords_to_latlon_xy(coords, coord_settings, bounds=None, max_segment_deg=0.0):
    if not coords:
        return []

    try:
        lon0, lat0, *_ = coords[0]
    except Exception:
        return []

    points = [flat_project(lat0, lon0, coord_settings, bounds)]

    for coord in coords[1:]:
        try:
            lon1, lat1, *_ = coord
        except Exception:
            continue

        delta_lon = lon1 - lon0
        if delta_lon > 180.0:
            delta_lon -= 360.0
        elif delta_lon < -180.0:
            delta_lon += 360.0

        delta_lat = lat1 - lat0
        segments = 1
        if max_segment_deg > 0.0:
            arc_deg = max(abs(delta_lon), abs(delta_lat))
            if arc_deg > max_segment_deg:
                segments = int(math.ceil(arc_deg / max_segment_deg))

        for step in range(1, segments + 1):
            t = step / segments
            lon = lon0 + delta_lon * t
            lat = lat0 + delta_lat * t
            points.append(flat_project(lat, lon, coord_settings, bounds))

        lon0 = lon1
        lat0 = lat1

    return points


def _merge_lonlat_bounds(bounds, lon, lat):
    if lon is None or lat is None:
        return bounds

    lon = float(lon)
    lat = float(lat)
    if bounds is None:
        return (lon, lat, lon, lat)

    min_lon, min_lat, max_lon, max_lat = bounds
    return (
        min(min_lon, lon),
        min(min_lat, lat),
        max(max_lon, lon),
        max(max_lat, lat),
    )


def _merge_bounds(bounds, other_bounds):
    if other_bounds is None:
        return bounds
    min_lon, min_lat, max_lon, max_lat = other_bounds
    bounds = _merge_lonlat_bounds(bounds, min_lon, min_lat)
    bounds = _merge_lonlat_bounds(bounds, max_lon, max_lat)
    return bounds


def _bounds_from_coord_sequence(coords):
    bounds = None
    for coord in coords:
        lonlat = _coord_to_lonlat(coord)
        if lonlat is None:
            continue
        lon, lat = lonlat
        bounds = _merge_lonlat_bounds(bounds, lon, lat)
    return bounds


def _bounds_from_geometry(geometry):
    try:
        min_lon, min_lat, max_lon, max_lat = geometry.bounds
    except Exception:
        return None

    if None in (min_lon, min_lat, max_lon, max_lat):
        return None
    return (float(min_lon), float(min_lat), float(max_lon), float(max_lat))


def _bounds_from_geojson_payload(payload):
    bounds = None
    for feature in payload:
        for line in feature.get("lines", []):
            bounds = _merge_bounds(bounds, _bounds_from_coord_sequence(line))
        for point in feature.get("points", []):
            lonlat = _coord_to_lonlat(point)
            if lonlat is None:
                continue
            lon, lat = lonlat
            bounds = _merge_lonlat_bounds(bounds, lon, lat)
    return bounds


def _coords_lonlat_equal(coord_a, coord_b, epsilon=1e-12):
    try:
        lon_a, lat_a, *_ = coord_a
        lon_b, lat_b, *_ = coord_b
    except Exception:
        return False
    return abs(lon_a - lon_b) <= epsilon and abs(lat_a - lat_b) <= epsilon


def _points_xyz_equal(point_a, point_b, epsilon=1e-8):
    return (
        abs(point_a[0] - point_b[0]) <= epsilon
        and abs(point_a[1] - point_b[1]) <= epsilon
        and abs(point_a[2] - point_b[2]) <= epsilon
    )


def _point_segment_angular_distance_deg(point, seg_start, seg_end):
    a = _normalize_vec3(seg_start)
    b = _normalize_vec3(seg_end)
    p = _normalize_vec3(point)

    ab_dot = max(-1.0, min(1.0, _dot3(a, b)))
    ab_angle = math.acos(ab_dot)
    if ab_angle <= 1e-12:
        ap_dot = max(-1.0, min(1.0, _dot3(a, p)))
        return math.degrees(math.acos(ap_dot))

    n = _cross3(a, b)
    n_len = math.sqrt(_dot3(n, n))
    if n_len <= 1e-12:
        ap_dot = max(-1.0, min(1.0, _dot3(a, p)))
        bp_dot = max(-1.0, min(1.0, _dot3(b, p)))
        return min(
            math.degrees(math.acos(ap_dot)),
            math.degrees(math.acos(bp_dot)),
        )

    n_hat = (n[0] / n_len, n[1] / n_len, n[2] / n_len)
    projection = (
        p[0] - _dot3(p, n_hat) * n_hat[0],
        p[1] - _dot3(p, n_hat) * n_hat[1],
        p[2] - _dot3(p, n_hat) * n_hat[2],
    )
    q = _normalize_vec3(projection)

    aq = math.acos(max(-1.0, min(1.0, _dot3(a, q))))
    qb = math.acos(max(-1.0, min(1.0, _dot3(q, b))))

    on_arc = abs((aq + qb) - ab_angle) <= 1e-5
    if on_arc:
        pq_dot = max(-1.0, min(1.0, _dot3(p, q)))
        return math.degrees(math.acos(pq_dot))

    ap_dot = max(-1.0, min(1.0, _dot3(a, p)))
    bp_dot = max(-1.0, min(1.0, _dot3(b, p)))
    return min(
        math.degrees(math.acos(ap_dot)),
        math.degrees(math.acos(bp_dot)),
    )


def _point_segment_distance_2d(point, seg_start, seg_end):
    px, py = point
    ax, ay = seg_start
    bx, by = seg_end

    dx = bx - ax
    dy = by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-18:
        return math.hypot(px - ax, py - ay)

    t = ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    cx = ax + t * dx
    cy = ay + t * dy
    return math.hypot(px - cx, py - cy)


def _simplify_line_coords(coords, tolerance_deg):
    if tolerance_deg <= 0.0 or len(coords) <= 2:
        return list(coords)

    is_closed = len(coords) > 3 and _coords_lonlat_equal(coords[0], coords[-1])
    working = list(coords[:-1]) if is_closed else list(coords)
    if len(working) <= 2:
        return list(coords)

    line2d = []
    for coord in working:
        try:
            lon, lat, *_ = coord
        except Exception:
            return list(coords)
        line2d.append((float(lon), float(lat)))

    keep = [False] * len(working)
    keep[0] = True
    keep[-1] = True
    stack = [(0, len(working) - 1)]

    while stack:
        start_idx, end_idx = stack.pop()
        max_dist = -1.0
        max_idx = -1

        start_pt = line2d[start_idx]
        end_pt = line2d[end_idx]
        for idx in range(start_idx + 1, end_idx):
            dist = _point_segment_distance_2d(line2d[idx], start_pt, end_pt)
            if dist > max_dist:
                max_dist = dist
                max_idx = idx

        if max_idx >= 0 and max_dist > tolerance_deg:
            keep[max_idx] = True
            if max_idx - start_idx > 1:
                stack.append((start_idx, max_idx))
            if end_idx - max_idx > 1:
                stack.append((max_idx, end_idx))

    simplified = [coord for idx, coord in enumerate(working) if keep[idx]]
    if len(simplified) < 2:
        simplified = [working[0], working[-1]]

    if is_closed:
        if len(simplified) < 3:
            simplified = working[:3]
        simplified.append(simplified[0])

    return simplified


def _simplify_lines(lines, tolerance_deg):
    if tolerance_deg <= 0.0:
        return [list(line) for line in lines]

    simplified = []
    for line in lines:
        reduced = _simplify_line_coords(line, tolerance_deg)
        if len(reduced) >= 2:
            simplified.append(reduced)
    return simplified


def _simplify_spherical_points(points, tolerance_deg):
    if tolerance_deg <= 0.0 or len(points) <= 2:
        return list(points)

    is_closed = len(points) > 3 and _points_xyz_equal(points[0], points[-1])
    working = list(points[:-1]) if is_closed else list(points)
    if len(working) <= 2:
        return list(points)

    keep = [False] * len(working)
    keep[0] = True
    keep[-1] = True
    stack = [(0, len(working) - 1)]

    while stack:
        start_idx, end_idx = stack.pop()
        max_dist = -1.0
        max_idx = -1

        start_pt = working[start_idx]
        end_pt = working[end_idx]
        for idx in range(start_idx + 1, end_idx):
            dist = _point_segment_angular_distance_deg(
                working[idx],
                start_pt,
                end_pt,
            )
            if dist > max_dist:
                max_dist = dist
                max_idx = idx

        if max_idx >= 0 and max_dist > tolerance_deg:
            keep[max_idx] = True
            if max_idx - start_idx > 1:
                stack.append((start_idx, max_idx))
            if end_idx - max_idx > 1:
                stack.append((max_idx, end_idx))

    simplified = [point for idx, point in enumerate(working) if keep[idx]]
    if len(simplified) < 2:
        simplified = [working[0], working[-1]]

    if is_closed:
        if len(simplified) < 3:
            simplified = working[:3]
        simplified.append(simplified[0])

    return simplified


def _line_coords_to_simplified_xyz(
    coords,
    globe_radius,
    max_segment_deg=0.0,
    spherical_tolerance_deg=0.0,
    coord_settings=None,
    latlon_bounds=None,
):
    use_flat = coord_settings is not None and coord_settings.coordinate_space == "FLAT"

    if use_flat:
        if spherical_tolerance_deg > 0.0:
            coords = _simplify_line_coords(coords, spherical_tolerance_deg)
        return _line_coords_to_latlon_xy(
            coords,
            coord_settings,
            latlon_bounds,
            max_segment_deg=max_segment_deg,
        )

    points = _line_coords_to_xyz(
        coords,
        globe_radius,
        max_segment_deg=max_segment_deg,
    )
    if spherical_tolerance_deg > 0.0:
        points = _simplify_spherical_points(points, spherical_tolerance_deg)
    return points


def _to_uv(lat_deg, lon_deg):
    u = (lon_deg + 180.0) / 360.0
    v = (lat_deg + 90.0) / 180.0
    return (u, v)


def _collapse_outliner_view():
    """Collapse the whole Outliner tree, if one is visible in the current
    screen layout.

    There's no per-collection "start collapsed" property exposed to Python -
    that expand/collapse state lives in the Outliner's own private UI
    session data, not on the Collection itself - so an Outliner UI operator
    (not a pure data one) is the only lever available, and it's necessarily
    all-or-nothing across whatever's currently shown in the outliner, not
    scoped to just the collections this script just created. Silently does
    nothing if no Outliner area is open (e.g. a non-default screen layout,
    or running headless).

    bpy.ops.outliner.collapse_all() doesn't exist (confirmed via a real
    AttributeError on Blender 5.3: "could not be found") - the actual,
    long-standing operator is outliner.show_one_level(open=False), which
    per its own name/docs collapses one level of the tree per call, not
    the whole hierarchy recursively in one shot. Calling it repeatedly
    (well past any realistic nesting depth this project's collections
    reach) reliably fully collapses regardless of exactly how many levels
    a single call actually closes - extra calls once nothing's left to
    collapse are harmless no-ops.
    """
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "OUTLINER":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None:
                continue
            with bpy.context.temp_override(window=window, area=area, region=region):
                for _ in range(20):
                    bpy.ops.outliner.show_one_level(open=False)
            return True
    return False


def _get_or_create_collection(parent, name):
    # Scoped to `parent`'s own children, NOT a global bpy.data.collections
    # lookup - a global lookup here silently re-attaches a same-named
    # collection from anywhere else in the file, including one that
    # _archive_geojson_imports() just archived: archiving only renames the
    # outer "GeoJSON Imports" wrapper with a timestamp, never its INNER
    # per-feature-type subcollections (e.g. "GeoJSON_unknown", used by any
    # GeoJSON with no recognized OSM tag - i.e. most non-OSM data), so a
    # later import reusing that same type_label would find the OLD,
    # already-archived subcollection by name and link it in as an
    # additional parent of the NEW "GeoJSON Imports" too - resurrecting
    # every object it still holds, with no old link ever removed. Real,
    # confirmed bug: two sequential GeoJSON imports (US counties, then US
    # states, neither with OSM tags so both use "GeoJSON_unknown") - the
    # second import's result held 3221 archived county objects it should
    # never have contained, on top of its own real 52.
    existing = _get_child_collection(parent, name)
    if existing is None:
        existing = bpy.data.collections.new(name)
        parent.children.link(existing)
    return existing


def _get_child_collection(parent, name):
    for child in parent.children:
        if child.name == name:
            return child
    return None


_GLOBE_ROOT_NAME = "GeoGlobeRoot"


def _ensure_globe_root_empty(context):
    """Get or create the empty that every geo-referenced object is parented
    to, so rotating/scaling this one empty carries the reference globe and
    all country/continent/GeoJSON wireframes with it.
    """
    root_empty = bpy.data.objects.get(_GLOBE_ROOT_NAME)
    if root_empty is None or root_empty.type != "EMPTY":
        root_empty = bpy.data.objects.new(_GLOBE_ROOT_NAME, None)
        root_empty.empty_display_type = "PLAIN_AXES"
        coord_settings = getattr(context.scene, "geo_coord_settings", None)
        root_empty.empty_display_size = (
            coord_settings.globe_radius if coord_settings is not None else 1.0
        )

    root = _get_or_create_collection(context.scene.collection, "Geo Wireframes")
    if root_empty.name not in root.objects:
        root.objects.link(root_empty)

    return root_empty


def _parent_to_globe_root(obj, root_empty):
    """Parent obj to root_empty with an identity parent-inverse (not "keep
    transform"). Every geo object's local coordinates are already baked in
    the same unrotated globe frame (see _line_coords_to_xyz), so leaving the
    parent-inverse as identity means root_empty's current rotation/scale
    applies uniformly to every object - whether it was parented before or
    after that rotation was set.
    """
    if obj is None or root_empty is None or obj is root_empty:
        return
    if obj.parent is not root_empty:
        obj.parent = root_empty
    obj.matrix_parent_inverse.identity()


_GLOBE_OVERLAY_ROOT_NAME = "GeoOverlayRoot"


def _ensure_globe_overlay_root_empty(context):
    """Get or create the empty that every *overlay* object (country/
    continent wireframes, GeoJSON imports, anchor points - everything
    except the reference globe mesh itself) is parented to, as a child of
    the globe root empty.

    All overlay geometry is now built at plain globe_radius (no offset
    baked into vertex positions). The radial push-out above the reference
    globe surface - previously a build-time-only "Overlay Offset" baked
    into every vertex - is instead this empty's own uniform scale, so it
    stays adjustable after creation: grab GeoOverlayRoot in the Outliner
    and change its Scale, or use the Overlay Offset slider, which drives
    this same scale via an update callback (see geo_coord.py) instead of
    only taking effect on the next regenerate.
    """
    overlay_root = bpy.data.objects.get(_GLOBE_OVERLAY_ROOT_NAME)
    is_new = overlay_root is None or overlay_root.type != "EMPTY"
    if is_new:
        overlay_root = bpy.data.objects.new(_GLOBE_OVERLAY_ROOT_NAME, None)
        overlay_root.empty_display_type = "PLAIN_AXES"
        coord_settings = getattr(context.scene, "geo_coord_settings", None)
        radius = coord_settings.globe_radius if coord_settings is not None else 1.0
        offset = coord_settings.overlay_offset if coord_settings is not None else 0.0
        overlay_root.empty_display_size = radius * 0.05
        overlay_root.scale = (1.0 + offset, 1.0 + offset, 1.0 + offset)

    globe_root = _ensure_globe_root_empty(context)
    _parent_to_globe_root(overlay_root, globe_root)

    root = _get_or_create_collection(context.scene.collection, "Geo Wireframes")
    if overlay_root.name not in root.objects:
        root.objects.link(overlay_root)

    return overlay_root


def _unique_collection_name(base_name):
    if bpy.data.collections.get(base_name) is None:
        return base_name

    index = 1
    while True:
        candidate = f"{base_name}.{index:03d}"
        if bpy.data.collections.get(candidate) is None:
            return candidate
        index += 1


def _archive_geojson_imports(root, imports_coll):
    archive_parent = _get_or_create_collection(root, "GeoJSON Archive")
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    archived_name = _unique_collection_name(f"GeoJSON Imports {stamp}")
    imports_coll.name = archived_name

    if imports_coll.name not in archive_parent.children:
        archive_parent.children.link(imports_coll)
    if imports_coll.name in root.children:
        root.children.unlink(imports_coll)

    return archived_name


def _build_sphere_mesh(
    mesh_name,
    radius,
    resolution,
    add_surface,
):
    # Built via the primitive_uv_sphere_add operator rather than bmesh -
    # bmesh has no compiled extension in the pip-installed `bpy` wheel
    # (unlike a full Blender install/its bundled Python), only in a real
    # Blender application - confirmed directly, see cli/HEADLESS.md. The
    # operator needs an active collection/view layer, which always exists
    # even fully headless with no GPU (confirmed directly too). Built into
    # a throwaway object so the rest of this function can keep working
    # purely on the resulting `mesh` datablock, exactly as before -
    # everything below here (UV unwrap, pole/seam handling) only reads
    # mesh.vertices/polygons/loops, with no assumption about how the mesh
    # was constructed, just that it's a normal UV-sphere topology.
    bpy.ops.mesh.primitive_uv_sphere_add(
        segments=resolution,
        ring_count=max(3, resolution // 2),
        radius=radius,
    )
    temp_obj = bpy.context.active_object
    mesh = temp_obj.data
    mesh.name = mesh_name
    bpy.data.objects.remove(temp_obj, do_unlink=True)
    mesh.update()

    uv_layer = mesh.uv_layers.new(name="GeoUV")
    if uv_layer is not None:
        uv_data = uv_layer.data
        loops = mesh.loops
        vertices = mesh.vertices

        # The two pole vertices sit exactly on the Z axis (x == y == 0.0,
        # not just close to it - create_uvsphere puts them there exactly),
        # so atan2(0, 0) is where every polar-cap triangle's apex loop
        # lands - always the SAME longitude (0 degrees / u=0.5), regardless
        # of which face is asking. That's unlike every other seam case
        # here, which is a discontinuity a face can straddle; this is a
        # constant that's simply wrong for most faces around the pole.
        pole_vertex_indices = {
            v.index for v in vertices if abs(v.co.x) < 1e-9 and abs(v.co.y) < 1e-9
        }

        for i, loop in enumerate(loops):
            coord = vertices[loop.vertex_index].co
            length = max(coord.length, 1e-8)
            z_ratio = coord.z / length
            z_ratio = max(-1.0, min(1.0, z_ratio))
            lat = math.degrees(math.asin(z_ratio))
            lon = math.degrees(math.atan2(coord.y, coord.x))
            uv_data[i].uv = _to_uv(lat, lon)

        # atan2-based longitude has a discontinuity exactly at +-180 degrees
        # (the antimeridian) - computed per-loop/per-vertex with no
        # awareness of which face a loop belongs to, so a face straddling
        # that seam (two geometrically adjacent vertices landing on
        # opposite sides of it) gets some loops at u~0 and others at u~1,
        # stretching the texture across the whole image width for that
        # face - a real, visible zigzag along the seam. Fixed per-FACE (not
        # per-vertex - a shared vertex needs different UV values depending
        # on which face is asking, which is exactly why UVs live on loops,
        # not vertices): any polygon whose loops span more than half the UV
        # width gets its "low" loops pushed past 1.0, staying geometrically
        # continuous with the "high" side instead of wrapping. Pole loops
        # are excluded here - their constant u=0.5 isn't a real seam
        # crossing, and including it would either mask a real wraparound
        # face or trigger a shift the pole loop doesn't need.
        for polygon in mesh.polygons:
            non_pole_loops = [
                li
                for li in polygon.loop_indices
                if loops[li].vertex_index not in pole_vertex_indices
            ]
            us = [uv_data[li].uv[0] for li in non_pole_loops]
            if len(us) >= 2 and max(us) - min(us) > 0.5:
                for li in non_pole_loops:
                    u, v = uv_data[li].uv
                    if u < 0.5:
                        uv_data[li].uv = (u + 1.0, v)

        # Now that every non-pole loop's u is seam-corrected, snap each
        # polar-cap triangle's apex loop to match the longitude of its own
        # base edge instead of the constant atan2(0, 0) value - this is
        # what was pulling every polar triangle's texture toward one fixed
        # meridian and produced the pinwheel crease down to the pole.
        for polygon in mesh.polygons:
            pole_loops = [
                li
                for li in polygon.loop_indices
                if loops[li].vertex_index in pole_vertex_indices
            ]
            if not pole_loops:
                continue
            other_loops = [li for li in polygon.loop_indices if li not in pole_loops]
            if not other_loops:
                continue
            avg_u = sum(uv_data[li].uv[0] for li in other_loops) / len(other_loops)
            for li in pole_loops:
                _, v = uv_data[li].uv
                uv_data[li].uv = (avg_u, v)

    if not add_surface:
        mesh.polygons.foreach_set("hide", [True] * len(mesh.polygons))

    return mesh


def _iter_line_coords(geom):
    if geom is None:
        return

    gtype = getattr(geom, "geom_type", "")
    if gtype == "LineString":
        yield list(geom.coords)
        return

    if gtype in {"MultiLineString", "GeometryCollection"}:
        for sub in geom.geoms:
            yield from _iter_line_coords(sub)
        return

    if gtype in {"Polygon", "MultiPolygon"}:
        yield from _iter_line_coords(geom.boundary)


def _iter_geojson_line_coords(geometry):
    if not isinstance(geometry, dict):
        return

    gtype = str(geometry.get("type", ""))
    coords = geometry.get("coordinates")

    if gtype == "LineString" and isinstance(coords, list):
        yield coords
        return

    if gtype == "MultiLineString" and isinstance(coords, list):
        for line in coords:
            if isinstance(line, list):
                yield line
        return

    if gtype == "Polygon" and isinstance(coords, list):
        for ring in coords:
            if isinstance(ring, list):
                yield ring
        return

    if gtype == "MultiPolygon" and isinstance(coords, list):
        for polygon in coords:
            if not isinstance(polygon, list):
                continue
            for ring in polygon:
                if isinstance(ring, list):
                    yield ring
        return

    if gtype == "GeometryCollection":
        for sub in geometry.get("geometries", []):
            yield from _iter_geojson_line_coords(sub)


def _iter_geojson_point_coords(geometry):
    if not isinstance(geometry, dict):
        return

    gtype = str(geometry.get("type", ""))
    coords = geometry.get("coordinates")

    if gtype == "Point" and isinstance(coords, list) and len(coords) >= 2:
        yield coords
        return

    if gtype == "MultiPoint" and isinstance(coords, list):
        for point in coords:
            if isinstance(point, list) and len(point) >= 2:
                yield point
        return

    if gtype == "GeometryCollection":
        for sub in geometry.get("geometries", []):
            yield from _iter_geojson_point_coords(sub)

def _feature_name_from_properties(properties, fallback):
    if not isinstance(properties, dict):
        return fallback

    # Helper function to ensure we skip empty values or literal 'None' strings
    def clean_val(key):
        val = properties.get(key)
        if val is None:
            return None
        s = str(val).strip()
        if s == "" or s.lower() == "none":
            return None
        return s

    # 1. Primary Check: Look for an explicit individual name
    name_candidates = ("name", "NAME", "Name", "name:en", "official_name")
    for key in name_candidates:
        valid_name = clean_val(key)
        if valid_name:
            return valid_name

    # 2. Secondary Check: Build from a street address if available
    housenumber = clean_val("addr:housenumber")
    street = clean_val("addr:street")
    if housenumber and street:
        return f"{housenumber} {street}"
    elif street:
        return street

    # 3. Tertiary Check: Use structural types if no location address exists
    type_candidates = ("building", "highway", "amenity", "landuse", "natural")
    for key in type_candidates:
        valid_type = clean_val(key)
        if valid_type:
            if valid_type.lower() == "yes":
                return key.capitalize()
            return f"{key.capitalize()}: {valid_type}"

    # 4. Fallback to basic identifier properties
    admin_candidates = ("admin", "ADMIN", "id", "ID")
    for key in admin_candidates:
        valid_id = clean_val(key)
        if valid_id:
            return valid_id

    return fallback

def _truncate_utf8_bytes(name, max_bytes):
    """Truncate to Blender's object-name limit, which is 63 UTF-8 BYTES, not
    characters - a name with accented/CJK/Cyrillic characters can still be
    too long after character-based truncation. A byte-level slice can land
    mid-character; errors="ignore" drops whatever incomplete trailing bytes
    that leaves instead of raising. Same fix as user_scripts/csv_menu_select
    .py's _truncate_utf8, replicated here rather than imported since that
    module lives in a differently-loaded registry (user_scripts/).
    """
    encoded = name.encode("utf-8")
    if len(encoded) <= max_bytes:
        return name
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _safe_object_name(text, fallback="Feature"):
    value = str(text).strip().replace(": ", "_")
    if not value:
        value = fallback

    cleaned = []
    for ch in value:
        if ch.isalnum() or ch in {"_", "-", " ", "."}:
            cleaned.append(ch)
        else:
            cleaned.append("_")

    name = "".join(cleaned).strip()
    if not name:
        name = fallback
    return _truncate_utf8_bytes(name, 63)

def _clear_collection_recursive(collection):
    for child in list(collection.children):
        _clear_collection_recursive(child)
        bpy.data.collections.remove(child)

    for obj in list(collection.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


def _geojson_structure_diagnostics(data):
    if isinstance(data, dict):
        top_type = data.get("type", "<missing>")
        keys = sorted(str(key) for key in data.keys())
        keys_preview = ", ".join(keys[:8])
        if len(keys) > 8:
            keys_preview += ", ..."

        details = [f"top-level type={top_type!r}", f"keys=[{keys_preview}]"]

        if top_type == "FeatureCollection":
            features_value = data.get("features")
            if isinstance(features_value, list):
                details.append(f"feature_count={len(features_value)}")
                if features_value and isinstance(features_value[0], dict):
                    first_geom = features_value[0].get("geometry")
                    if isinstance(first_geom, dict):
                        details.append(
                            "first_feature_geometry="
                            f"{first_geom.get('type', '<missing>')!r}"
                        )
            else:
                details.append(f"features_type={type(features_value).__name__}")
        elif top_type == "Feature":
            geom = data.get("geometry")
            if isinstance(geom, dict):
                details.append(f"feature_geometry={geom.get('type', '<missing>')!r}")
            else:
                details.append(f"geometry_type={type(geom).__name__}")
        elif top_type == "Topology":
            details.append(
                "looks_like_topojson=True (convert TopoJSON to GeoJSON first)"
            )

        return "; ".join(details)

    return f"top-level JSON value is {type(data).__name__}, expected object/dict"


def _load_geojson_feature_payload_from_data(data):
    payload = []

    def _append_feature(geometry, properties, fallback_name):
        if not isinstance(geometry, dict):
            return

        lines = [line for line in _iter_geojson_line_coords(geometry) if len(line) >= 2]
        points = [point for point in _iter_geojson_point_coords(geometry)]

        if not lines and not points:
            return

        payload.append(
            {
                "name": _feature_name_from_properties(properties, fallback_name),
                "properties": properties if isinstance(properties, dict) else {},
                "lines": lines,
                "points": points,
            }
        )

    diagnostics = _geojson_structure_diagnostics(data)

    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        for idx, feature in enumerate(data.get("features", []), start=1):
            if not isinstance(feature, dict):
                continue
            _append_feature(
                feature.get("geometry"),
                feature.get("properties"),
                f"Feature_{idx}",
            )
        return payload

    if isinstance(data, dict) and data.get("type") == "Feature":
        _append_feature(data.get("geometry"), data.get("properties"), "Feature_1")
        return payload

    if isinstance(data, dict) and isinstance(data.get("type"), str):
        _append_feature(data, {}, "Geometry_1")
        return payload

    raise RuntimeError(
        "Unsupported GeoJSON structure. "
        f"{diagnostics}. Accepted top-level types: FeatureCollection, "
        "Feature, or a geometry object (Point/MultiPoint/LineString/"
        "MultiLineString/Polygon/MultiPolygon/GeometryCollection)."
    )


def _load_geojson_features(file_path):
    with open(file_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return _load_geojson_feature_payload_from_data(data)


def _create_boundary_wire_object_from_lines(
    collection,
    obj_name,
    line_coords,
    globe_radius,
    kind,
    geo_name,
    continent_name,
    iso2,
    iso3,
    m49="",
    max_segment_deg=0.0,
    spherical_tolerance_deg=0.0,
    coord_settings=None,
    latlon_bounds=None,
    globe_root=None,
):
    mesh = _boundary_wire_mesh(
        obj_name,
        line_coords,
        globe_radius,
        m49=m49,
        max_segment_deg=max_segment_deg,
        spherical_tolerance_deg=spherical_tolerance_deg,
        coord_settings=coord_settings,
        latlon_bounds=latlon_bounds,
    )
    if mesh is None:
        return None

    obj = bpy.data.objects.new(obj_name, mesh)
    obj.display_type = "WIRE"
    _set_geo_identity(obj, kind, geo_name, continent_name, iso2, iso3, m49)
    collection.objects.link(obj)
    _parent_to_globe_root(obj, globe_root)
    return obj


def _set_geo_identity(obj, kind, geo_name, continent_name, iso2, iso3, m49=""):
    obj["geo_kind"] = kind
    obj["geo_name"] = geo_name
    obj["geo_continent"] = continent_name
    obj["geo_iso2"] = iso2
    obj["geo_iso3"] = iso3
    obj["geo_m49"] = m49


def _boundary_wire_mesh(
    obj_name,
    line_coords,
    globe_radius,
    m49="",
    max_segment_deg=0.0,
    spherical_tolerance_deg=0.0,
    coord_settings=None,
    latlon_bounds=None,
):
    """Wire mesh (vertices + edges, no faces) tracing line_coords on the
    globe or flat map, or None if nothing drawable remains."""
    vertices = []
    edges = []
    offset = 0

    for coords in line_coords:
        local_points = _line_coords_to_simplified_xyz(
            coords,
            globe_radius,
            max_segment_deg=max_segment_deg,
            spherical_tolerance_deg=spherical_tolerance_deg,
            coord_settings=coord_settings,
            latlon_bounds=latlon_bounds,
        )
        if len(local_points) < 2:
            continue

        vertices.extend(local_points)
        for i in range(len(local_points) - 1):
            edges.append((offset + i, offset + i + 1))
        offset += len(local_points)

    if not vertices or not edges:
        return None

    mesh = bpy.data.meshes.new(f"{obj_name}_Mesh")
    mesh.from_pydata(vertices, edges, [])
    mesh.update()

    # Point-domain copy of geo_m49, so Geometry Nodes can read it via
    # Object Info -> Geometry -> Named Attribute("geo_m49") given a picked
    # object - custom (ID) properties like the one set below aren't
    # reachable from inside a node tree, only real geometry attributes are.
    m49_attr = mesh.attributes.new(name="geo_m49", type="INT", domain="POINT")
    m49_attr.data.foreach_set("value", [int(m49) if m49 else -1] * len(vertices))
    return mesh


def _create_curve_outline_object_from_lines(
    collection,
    obj_name,
    line_coords,
    globe_radius,
    kind,
    geo_name,
    continent_name,
    iso2,
    iso3,
    m49="",
    max_segment_deg=0.0,
    spherical_tolerance_deg=0.0,
    spline_type="NURBS",
    coord_settings=None,
    latlon_bounds=None,
    globe_root=None,
):
    curve_data = bpy.data.curves.new(f"{obj_name}_Curve", type="CURVE")
    curve_data.dimensions = "3D"
    # Blender versions differ on valid fill_mode enum values.
    try:
        fill_enum = curve_data.bl_rna.properties["fill_mode"].enum_items.keys()
        if "NONE" in fill_enum:
            curve_data.fill_mode = "NONE"
        elif "FULL" in fill_enum:
            curve_data.fill_mode = "FULL"
    except Exception:
        pass
    curve_data.resolution_u = 2

    created_any = False

    for coords in line_coords:
        points = _line_coords_to_simplified_xyz(
            coords,
            globe_radius,
            max_segment_deg=max_segment_deg,
            spherical_tolerance_deg=spherical_tolerance_deg,
            coord_settings=coord_settings,
            latlon_bounds=latlon_bounds,
        )
        if len(points) < 2:
            continue

        created_any = True
        if spline_type == "BEZIER":
            spline = curve_data.splines.new("BEZIER")
            spline.bezier_points.add(len(points) - 1)
            for idx, point in enumerate(points):
                bp = spline.bezier_points[idx]
                bp.co = point
                bp.handle_left_type = "AUTO"
                bp.handle_right_type = "AUTO"
        else:
            subtype = "POLY" if spline_type == "POLY" else "NURBS"
            spline = curve_data.splines.new(subtype)
            spline.points.add(len(points) - 1)
            for idx, point in enumerate(points):
                spline.points[idx].co = (point[0], point[1], point[2], 1.0)

            if subtype == "NURBS":
                spline.use_endpoint_u = True
                if len(points) >= 4:
                    spline.order_u = min(4, len(points))

    if not created_any:
        bpy.data.curves.remove(curve_data, do_unlink=True)
        return None

    obj = bpy.data.objects.new(obj_name, curve_data)
    obj["geo_kind"] = kind
    obj["geo_name"] = geo_name
    obj["geo_continent"] = continent_name
    obj["geo_iso2"] = iso2
    obj["geo_iso3"] = iso3
    obj["geo_m49"] = m49
    collection.objects.link(obj)
    _parent_to_globe_root(obj, globe_root)
    return obj


def _coord_to_lonlat(coord):
    try:
        lon, lat, *_ = coord
        return (float(lon), float(lat))
    except Exception:
        return None


def _scene_location_from_lonlat(lat_deg, lon_deg, coord_settings, bounds=None, radius=None):
    return project_point(lat_deg, lon_deg, 0.0, coord_settings, bounds, radius=radius)


def _create_geojson_point_object(
    collection,
    obj_name,
    location,
    mode,
    sphere_mesh,
    geo_name,
    globe_root=None,
):
    if mode == "EMPTY":
        obj = bpy.data.objects.new(obj_name, None)
        obj.empty_display_type = "SPHERE"
        obj.empty_display_size = 0.2
    else:
        obj = bpy.data.objects.new(obj_name, sphere_mesh)
        obj.display_type = "SOLID"

    obj.location = location
    obj["geo_kind"] = "geojson_point"
    obj["geo_name"] = geo_name
    obj["geo_continent"] = ""
    obj["geo_iso2"] = ""
    obj["geo_iso3"] = ""
    collection.objects.link(obj)
    _parent_to_globe_root(obj, globe_root)
    return obj


_INT32_MIN = -(2 ** 31)
_INT32_MAX = 2 ** 31 - 1


def _safe_custom_property_value(value):
    """Blender custom (ID) properties store a plain Python `int` as a
    32-bit C int - assigning one outside that range raises OverflowError.
    Real-world trigger: US Census GeoJSON county polygons carry an
    'ALAND'/'AWATER' land/water-area-in-square-meters property that's
    genuinely a Python int (JSON numbers with no decimal point parse as
    int, unlike raw OSM XML tags which are always strings) and routinely
    exceeds 2**31-1 for larger counties (e.g. Baldwin County, AL:
    ALAND=4117656199).

    Only ints need this - a Python float assigns to a full C double, with
    no equivalent range problem. Falling back to str() keeps the value
    exactly, with no precision loss (unlike coercing to float, which
    starts silently losing precision above 2**53) - at the cost of it no
    longer being numeric. In-range ints/floats/strings/bools pass through
    unchanged.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        if value < _INT32_MIN or value > _INT32_MAX:
            return str(value)
    return value


def _import_geojson_feature_payload(
    context,
    settings,
    payload,
    source_label,
    clear_existing=None,
    existing_data_mode=None,
):
    root = _get_or_create_collection(context.scene.collection, "Geo Wireframes")
    imports_coll = _get_child_collection(root, "GeoJSON Imports")
    if imports_coll is None:
        imports_coll = _get_or_create_collection(root, "GeoJSON Imports")

    if clear_existing is None:
        clear_existing = settings.clear_existing
    if existing_data_mode is None:
        existing_data_mode = settings.geojson_existing_data_mode

    if clear_existing:
        if existing_data_mode == "DELETE":
            _clear_collection_recursive(imports_coll)
        else:
            if imports_coll.objects or imports_coll.children:
                _archive_geojson_imports(root, imports_coll)
            imports_coll = _get_or_create_collection(root, "GeoJSON Imports")

    coord_settings = context.scene.geo_coord_settings
    overlay_root = _ensure_globe_overlay_root_empty(context)
    overlay_radius = coord_settings.globe_radius
    line_created = 0
    point_created = 0
    unknown_type_count = 0
    latlon_bounds = _bounds_from_geojson_payload(payload) if coord_settings.flat_fit_to_bbox else None

    sphere_mesh = None
    if settings.geojson_point_mode == "SPHERE":
        point_radius = max(
            coord_settings.globe_radius * settings.geojson_point_scale,
            0.00005,
        )
        sphere_mesh = _build_sphere_mesh(
            "GeoJSONPointPrototypeMesh",
            point_radius,
            max(6, settings.geojson_point_resolution),
            add_surface=True,
        )
        proto_obj = bpy.data.objects.new("GeoJSONPointPrototype", sphere_mesh)
        proto_obj.hide_viewport = True
        proto_obj.hide_render = True
        imports_coll.objects.link(proto_obj)

    print(f"[Geo Wireframes] Converting {len(payload)} GeoJSON feature(s) from '{source_label}'...")
    for index, feature in enumerate(payload, start=1):
        feature_name = str(feature.get("name", f"Feature_{index}")).strip()
        properties = {key: value for key, value in (feature.get("properties", {}) or {}).items() if value is not None}

        if "building" in properties:
            type_label = "Buildings"
        elif "highway" in properties or "way" in properties:
            type_label = "Ways"
        elif properties.get("amenity") == "tree" or "natural" in properties:
            type_label = "Vegetation"
        elif "landuse" in properties:
            type_label = "Landuse"
        elif "amenity" in properties:
            type_label = "Amenities"
        elif "tourism" in properties:
            type_label = "Tourism"
        elif "type" in properties:
            type_label = properties["type"]  # Fallback to a custom feature 'type' if defined
        else:
            type_label = "GeoJSON_unknown"  # Ultimate fallback to geometry style (e.g., 'Points', 'Polygons')
            # Printed in full only once as a diagnostic sample - a dataset
            # with no OSM-style tags at all (e.g. US Census county
            # polygons, which carry FID/GISJOIN/ALAND/... instead) would
            # otherwise dump every single feature's properties dict here,
            # one print per feature (thousands, for a large file).
            if unknown_type_count == 0:
                print(f"[Geo Wireframes] Untyped feature properties (showing first only): {properties}")
            unknown_type_count += 1

        if not feature_name:
            feature_name = f"Feature_{index}"
        object_suffix = _safe_object_name(
            feature_name,
            fallback=f"Feature_{index}",
        )

        if settings.geojson_split_collections:
            feature_coll = _get_or_create_collection(imports_coll, type_label)
        else:
            feature_coll = imports_coll

        source_lines = feature.get("lines", [])
        if settings.geojson_simplify_tolerance_deg > 0.0 and source_lines:
            source_lines = _simplify_lines(
                source_lines,
                settings.geojson_simplify_tolerance_deg,
            )

        if source_lines:
            if settings.geojson_output_mode == "CURVE":
                line_obj = _create_curve_outline_object_from_lines(
                    collection=feature_coll,
                    obj_name=f"GeoJSON_{object_suffix}",
                    line_coords=source_lines,
                    globe_radius=overlay_radius,
                    kind="geojson",
                    geo_name=feature_name,
                    continent_name="",
                    iso2="",
                    iso3="",
                    max_segment_deg=settings.geojson_curve_step_deg,
                    spherical_tolerance_deg=(settings.geojson_spherical_tolerance_deg),
                    spline_type=settings.geojson_curve_spline_type,
                    coord_settings=coord_settings,
                    latlon_bounds=latlon_bounds,
                    globe_root=overlay_root,
                )
            else:
                line_obj = _create_boundary_wire_object_from_lines(
                    collection=feature_coll,
                    obj_name=f"GeoJSON_{object_suffix}",
                    line_coords=source_lines,
                    globe_radius=overlay_radius,
                    kind="geojson",
                    geo_name=feature_name,
                    continent_name="",
                    iso2="",
                    iso3="",
                    max_segment_deg=settings.geojson_curve_step_deg,
                    spherical_tolerance_deg=(settings.geojson_spherical_tolerance_deg),
                    coord_settings=coord_settings,
                    latlon_bounds=latlon_bounds,
                    globe_root=overlay_root,
                )

            if line_obj is not None:
                line_obj["geo_source_file"] = source_label
                line_obj["geo_feature_index"] = index
                line_created += 1
                for key, value in sorted(properties.items()):
                    if value is not None:
                        line_obj[f"osm:{key}"] = _safe_custom_property_value(value)

        if settings.geojson_point_mode == "IGNORE":
            continue

        point_coords = feature.get("points", [])
        for point_index, coord in enumerate(point_coords, start=1):
            lonlat = _coord_to_lonlat(coord)
            if lonlat is None:
                continue

            lon, lat = lonlat
            location = _scene_location_from_lonlat(
                lat,
                lon,
                coord_settings,
                bounds=latlon_bounds,
                radius=overlay_radius,
            )
            point_name = f"GeoJSONPoint_{object_suffix}_{point_index:04d}"
            point_obj = _create_geojson_point_object(
                collection=feature_coll,
                obj_name=point_name,
                location=location,
                mode=settings.geojson_point_mode,
                sphere_mesh=sphere_mesh,
                geo_name=feature_name,
                globe_root=overlay_root,
            )
            point_obj["geo_source_file"] = source_label
            point_obj["geo_feature_index"] = index
            point_obj["geo_point_index"] = point_index
            point_obj["geo_lon"] = lon
            point_obj["geo_lat"] = lat
            for key, value in properties.items():
                if value is not None:
                    point_obj[f"osm:{key}"] = _safe_custom_property_value(value)
            point_created += 1

    if unknown_type_count:
        print(
            f"[Geo Wireframes] {unknown_type_count} feature(s) had no "
            f"recognized type tag - categorized as 'GeoJSON_unknown'."
        )
    print(
        f"[Geo Wireframes] Done: {line_created} line feature(s), "
        f"{point_created} point marker(s) created."
    )
    return line_created, point_created


def _parse_osm_bbox(raw_text):
    tokens = [token.strip() for token in str(raw_text).split(",") if token.strip()]
    if len(tokens) != 4:
        raise RuntimeError("OSM bbox must have 4 comma-separated numbers.")

    south, west, north, east = [float(token) for token in tokens]
    if south >= north:
        raise RuntimeError("OSM bbox south must be < north.")
    if west >= east:
        raise RuntimeError("OSM bbox west must be < east.")
    return (south, west, north, east)


def _osm_preflight_messages(settings):
    """Thin settings-object adapter around the shared preflight check in
    _osm_safety_shared - see that module for the actual threshold logic,
    which flat_gis_importer.py's OSM importer also uses.
    """
    south, west, north, east = _parse_osm_bbox(settings.osm_bbox)
    area_deg2 = _osm_safety_shared.bbox_area_deg2(south, west, north, east)

    return _osm_safety_shared.osm_preflight_messages(
        area_deg2,
        settings.osm_warn_bbox_area_deg2,
        settings.osm_hard_bbox_area_deg2,
        settings.osm_max_features,
        settings.osm_query_mode == "CUSTOM",
        settings.osm_timeout_seconds,
    )


def _parse_osm_tag_filters(raw_text):
    tokens = _tokenize_values(raw_text)
    parsed = []
    for token in tokens:
        if "=" not in token:
            parsed.append((token.strip(), None))
            continue
        key, value = token.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        parsed.append((key, value if value else None))
    return parsed


def _osm_tags_dict_from_filters(raw_text):
    tags = {}
    for key, value in _parse_osm_tag_filters(raw_text):
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


def _osm_presets_map():
    return {
        "BUILDINGS": "building",
        "ROADS": "highway",
        "WATER": "waterway,natural=water",
        "LANDUSE": "landuse,natural",
        "POI": "amenity,shop,tourism",
        "BOUNDARIES": "boundary=administrative",
        "ALL": "building,highway,waterway,natural=water,landuse,natural,amenity,shop,tourism,boundary=administrative",
    }


def _fetch_osm_geojson_with_osmnx(settings):
    try:
        import osmnx as ox
    except Exception as exc:
        raise RuntimeError(
            "Could not import osmnx. Install it in the Blender uv environment "
            "to enable preset/tag-based OSM import."
        ) from exc

    south, west, north, east = _parse_osm_bbox(settings.osm_bbox)
    print(
        f"[Geo Wireframes] Fetching OSM features via osmnx: "
        f"south={south}, west={west}, north={north}, east={east}"
    )
    tags = _osm_tags_dict_from_filters(settings.osm_tag_filters)
    if not tags:
        tags = {"building": True}

    timeout_s = max(5, int(settings.osm_timeout_seconds))
    try:
        ox.settings.requests_timeout = timeout_s
    except Exception:
        pass

    gdf = None

    fetch_attempts = [
        lambda: ox.features.features_from_bbox((west, south, east, north), tags),
        # lambda: ox.features_from_bbox(north, south, east, west, tags),
        # lambda: ox.features.features_from_bbox((north, south, east, west), tags),
    ]

    fetch_error = None
    for attempt in fetch_attempts:
        try:
            gdf = attempt()
            break
        except Exception as exc:
            fetch_error = exc

    if gdf is None:
        raise RuntimeError(f"osmnx fetch failed: {fetch_error}") from fetch_error

    print(f"[Geo Wireframes] osmnx fetch complete: {len(gdf)} feature(s).")

    if gdf.empty:
        return {
            "type": "FeatureCollection",
            "features": [],
        }

    max_features = int(settings.osm_max_features)
    if max_features > 0 and len(gdf) > max_features:
        gdf = gdf.head(max_features)

    tolerance = float(settings.osm_simplify_tolerance_deg)
    if tolerance > 0.0:
        gdf = gdf.copy()
        gdf.geometry = gdf.geometry.simplify(
            tolerance,
            preserve_topology=True,
        )
        gdf = gdf[gdf.geometry.notnull()]

    return json.loads(gdf.to_json())


def _build_overpass_query_from_settings(settings):
    if settings.osm_query_mode == "CUSTOM":
        custom = str(settings.osm_custom_query).strip()
        if not custom:
            raise RuntimeError("OSM custom query is empty.")
        return custom

    south, west, north, east = _parse_osm_bbox(settings.osm_bbox)
    bbox = f"({south},{west},{north},{east})"
    filters = _parse_osm_tag_filters(settings.osm_tag_filters)

    selector_lines = []
    if not filters:
        selector_lines.append(f"  nwr{bbox};")
    else:
        for key, value in filters:
            if value is None:
                selector_lines.append(f'  nwr["{key}"]{bbox};')
            else:
                selector_lines.append(f'  nwr["{key}"="{value}"]{bbox};')

    timeout_s = max(5, int(settings.osm_timeout_seconds))
    query = [f"[out:json][timeout:{timeout_s}];", "("]
    query.extend(selector_lines)
    query.extend([")", "out body geom;"])
    return "\n".join(query)


def _fetch_overpass_json(endpoint, query, timeout_seconds, max_response_bytes):
    print(f"[Geo Wireframes] Fetching OSM data from Overpass endpoint {endpoint} ...")
    encoded = urllib.parse.urlencode({"data": query}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=encoded,
        headers={"User-Agent": "BlenderGeoWireframes/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            total = 0
            chunks = []
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)

                if total > max_response_bytes:
                    raise RuntimeError(
                        "Overpass response exceeded safety limit. "
                        "Narrow bbox/tags or lower Max Features."
                    )

            content = b"".join(chunks).decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
        raise RuntimeError(
            f"Overpass request failed with HTTP {exc.code}. {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Overpass request failed: {exc}") from exc

    print(f"[Geo Wireframes] Overpass fetch complete ({total} bytes).")

    try:
        return json.loads(content)
    except Exception as exc:
        raise RuntimeError(f"Overpass returned invalid JSON: {exc}") from exc


def _convert_overpass_to_geojson(overpass_json):
    try:
        import osm2geojson
    except Exception as exc:
        raise RuntimeError(
            "Could not import osm2geojson. Install it in the Blender uv "
            "environment to enable OSM conversion."
        ) from exc

    elements = overpass_json.get("elements", []) if isinstance(overpass_json, dict) else []
    print(f"[Geo Wireframes] Converting {len(elements)} OSM element(s) to GeoJSON...")
    try:
        geojson = osm2geojson.json2geojson(overpass_json)
    except Exception as exc:
        raise RuntimeError(f"Failed converting OSM JSON to GeoJSON: {exc}") from exc
    print(f"[Geo Wireframes] Conversion complete: {len(geojson.get('features', []))} GeoJSON feature(s).")
    return geojson


def _download_url_to_temp_file(url, timeout_seconds, max_bytes):
    print(f"[Geo Wireframes] Downloading imagery from {url} ...")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "BlenderGeoWireframes/1.0"},
    )

    fd, temp_path = tempfile.mkstemp(prefix="geo_download_", suffix=".img")
    os.close(fd)

    written = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            with open(temp_path, "wb") as handle:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise RuntimeError(
                            "Remote image exceeds configured download limit "
                            f"({max_bytes // (1024 * 1024)} MB)."
                        )
                    handle.write(chunk)
    except Exception:
        try:
            os.remove(temp_path)
        except Exception:
            pass
        raise

    print(f"[Geo Wireframes] Download complete ({written / (1024 * 1024):.1f} MB).")
    return temp_path


def _local_file_size_mb(path):
    try:
        size = os.path.getsize(path)
    except Exception:
        return 0.0
    return float(size) / (1024.0 * 1024.0)


def _raster_dimensions(path):
    try:
        import rasterio
    except Exception:
        return None

    try:
        with rasterio.open(path) as src:
            return (int(src.width), int(src.height))
    except Exception:
        return None


def _create_reference_globe(collection, radius, resolution, globe_root=None):
    mesh = _build_sphere_mesh(
        "GeoReferenceGlobeMesh",
        radius,
        max(8, resolution),
        add_surface=True,
    )
    obj = bpy.data.objects.new("GeoReferenceGlobe", mesh)
    obj.display_type = "SOLID"
    obj["geo_kind"] = "reference_globe"
    obj["geo_name"] = "reference_globe"
    obj["geo_continent"] = ""
    obj["geo_iso2"] = ""
    obj["geo_iso3"] = ""
    obj["geo_m49"] = ""
    for poly in mesh.polygons:
        poly.use_smooth = True
    collection.objects.link(obj)
    _parent_to_globe_root(obj, globe_root)
    return obj


def _find_reference_globe_object():
    direct = bpy.data.objects.get("GeoReferenceGlobe")
    if direct is not None:
        return direct
    for obj in bpy.data.objects:
        if obj.name.startswith("GeoReferenceGlobe"):
            return obj
    return None


def _ensure_reference_globe(context, settings):
    globe_root = _ensure_globe_root_empty(context)

    existing = _find_reference_globe_object()
    if existing is not None:
        _parent_to_globe_root(existing, globe_root)
        return existing

    coord_settings = context.scene.geo_coord_settings
    root = _get_or_create_collection(context.scene.collection, "Geo Wireframes")
    reference_coll = _get_or_create_collection(root, "Geo Reference")
    return _create_reference_globe(
        collection=reference_coll,
        radius=coord_settings.globe_radius,
        resolution=settings.resolution,
        globe_root=globe_root,
    )


def _normalize_image_array(array):
    try:
        import numpy as np
    except Exception as exc:
        raise RuntimeError("Could not import numpy for raster conversion.") from exc

    image = array.astype("float32", copy=False)
    if image.ndim == 2:
        image = np.stack([image, image, image], axis=-1)

    if image.ndim == 3 and image.shape[-1] > 3:
        image = image[:, :, :3]

    finite_mask = np.isfinite(image)
    if not finite_mask.any():
        raise RuntimeError("Raster image contains no finite values.")

    valid = image[finite_mask]
    lo = float(np.percentile(valid, 2.0))
    hi = float(np.percentile(valid, 98.0))
    if hi <= lo:
        lo = float(valid.min())
        hi = float(valid.max())
        if hi <= lo:
            hi = lo + 1.0

    image = (image - lo) / (hi - lo)
    image = np.clip(image, 0.0, 1.0)
    image[~finite_mask] = 0.0
    return image


def _convert_raster_to_png(source_path, max_size=4096):
    print(f"[Geo Wireframes] Converting raster imagery to PNG: {source_path} ...")
    try:
        import numpy as np
        import rasterio
        from rasterio.enums import Resampling
    except Exception as exc:
        raise RuntimeError("Could not import rasterio for imagery conversion.") from exc

    with rasterio.open(source_path) as src:
        if src.count <= 0:
            raise RuntimeError("Raster has no bands.")

        scale = 1.0
        if max(src.width, src.height) > max_size:
            scale = max_size / float(max(src.width, src.height))

        out_h = max(1, int(src.height * scale))
        out_w = max(1, int(src.width * scale))
        band_count = min(3, src.count)
        data = src.read(
            indexes=list(range(1, band_count + 1)),
            out_shape=(band_count, out_h, out_w),
            resampling=Resampling.bilinear,
        )

    image = np.moveaxis(data, 0, -1)
    image = _normalize_image_array(image)

    fd, target_path = tempfile.mkstemp(prefix="geo_texture_", suffix=".png")
    os.close(fd)

    try:
        import matplotlib.pyplot as plt

        plt.imsave(target_path, image)
    except Exception as exc:
        raise RuntimeError(f"Could not save converted raster image: {exc}") from exc

    print(f"[Geo Wireframes] Raster conversion complete: {target_path}")
    return target_path


def _socket_by_name_and_type(sockets, name, socket_type):
    """Like _socket_by_name, but also requires an exact socket.type match.

    Needed for the generic "Mix" shader node: it carries Float/Vector/Color
    variants of "A"/"B"/"Factor"/"Result" simultaneously, all sharing the
    same display name - normally distinguished by only the variant matching
    the node's data_type being left un-hidden (.hide == False). That's what
    an earlier version of this helper matched on, but it's not reliable: a
    real bug on Blender 5.3 showed the wrong (Float) "B" socket getting
    matched immediately after setting cloud_mix.data_type = "RGBA" in the
    same script run - the .hide flags apparently hadn't been refreshed to
    reflect the new data_type yet at that point, an update-timing dependency
    this code shouldn't need to reason about at all. Filtering by the
    socket's actual type instead sidesteps that entirely - it doesn't matter
    whether Blender has gotten around to updating .hide yet, since RGBA vs
    VALUE vs VECTOR is intrinsic to the socket itself, not a UI-state flag.
    """
    for socket in sockets:
        if socket.name == name and socket.type == socket_type:
            return socket
    return None


def _ensure_texture_material(
    context,
    material_name,
    image_path,
    clouds_image_path=None,
    animate_clouds=False,
    cloud_rotation_period_frames=500.0,
):
    image = bpy.data.images.load(image_path, check_existing=True)
    material = bpy.data.materials.get(material_name)
    if material is None:
        material = bpy.data.materials.new(material_name)

    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()

    uv_map = nodes.new("ShaderNodeUVMap")
    uv_map.uv_map = "GeoUV"
    uv_map.location = (-900, 0)

    image_tex = nodes.new("ShaderNodeTexImage")
    image_tex.image = image
    image_tex.interpolation = "Smart"
    image_tex.location = (-620, 200)

    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (-120, 0)

    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (160, 0)

    uv_out = _socket_by_name(uv_map.outputs, "UV")
    tex_vec = _socket_by_name(image_tex.inputs, "Vector")
    base_color_socket = _socket_by_name(image_tex.outputs, "Color")
    bsdf_base = _socket_by_name(bsdf.inputs, "Base Color")
    bsdf_out = _socket_by_name(bsdf.outputs, "BSDF")
    out_surface = _socket_by_name(output.inputs, "Surface")

    if uv_out is not None and tex_vec is not None:
        links.new(uv_out, tex_vec)

    if clouds_image_path:
        clouds_image = bpy.data.images.load(clouds_image_path, check_existing=True)

        # Own Mapping node (not a direct link to uv_map) so its rotation can
        # be driven independently of the land texture underneath - that's
        # the entire "clouds move" effect: a static cloud texture, slowly
        # rotated, layered over an otherwise-static planet.
        cloud_mapping = nodes.new("ShaderNodeMapping")
        cloud_mapping.location = (-620, -280)
        cloud_uv_in = _socket_by_name(cloud_mapping.inputs, "Vector")
        cloud_uv_out = _socket_by_name(cloud_mapping.outputs, "Vector")
        if uv_out is not None and cloud_uv_in is not None:
            links.new(uv_out, cloud_uv_in)

        if animate_clouds:
            rotation_input = _socket_by_name(cloud_mapping.inputs, "Rotation")
            period = max(1.0, float(cloud_rotation_period_frames))
            fcurve = rotation_input.driver_add("default_value", 2)
            driver = fcurve.driver
            driver.type = "SCRIPTED"
            var = driver.variables.new()
            var.name = "frame"
            var.type = "SINGLE_PROP"
            var.targets[0].id_type = "SCENE"
            var.targets[0].id = context.scene
            var.targets[0].data_path = "frame_current"
            driver.expression = f"frame * (2 * pi / {period})"

        cloud_tex = nodes.new("ShaderNodeTexImage")
        cloud_tex.image = clouds_image
        cloud_tex.interpolation = "Smart"
        cloud_tex.location = (-380, -280)
        cloud_tex_vec = _socket_by_name(cloud_tex.inputs, "Vector")
        cloud_tex_color = _socket_by_name(cloud_tex.outputs, "Color")
        if cloud_uv_out is not None and cloud_tex_vec is not None:
            links.new(cloud_uv_out, cloud_tex_vec)

        cloud_factor_node = nodes.new("ShaderNodeRGBToBW")
        cloud_factor_node.location = (-150, -280)
        cloud_factor_in = _socket_by_name(cloud_factor_node.inputs, "Color")
        cloud_factor_out = _socket_by_name(cloud_factor_node.outputs, "Val")
        if cloud_tex_color is not None and cloud_factor_in is not None:
            links.new(cloud_tex_color, cloud_factor_in)

        # cloud_combined_2048.jpg has no alpha channel (black = clear sky,
        # white = cloud) - its own brightness (via RGBToBW above) is the
        # mix factor, not transparency.
        cloud_mix = nodes.new("ShaderNodeMix")
        cloud_mix.data_type = "RGBA"
        cloud_mix.location = (100, -140)
        mix_fac = _socket_by_name_and_type(cloud_mix.inputs, "Factor", "VALUE")
        mix_a = _socket_by_name_and_type(cloud_mix.inputs, "A", "RGBA")
        mix_b = _socket_by_name_and_type(cloud_mix.inputs, "B", "RGBA")
        mix_result = _socket_by_name_and_type(cloud_mix.outputs, "Result", "RGBA")
        if mix_b is not None:
            mix_b.default_value = (0.92, 0.92, 0.94, 1.0)
        if cloud_factor_out is not None and mix_fac is not None:
            links.new(cloud_factor_out, mix_fac)
        if base_color_socket is not None and mix_a is not None:
            links.new(base_color_socket, mix_a)

        base_color_socket = mix_result

    if base_color_socket is not None and bsdf_base is not None:
        links.new(base_color_socket, bsdf_base)
    if bsdf_out is not None and out_surface is not None:
        links.new(bsdf_out, out_surface)

    return material


def _apply_texture_to_reference_globe(
    context,
    settings,
    image_path,
    clouds_image_path=None,
    animate_clouds=False,
    cloud_rotation_period_frames=500.0,
):
    globe = _ensure_reference_globe(context, settings)
    material = _ensure_texture_material(
        context,
        "GeoReferenceGlobeMaterial",
        image_path,
        clouds_image_path=clouds_image_path,
        animate_clouds=animate_clouds,
        cloud_rotation_period_frames=cloud_rotation_period_frames,
    )

    if globe.data is None:
        raise RuntimeError("Reference globe has no mesh data.")

    if globe.data.materials:
        globe.data.materials[0] = material
    else:
        globe.data.materials.append(material)

    return globe


def _sync_country_items_from_world(settings, world):
    selected_by_name = {item.name: item.selected for item in settings.country_items}
    settings.country_items.clear()

    seen = set()
    sorted_world = world.sort_values(by="name")
    for _, row in sorted_world.iterrows():
        name = str(row["name"]).strip()
        if not name:
            continue

        lowered = name.lower()
        if lowered in seen:
            continue
        seen.add(lowered)

        item = settings.country_items.add()
        item.name = name
        item.iso2 = str(row["iso_a2"]).upper()
        item.iso3 = str(row["iso_a3"]).upper()
        item.continent = str(row["continent"])
        item.selected = bool(selected_by_name.get(name, False))


def _reconcile_geo_markers(collection, prefix, items, remove_stale, globe_root=None):
    """Create, update or remove one marker object per item in `collection`.

    Uses d3blender's data().join(), keyed by the item's geo name, so re-running
    Create Markers updates existing objects in place: anything pointing at a
    country object (a CMET generator's Country A/B, a Geometry Nodes Object
    input, a linked project) keeps working, and modifiers added by hand stay.
    Each item supplies "name", "mesh" (a boundary wire mesh, or the shared
    prototype sphere in marker mode), "location" and the geo identity fields.
    remove_stale deletes markers whose name isn't in `items`; otherwise
    they're left alone, so runs with different selections accumulate.

    Objects made before this used joins carry no d3_key; ones still named
    exactly <prefix><name> are adopted. Returns the names of leftover
    <prefix><name>.NNN duplicates from those older runs, which are left as is.
    """
    import re

    from d3blender import select
    from d3blender.blender_adapter import get_adapter

    duplicates = []
    for obj in collection.objects:
        if "d3_key" in obj or not obj.name.startswith(prefix):
            continue
        if re.search(r"\.\d{3}$", obj.name):
            duplicates.append(obj.name)
        else:
            obj["d3_key"] = obj.name[len(prefix):]

    def enter(item):
        obj = bpy.data.objects.new(f"{prefix}{item['name']}", item["mesh"])
        collection.objects.link(obj)
        return obj

    def remove(obj):
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)

    joined = select(get_adapter(), list(collection.objects)).data(
        items, key=lambda i, item: item["name"]
    ).join(enter, exit=remove if remove_stale else (lambda obj: None))

    for obj, item in zip(joined.elements, joined.datum):
        old_mesh = obj.data
        obj.data = item["mesh"]
        if old_mesh is not None and old_mesh is not item["mesh"] and old_mesh.users == 0:
            bpy.data.meshes.remove(old_mesh)
        obj.location = item["location"]
        obj.display_type = "WIRE"
        _set_geo_identity(
            obj,
            item["kind"],
            item["name"],
            item["continent"],
            item["iso2"],
            item["iso3"],
            item["m49"],
        )
        _parent_to_globe_root(obj, globe_root)
    return duplicates


def _write_anchor_attributes(anchor_obj, items):
    mesh = anchor_obj.data
    if len(mesh.vertices) != len(items):
        return

    try:
        attr_kind = mesh.attributes.get("geo_kind")
        if attr_kind is None:
            attr_kind = mesh.attributes.new("geo_kind", "STRING", "POINT")

        attr_name = mesh.attributes.get("geo_name")
        if attr_name is None:
            attr_name = mesh.attributes.new("geo_name", "STRING", "POINT")

        attr_cont = mesh.attributes.get("geo_continent")
        if attr_cont is None:
            attr_cont = mesh.attributes.new("geo_continent", "STRING", "POINT")

        attr_iso2 = mesh.attributes.get("geo_iso2")
        if attr_iso2 is None:
            attr_iso2 = mesh.attributes.new("geo_iso2", "STRING", "POINT")

        attr_iso3 = mesh.attributes.get("geo_iso3")
        if attr_iso3 is None:
            attr_iso3 = mesh.attributes.new("geo_iso3", "STRING", "POINT")

        attr_m49 = mesh.attributes.get("geo_m49")
        if attr_m49 is None:
            attr_m49 = mesh.attributes.new("geo_m49", "STRING", "POINT")

        attr_index = mesh.attributes.get("geo_index")
        if attr_index is None:
            attr_index = mesh.attributes.new("geo_index", "INT", "POINT")

        attr_uv = mesh.attributes.get("geo_uv")
        if attr_uv is None:
            attr_uv = mesh.attributes.new("geo_uv", "FLOAT2", "POINT")
    except Exception as exc:
        print(f"[Geo Wireframes] Could not create anchor attributes: {exc}")
        return

    def _as_attr_bytes(value):
        if isinstance(value, bytes):
            return value
        return str(value).encode("utf-8")

    for i, item in enumerate(items):
        attr_kind.data[i].value = _as_attr_bytes(item["kind"])
        attr_name.data[i].value = _as_attr_bytes(item["name"])
        attr_cont.data[i].value = _as_attr_bytes(item["continent"])
        attr_iso2.data[i].value = _as_attr_bytes(item["iso2"])
        attr_iso3.data[i].value = _as_attr_bytes(item["iso3"])
        attr_m49.data[i].value = _as_attr_bytes(item["m49"])
        attr_index.data[i].value = i
        attr_uv.data[i].vector = item["uv"]


def _create_anchor_object(context, name, points, items, collection, globe_root=None):
    mesh = bpy.data.meshes.new(f"{name}_Mesh")
    mesh.from_pydata(points, [], [])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj.hide_render = True
    obj.display_type = "BOUNDS"
    obj["geo_description"] = (
        "Use this point mesh in Geometry Nodes; it stores string IDs and "
        "UV-like "
        "lat/lon coordinates in attributes."
    )
    collection.objects.link(obj)
    _parent_to_globe_root(obj, globe_root)
    _write_anchor_attributes(obj, items)
    return obj


def _socket_by_name(sockets, name):
    for socket in sockets:
        if socket.name == name:
            return socket
    return None


def _clear_group_interface(node_group):
    if not hasattr(node_group, "interface"):
        if hasattr(node_group, "inputs"):
            while node_group.inputs:
                node_group.inputs.remove(node_group.inputs[-1])
        if hasattr(node_group, "outputs"):
            while node_group.outputs:
                node_group.outputs.remove(node_group.outputs[-1])
        return

    items = list(node_group.interface.items_tree)
    for item in items:
        try:
            node_group.interface.remove(item)
        except Exception:
            pass


def _create_group_socket(node_group, name, in_out, socket_type):
    if hasattr(node_group, "interface"):
        return node_group.interface.new_socket(
            name=name,
            in_out=in_out,
            socket_type=socket_type,
        )

    if in_out == "INPUT":
        return node_group.inputs.new(socket_type, name)
    return node_group.outputs.new(socket_type, name)


def _set_group_input_default(node_group, socket_name, value):
    if hasattr(node_group, "interface"):
        for item in node_group.interface.items_tree:
            if getattr(item, "item_type", None) != "SOCKET":
                continue
            if item.in_out != "INPUT" or item.name != socket_name:
                continue
            try:
                item.default_value = value
            except Exception:
                pass
            return

    if hasattr(node_group, "inputs"):
        socket = node_group.inputs.get(socket_name)
        if socket is None:
            return
        try:
            socket.default_value = value
        except Exception:
            pass


def _ensure_geo_source_group(anchor_object):
    group_name = "Geo Source From Anchors"
    group = bpy.data.node_groups.get(group_name)
    if group is None or group.bl_idname != "GeometryNodeTree":
        group = bpy.data.node_groups.new(group_name, "GeometryNodeTree")

    group.nodes.clear()
    group.links.clear()
    _clear_group_interface(group)

    _create_group_socket(group, "Anchor Object", "INPUT", "NodeSocketObject")
    _create_group_socket(group, "As Instance", "INPUT", "NodeSocketBool")
    _create_group_socket(group, "Geometry", "OUTPUT", "NodeSocketGeometry")

    _set_group_input_default(group, "Anchor Object", anchor_object)
    _set_group_input_default(group, "As Instance", True)

    nodes = group.nodes
    links = group.links

    group_in = nodes.new("NodeGroupInput")
    group_in.location = (-760, 0)

    obj_info = nodes.new("GeometryNodeObjectInfo")
    obj_info.location = (-420, 0)

    group_out = nodes.new("NodeGroupOutput")
    group_out.location = (-80, 0)

    in_anchor = _socket_by_name(group_in.outputs, "Anchor Object")
    in_instance = _socket_by_name(group_in.outputs, "As Instance")
    info_object = _socket_by_name(obj_info.inputs, "Object")
    info_instance = _socket_by_name(obj_info.inputs, "As Instance")
    info_geo = _socket_by_name(obj_info.outputs, "Geometry")
    out_geo = _socket_by_name(group_out.inputs, "Geometry")

    if in_anchor is not None and info_object is not None:
        links.new(in_anchor, info_object)
    if in_instance is not None and info_instance is not None:
        links.new(in_instance, info_instance)
    if info_geo is not None and out_geo is not None:
        links.new(info_geo, out_geo)

    frame = nodes.new("NodeFrame")
    frame.label = "Named attributes on incoming points"
    frame.location = (-420, -380)

    attrs = [
        ("geo_kind", "STRING"),
        ("geo_name", "STRING"),
        ("geo_continent", "STRING"),
        ("geo_iso2", "STRING"),
        ("geo_iso3", "STRING"),
        ("geo_uv", "FLOAT_VECTOR"),
        ("geo_index", "INT"),
    ]
    y = -420
    for attr_name, data_type in attrs:
        node = nodes.new("GeometryNodeInputNamedAttribute")
        node.label = attr_name
        node.location = (-760, y)
        node.parent = frame
        if hasattr(node, "data_type"):
            try:
                node.data_type = data_type
            except Exception:
                pass
        name_input = _socket_by_name(node.inputs, "Name")
        if name_input is not None:
            name_input.default_value = attr_name
        y -= 160

    return group


def _ensure_geo_source_host(context):
    host = bpy.data.objects.get("GeoSourceHost")
    if host is not None:
        return host

    mesh = bpy.data.meshes.new("GeoSourceHostMesh")
    mesh.from_pydata([], [], [])
    mesh.update()
    host = bpy.data.objects.new("GeoSourceHost", mesh)
    context.scene.collection.objects.link(host)
    return host


def _find_geo_anchor_object():
    anchor = bpy.data.objects.get("GeoAnchorPoints")
    if anchor is not None:
        return anchor

    for obj in bpy.data.objects:
        if obj.name.startswith("GeoAnchorPoints"):
            return obj
    return None


def _apply_node_group_input_defaults(modifier, node_group, values):
    if not hasattr(node_group, "interface"):
        return

    for item in node_group.interface.items_tree:
        if getattr(item, "item_type", None) != "SOCKET":
            continue
        if item.in_out != "INPUT":
            continue
        value = values.get(item.name)
        if value is None:
            continue
        # Blender 5.2+ keeps GN modifier inputs in modifier.properties.inputs
        # (each a property group holding "value"); modifier[identifier] = value
        # raises TypeError there, which used to be swallowed here silently.
        properties = getattr(modifier, "properties", None)
        try:
            if properties is not None:
                properties.inputs[item.identifier]["value"] = value
            else:
                modifier[item.identifier] = value
        except Exception as exc:
            print(f"[Geo] Couldn't set input '{item.name}' on {modifier.name}: {exc}")


class GeoCountryListItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Name", default="")
    iso2: bpy.props.StringProperty(name="ISO2", default="")
    iso3: bpy.props.StringProperty(name="ISO3", default="")
    continent: bpy.props.StringProperty(name="Continent", default="")
    selected: bpy.props.BoolProperty(name="Selected", default=False)


class VIEW3D_UL_geo_country_list(bpy.types.UIList):
    def draw_item(
        self,
        _context,
        layout,
        _data,
        item,
        _icon,
        _active_data,
        _active_propname,
        _index,
    ):
        row = layout.row(align=True)
        row.prop(item, "selected", text="")
        row.label(text=item.name)
        row.label(text=item.continent)


class OBJECT_OT_refresh_geo_country_list(bpy.types.Operator):
    bl_idname = "object.refresh_geo_country_list"
    bl_label = "Refresh Countries"
    bl_description = "Load countries into the selectable list"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.country_wireframe_settings
        try:
            world, _country_lookup, _continents_lookup = _load_geo_data(
                scale=settings.world_dataset_scale,
            )
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        _sync_country_items_from_world(settings, world)
        self.report({"INFO"}, f"Loaded {len(settings.country_items)} countries.")
        return {"FINISHED"}


class OBJECT_OT_select_all_geo_countries(bpy.types.Operator):
    bl_idname = "object.select_all_geo_countries"
    bl_label = "Select All Countries"
    bl_description = "Select all countries in the list"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.country_wireframe_settings
        for item in settings.country_items:
            item.selected = True
        self.report({"INFO"}, "Selected all countries.")
        return {"FINISHED"}


class OBJECT_OT_select_none_geo_countries(bpy.types.Operator):
    bl_idname = "object.select_none_geo_countries"
    bl_label = "Select No Countries"
    bl_description = "Unselect all countries in the list"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.country_wireframe_settings
        for item in settings.country_items:
            item.selected = False
        self.report({"INFO"}, "Cleared country selection.")
        return {"FINISHED"}


class CountryWireframeSettings(bpy.types.PropertyGroup):
    generation_mode: bpy.props.EnumProperty(
        name="Mode",
        description="Generate true boundary wireframes or marker spheres",
        items=(
            ("BOUNDARY", "Boundary Wireframes", "Country/continent outlines"),
            ("MARKER", "Marker Spheres", "Centroid markers as spheres"),
        ),
        default="BOUNDARY",
    )
    clear_existing: bpy.props.BoolProperty(
        name="Clear Existing",
        description=(
            "Remove previously generated geo objects first. Country/continent "
            "markers that are still selected are updated in place instead, so "
            "references to them keep working; deselected ones are removed"
        ),
        default=True,
    )
    countries_enabled: bpy.props.BoolProperty(
        name="Countries",
        description="Generate country instances",
        default=True,
    )
    continents_enabled: bpy.props.BoolProperty(
        name="Continents",
        description="Generate continent instances",
        default=True,
    )
    continents: bpy.props.StringProperty(
        name="Continents",
        description=(
            "Optional comma-separated continent names. "
            "Leave empty to include all continents."
        ),
        default="",
    )
    world_dataset_scale: bpy.props.EnumProperty(
        name="Coastline Detail",
        description=(
            "Natural Earth admin-0 boundary dataset to load countries/"
            "continents from. Affects both the country list (Refresh) and "
            "Create Country Wireframes - re-run Refresh after changing it"
        ),
        items=(
            (
                "110M",
                "Coarse (1:110m)",
                "Fastest, smallest; heavily simplified coastlines meant for "
                "whole-globe views. Will visibly cut across bays, fjords, "
                "and small islands compared to Blue Marble imagery.",
            ),
            (
                "50M",
                "Medium (1:50m)",
                "Moderate detail and download/vertex count.",
            ),
            (
                "10M",
                "Fine (1:10m)",
                "Closest coastline match to Blue Marble imagery; largest "
                "download and vertex count, slower to generate.",
            ),
        ),
        default="110M",
    )
    resolution: bpy.props.IntProperty(
        name="Resolution",
        description="Segments for the generated UV spheres",
        default=24,
        min=6,
        max=256,
    )
    marker_scale: bpy.props.FloatProperty(
        name="Country Scale",
        description="Country sphere size relative to globe radius",
        default=0.03,
        min=0.0001,
        max=1.0,
        subtype="FACTOR",
    )
    continent_scale: bpy.props.FloatProperty(
        name="Continent Scale",
        description="Continent sphere size relative to globe radius",
        default=0.06,
        min=0.0001,
        max=1.0,
        subtype="FACTOR",
    )
    add_surface_mesh: bpy.props.BoolProperty(
        name="Mesh Surface + UV",
        description=(
            "If enabled, instances keep visible faces and GeoUVs for shading; "
            "otherwise they are wireframe-only"
        ),
        default=True,
    )
    create_reference_globe: bpy.props.BoolProperty(
        name="Reference Globe",
        description="Create a reference globe under the overlays",
        default=True,
    )
    geojson_curve_step_deg: bpy.props.FloatProperty(
        name="Curve Step (deg)",
        description=(
            "Max spherical arc segment for imported GeoJSON lines. "
            "Smaller values better preserve curvature on coarse datasets"
        ),
        default=2.0,
        min=0.1,
        max=30.0,
        step=1,
        precision=2,
    )
    geojson_simplify_tolerance_deg: bpy.props.FloatProperty(
        name="Simplify Tol (deg)",
        description=(
            "Reduce dense GeoJSON vertices before projection. "
            "Set 0 to disable simplification"
        ),
        default=0.0,
        min=0.0,
        max=5.0,
        step=0.1,
        precision=3,
    )
    geojson_output_mode: bpy.props.EnumProperty(
        name="Import As",
        description="Create imported outlines as mesh edges or curve objects",
        items=(
            ("MESH", "Mesh Wire", "Mesh edges suitable for conversion/editing"),
            ("CURVE", "Curve Outline", "3D curve outlines on the sphere"),
        ),
        default="CURVE",
    )
    geojson_point_mode: bpy.props.EnumProperty(
        name="Point Layers",
        description="How to import GeoJSON Point and MultiPoint geometries",
        items=(
            ("IGNORE", "Ignore", "Skip point and multipoint geometries"),
            ("EMPTY", "Empties", "Create empty marker objects for points"),
            ("SPHERE", "Spheres", "Create small sphere markers for points"),
        ),
        default="SPHERE",
    )
    geojson_point_scale: bpy.props.FloatProperty(
        name="Point Scale",
        description="Point sphere size relative to globe radius",
        default=0.004,
        min=0.00001,
        max=0.25,
        subtype="FACTOR",
    )
    geojson_point_resolution: bpy.props.IntProperty(
        name="Point Resolution",
        description="Segments for point sphere markers",
        default=10,
        min=4,
        max=128,
    )
    geojson_curve_spline_type: bpy.props.EnumProperty(
        name="Curve Type",
        description="Spline type for curve outline import",
        items=(
            ("NURBS", "NURBS", "Smooth parametric curves"),
            ("BEZIER", "Bezier", "Bezier splines with auto handles"),
            ("POLY", "Poly", "Polyline curve matching sampled points"),
        ),
        default="POLY",
    )
    geojson_spherical_tolerance_deg: bpy.props.FloatProperty(
        name="Sphere Tol (deg)",
        description=(
            "Post-projection simplification on the sphere using angular error. "
            "Set 0 to disable"
        ),
        default=0.0,
        min=0.0,
        max=5.0,
        step=0.1,
        precision=3,
    )
    geojson_split_collections: bpy.props.BoolProperty(
        name="Split By Feature",
        description="Create one child collection per imported GeoJSON feature",
        default=True,
    )
    geojson_existing_data_mode: bpy.props.EnumProperty(
        name="When Clearing",
        description="How to handle existing GeoJSON imports when Clear Existing is on",
        items=(
            ("ARCHIVE", "Archive", "Move current imports into GeoJSON Archive"),
            (
                "DELETE",
                "Delete",
                "Delete existing imports recursively",
            ),
        ),
        default="ARCHIVE",
    )
    geojson_clear_existing: bpy.props.BoolProperty(
        name="Clear Existing",
        description="Remove previously imported GeoJSON data first",
        default=True,
    )
    osm_clear_existing: bpy.props.BoolProperty(
        name="Clear Existing",
        description="Remove previously imported OSM data first",
        default=True,
    )
    osm_existing_data_mode: bpy.props.EnumProperty(
        name="When Clearing",
        description="How to handle existing OSM imports when Clear Existing is on",
        items=(
            ("ARCHIVE", "Archive", "Move current imports into GeoJSON Archive"),
            ("DELETE", "Delete", "Delete existing imports recursively"),
        ),
        default="ARCHIVE",
    )
    osm_query_mode: bpy.props.EnumProperty(
        name="OSM Query",
        description=(
            "Use osmnx with bbox/tags or paste raw Overpass QL " "for advanced queries"
        ),
        items=(
            (
                "BBOX_TAGS",
                "BBox + Tags (osmnx)",
                "Use osmnx/geopandas for OSM features",
            ),
            (
                "CUSTOM",
                "Custom Overpass",
                "Use raw Overpass QL (fallback path)",
            ),
        ),
        default="BBOX_TAGS",
    )
    osm_preset: bpy.props.EnumProperty(
        name="Preset",
        description="Populate common OSM tag filters",
        items=(
            ("BUILDINGS", "Buildings", "Building footprints and parts"),
            ("ROADS", "Roads", "Road network and paths"),
            ("WATER", "Water", "Waterways and water bodies"),
            ("LANDUSE", "Landuse", "Landuse and natural areas"),
            ("POI", "POIs", "Amenities, shops, and tourism points"),
            (
                "BOUNDARIES",
                "Boundaries",
                "Administrative and political boundaries",
            ),
            ("ALL", "All", "All other presets"),
        ),
        default="ALL",
    )
    osm_endpoint: bpy.props.StringProperty(
        name="Overpass Endpoint",
        description="Overpass API interpreter endpoint",
        default="https://overpass-api.de/api/interpreter",
    )
    osm_timeout_seconds: bpy.props.IntProperty(
        name="Timeout (s)",
        description="Network timeout for Overpass request",
        default=45,
        min=5,
        max=600,
    )
    osm_require_confirmation: bpy.props.BoolProperty(
        name="Confirm Large OSM Requests",
        description="Prompt before potentially large OSM imports",
        default=True,
    )
    osm_bbox: bpy.props.StringProperty(
        name="BBox",
        description="south,west,north,east in WGS84 degrees",
        default="40.70,-74.03,40.75,-73.96",
    )
    osm_warn_bbox_area_deg2: bpy.props.FloatProperty(
        name="Warn Area (deg^2)",
        description="Show confirmation when bbox area exceeds this threshold",
        default=0.25,
        min=0.001,
        max=100.0,
        precision=3,
    )
    osm_hard_bbox_area_deg2: bpy.props.FloatProperty(
        name="Hard Area Limit (deg^2)",
        description="Abort imports above this bbox area",
        default=2.0,
        min=0.01,
        max=1000.0,
        precision=3,
    )
    osm_tag_filters: bpy.props.StringProperty(
        name="Tag Filters",
        description="Comma-separated tags, e.g. building, highway=primary",
        default="building",
    )
    osm_max_features: bpy.props.IntProperty(
        name="Max Features",
        description="Hard cap on imported OSM features (0 disables cap)",
        default=2000,
        min=0,
        max=200000,
    )
    osm_simplify_tolerance_deg: bpy.props.FloatProperty(
        name="Simplify Tol (deg)",
        description="Pre-simplify OSM geometries in lon/lat before projection",
        default=0.0004,
        min=0.0,
        max=1.0,
        step=0.1,
        precision=6,
    )
    osm_max_response_mb: bpy.props.IntProperty(
        name="Max Response (MB)",
        description="Abort if Overpass response exceeds this size",
        default=64,
        min=5,
        max=1024,
    )
    osm_custom_query: bpy.props.StringProperty(
        name="Overpass QL",
        description="Custom Overpass query (must include out clause)",
        default=(
            "[out:json][timeout:45];\n"
            "(\n"
            '  nwr["building"](40.70,-74.03,40.75,-73.96);\n'
            ");\n"
            "out body geom;"
        ),
    )
    imagery_quality: bpy.props.EnumProperty(
        name="Blue Marble Quality",
        description="Which NASA Blue Marble resolution to fetch",
        items=[
            (
                "STANDARD",
                "Standard (5400x2700, ~2.6 MB)",
                "Good for a 4K render with Earth taking up a good fraction of "
                "the frame, without excessive texture memory (~44 MB uncompressed)",
            ),
            (
                "FULL",
                "Full Resolution (21600x10800, ~30 MB)",
                "Maximum published detail - only worth it for close zoom-ins; "
                "~700 MB uncompressed in VRAM",
            ),
            (
                "CUSTOM",
                "Custom URL",
                "Use the 'Blue Marble URL' field below instead",
            ),
        ],
        default="STANDARD",
    )
    imagery_blue_marble_url: bpy.props.StringProperty(
        name="Blue Marble URL",
        description="Open equirectangular Earth image URL or file URL (only used when Quality is 'Custom URL')",
        default=_blue_marble_fallback_url(),
    )
    imagery_use_clouds: bpy.props.BoolProperty(
        name="Add Cloud Layer",
        description="Blend NASA's cloud-layer texture over the Blue Marble base texture",
        default=False,
    )
    imagery_clouds_url: bpy.props.StringProperty(
        name="Clouds URL",
        description="Equirectangular cloud-layer image URL (black=clear, white=cloud)",
        default=_BLUE_MARBLE_CLOUDS_URL,
    )
    imagery_animate_clouds: bpy.props.BoolProperty(
        name="Animate Clouds",
        description="Slowly rotate the cloud layer independently of the land texture, driven by the current frame",
        default=False,
    )
    imagery_cloud_rotation_period_frames: bpy.props.FloatProperty(
        name="Cloud Rotation Period (frames)",
        description="Frames for the cloud layer to complete one full rotation",
        default=500.0,
        min=1.0,
        max=100000.0,
    )
    imagery_max_size: bpy.props.IntProperty(
        name="Imagery Max Size",
        description="Maximum pixel dimension when converting raster imagery",
        default=4096,
        min=512,
        max=16384,
    )
    imagery_require_confirmation: bpy.props.BoolProperty(
        name="Confirm Remote Imagery Downloads",
        description="Prompt before downloading remote imagery",
        default=True,
    )
    imagery_max_download_mb: bpy.props.IntProperty(
        name="Max Remote Download (MB)",
        description="Abort if remote imagery exceeds this size",
        default=64,
        min=1,
        max=2048,
    )
    imagery_max_local_file_mb: bpy.props.IntProperty(
        name="Max Local File (MB)",
        description="Abort local imagery import if file exceeds this size",
        default=2048,
        min=10,
        max=65536,
    )
    imagery_max_source_pixels: bpy.props.IntProperty(
        name="Max Source Pixels",
        description="Abort if source raster dimensions exceed this many pixels",
        default=50000000,
        min=1000000,
        max=1000000000,
    )
    country_items: bpy.props.CollectionProperty(type=GeoCountryListItem)
    country_index: bpy.props.IntProperty(default=0)


class OBJECT_OT_import_geojson_wireframes(bpy.types.Operator, ImportHelper):
    bl_idname = "object.import_geojson_wireframes"
    bl_label = "Import GeoJSON Wireframes"
    bl_description = "Import GeoJSON boundaries and project them onto the globe"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".geojson"
    filter_glob: bpy.props.StringProperty(
        default="*.geojson;*.json",
        options={"HIDDEN"},
    )

    def execute(self, context):
        settings = context.scene.country_wireframe_settings

        try:
            payload = _load_geojson_features(self.filepath)
        except Exception as exc:
            print(f"[Geo Wireframes][GeoJSON Diagnostic] {exc}")
            self.report({"ERROR"}, f"Could not read GeoJSON: {exc}")
            return {"CANCELLED"}

        if not payload:
            self.report(
                {"ERROR"},
                "No importable GeoJSON features found (lines, polygons, or points).",
            )
            return {"CANCELLED"}

        lines_created, points_created = _import_geojson_feature_payload(
            context,
            settings,
            payload,
            self.filepath,
            clear_existing=settings.geojson_clear_existing,
            existing_data_mode=settings.geojson_existing_data_mode,
        )

        if lines_created == 0 and points_created == 0:
            self.report(
                {"ERROR"},
                "No valid geometries were created from GeoJSON.",
            )
            return {"CANCELLED"}

        self.report(
            {"INFO"},
            (
                f"Imported {lines_created} line feature(s) and "
                f"{points_created} point marker(s)."
            ),
        )
        _collapse_outliner_view()
        return {"FINISHED"}


class OBJECT_OT_apply_osm_preset(bpy.types.Operator):
    bl_idname = "object.apply_osm_preset"
    bl_label = "Apply OSM Preset"
    bl_description = "Fill OSM tag filters from preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.country_wireframe_settings
        mapping = _osm_presets_map()
        tag_filter = mapping.get(settings.osm_preset, "building")
        settings.osm_tag_filters = tag_filter
        self.report({"INFO"}, f"OSM preset applied: {tag_filter}")
        return {"FINISHED"}


class OBJECT_OT_import_osm_overpass(bpy.types.Operator):
    bl_idname = "object.import_osm_overpass"
    bl_label = "Import OSM (Overpass)"
    bl_description = "Fetch OSM data through Overpass, convert to GeoJSON, and import"
    bl_options = {"REGISTER", "UNDO"}

    preflight_warning: bpy.props.StringProperty(default="", options={"HIDDEN"})

    def invoke(self, context, _event):
        settings = context.scene.country_wireframe_settings
        try:
            warnings = _osm_preflight_messages(settings)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        if settings.osm_require_confirmation and warnings:
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
        settings = context.scene.country_wireframe_settings

        try:
            if settings.osm_query_mode == "CUSTOM":
                endpoint = str(settings.osm_endpoint).strip()
                if not endpoint:
                    raise RuntimeError("OSM endpoint is empty.")

                query = _build_overpass_query_from_settings(settings)
                overpass_json = _fetch_overpass_json(
                    endpoint,
                    query,
                    max(5, int(settings.osm_timeout_seconds)),
                    max(1, int(settings.osm_max_response_mb)) * 1024 * 1024,
                )
                geojson = _convert_overpass_to_geojson(overpass_json)
                source_label = f"overpass:{endpoint}"
            else:
                geojson = _fetch_osm_geojson_with_osmnx(settings)
                source_label = "osmnx"

            payload = _load_geojson_feature_payload_from_data(geojson)
        except Exception as exc:
            print(f"[Geo Wireframes][OSM Diagnostic] {exc}")
            self.report({"ERROR"}, f"OSM import failed: {exc}")
            return {"CANCELLED"}

        if not payload:
            self.report({"WARNING"}, "OSM query returned no importable geometry.")
            return {"CANCELLED"}

        lines_created, points_created = _import_geojson_feature_payload(
            context,
            settings,
            payload,
            source_label,
            clear_existing=settings.osm_clear_existing,
            existing_data_mode=settings.osm_existing_data_mode,
        )

        self.report(
            {"INFO"},
            (
                f"OSM import complete: {lines_created} line feature(s), "
                f"{points_created} point marker(s)."
            ),
        )
        _collapse_outliner_view()
        return {"FINISHED"}


class OBJECT_OT_apply_blue_marble_texture(bpy.types.Operator):
    bl_idname = "object.apply_blue_marble_texture"
    bl_label = "Apply Blue Marble"
    bl_description = "Download and apply NASA Blue Marble texture to reference globe"
    bl_options = {"REGISTER", "UNDO"}

    preflight_warning: bpy.props.StringProperty(default="", options={"HIDDEN"})

    def invoke(self, context, _event):
        settings = context.scene.country_wireframe_settings
        if settings.imagery_quality == "CUSTOM":
            try:
                url = _resolve_blue_marble_url(settings.imagery_blue_marble_url)
            except Exception as exc:
                self.report({"ERROR"}, f"Blue Marble texture source is unavailable: {exc}")
                return {"CANCELLED"}
            settings.imagery_blue_marble_url = url

        if settings.imagery_require_confirmation:
            extra = " plus a cloud layer" if settings.imagery_use_clouds else ""
            self.preflight_warning = (
                f"This downloads a remote image{extra} into Blender's main process. "
                f"Max allowed download is {int(settings.imagery_max_download_mb)} MB per file."
            )
            return context.window_manager.invoke_props_dialog(self, width=560)

        self.preflight_warning = ""
        return self.execute(context)

    def draw(self, _context):
        layout = self.layout
        layout.label(text="Remote imagery download", icon="INFO")
        layout.label(text=self.preflight_warning)
        layout.separator()
        layout.label(text="Press OK to continue, or Cancel to adjust limits.")

    def execute(self, context):
        settings = context.scene.country_wireframe_settings
        try:
            url = _resolve_blue_marble_quality_url(
                settings.imagery_quality, settings.imagery_blue_marble_url
            )
        except Exception as exc:
            self.report({"ERROR"}, f"Blue Marble texture source is unavailable: {exc}")
            return {"CANCELLED"}

        if settings.imagery_quality == "CUSTOM":
            settings.imagery_blue_marble_url = url

        max_bytes = max(1, int(settings.imagery_max_download_mb)) * 1024 * 1024
        timeout = max(5, int(settings.osm_timeout_seconds))

        try:
            temp_path = _download_url_to_temp_file(url, timeout_seconds=timeout, max_bytes=max_bytes)

            clouds_path = None
            if settings.imagery_use_clouds:
                clouds_url = str(settings.imagery_clouds_url).strip() or _BLUE_MARBLE_CLOUDS_URL
                clouds_path = _download_url_to_temp_file(
                    clouds_url, timeout_seconds=timeout, max_bytes=max_bytes
                )

            globe = _apply_texture_to_reference_globe(
                context,
                settings,
                temp_path,
                clouds_image_path=clouds_path,
                animate_clouds=settings.imagery_animate_clouds,
                cloud_rotation_period_frames=settings.imagery_cloud_rotation_period_frames,
            )
        except Exception as exc:
            self.report({"ERROR"}, f"Failed applying Blue Marble texture: {exc}")
            return {"CANCELLED"}

        cloud_note = " with cloud layer" if clouds_path else ""
        self.report({"INFO"}, f"Blue Marble texture{cloud_note} applied to '{globe.name}'.")
        return {"FINISHED"}


class OBJECT_OT_apply_texture_from_raster(bpy.types.Operator, ImportHelper):
    bl_idname = "object.apply_texture_from_raster"
    bl_label = "Apply Texture From Raster"
    bl_description = "Convert local raster/image and apply as globe texture"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".tif"
    filter_glob: bpy.props.StringProperty(
        default="*.tif;*.tiff;*.png;*.jpg;*.jpeg;*.webp",
        options={"HIDDEN"},
    )

    def execute(self, context):
        settings = context.scene.country_wireframe_settings
        source_path = self.filepath
        if not source_path:
            self.report({"ERROR"}, "No image/raster file selected.")
            return {"CANCELLED"}

        size_mb = _local_file_size_mb(source_path)
        if size_mb > float(settings.imagery_max_local_file_mb):
            self.report(
                {"ERROR"},
                (
                    "Source file is too large: "
                    f"{size_mb:.1f} MB > {int(settings.imagery_max_local_file_mb)} MB"
                ),
            )
            return {"CANCELLED"}

        lower = source_path.lower()
        try:
            if lower.endswith((".tif", ".tiff")):
                dims = _raster_dimensions(source_path)
                if dims is not None:
                    width, height = dims
                    total_pixels = width * height
                    if total_pixels > int(settings.imagery_max_source_pixels):
                        self.report(
                            {"ERROR"},
                            (
                                "Source raster is too large: "
                                f"{total_pixels} pixels exceeds "
                                f"{int(settings.imagery_max_source_pixels)}"
                            ),
                        )
                        return {"CANCELLED"}

                texture_path = _convert_raster_to_png(
                    source_path,
                    max_size=max(512, int(settings.imagery_max_size)),
                )
            else:
                texture_path = source_path

            globe = _apply_texture_to_reference_globe(
                context,
                settings,
                texture_path,
            )
        except Exception as exc:
            self.report({"ERROR"}, f"Failed applying texture: {exc}")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Texture applied to '{globe.name}'.")
        return {"FINISHED"}


class OBJECT_OT_create_country_wireframes(bpy.types.Operator):
    bl_idname = "object.create_country_wireframes"
    bl_label = "Create Country Wireframes"
    bl_description = (
        "Create country/continent marker instances with identifying attributes"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.country_wireframe_settings
        coord_settings = context.scene.geo_coord_settings

        if not settings.countries_enabled and not settings.continents_enabled:
            self.report(
                {"ERROR"},
                "Enable Countries and/or Continents before creating markers.",
            )
            return {"CANCELLED"}

        country_tokens = [item.name for item in settings.country_items if item.selected]
        continent_tokens = _tokenize_values(settings.continents)

        if settings.countries_enabled and not country_tokens:
            self.report(
                {"ERROR"},
                "Select one or more countries in the list.",
            )
            return {"CANCELLED"}

        try:
            _world, country_lookup, continents_lookup = _load_geo_data(
                scale=settings.world_dataset_scale,
            )
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        m49_lookup = _m49_lookup_by_iso3()

        country_radius = max(coord_settings.globe_radius * settings.marker_scale, 0.0001)
        continent_radius = max(
            coord_settings.globe_radius * settings.continent_scale,
            0.0001,
        )

        selected_country_rows = []
        for token in country_tokens:
            row = country_lookup.get(token.lower())
            if row is not None:
                selected_country_rows.append(row)

        selected_continents = set(name.lower() for name in continent_tokens if name.strip())
        selected_continent_items = []
        for item in continents_lookup.values():
            if selected_continents and item["name"].lower() not in selected_continents:
                continue
            selected_continent_items.append(item)

        latlon_bounds = None
        if coord_settings.coordinate_space == "FLAT" and coord_settings.flat_fit_to_bbox:
            for row in selected_country_rows:
                latlon_bounds = _merge_bounds(latlon_bounds, _bounds_from_geometry(row.geometry))
            for item in selected_continent_items:
                latlon_bounds = _merge_bounds(latlon_bounds, _bounds_from_geometry(item.get("geometry")))

        root = _get_or_create_collection(
            context.scene.collection,
            "Geo Wireframes",
        )
        prototypes = _get_or_create_collection(root, "Geo Prototypes")
        countries_coll = _get_or_create_collection(root, "Geo Countries")
        continents_coll = _get_or_create_collection(root, "Geo Continents")
        anchors_coll = _get_or_create_collection(root, "Geo Anchors")
        reference_coll = _get_or_create_collection(root, "Geo Reference")

        if settings.clear_existing:
            # Country/continent markers aren't wiped: _reconcile_geo_markers
            # updates them in place and removes only the deselected ones.
            collections = (
                prototypes,
                anchors_coll,
                reference_coll,
            )
            for coll in collections:
                for obj in list(coll.objects):
                    bpy.data.objects.remove(obj, do_unlink=True)

        globe_root = _ensure_globe_root_empty(context)
        overlay_root = _ensure_globe_overlay_root_empty(context)

        if settings.create_reference_globe:
            _create_reference_globe(
                collection=reference_coll,
                radius=coord_settings.globe_radius,
                resolution=settings.resolution,
                globe_root=globe_root,
            )

        overlay_radius = coord_settings.globe_radius

        country_proto = None
        continent_proto = None
        if settings.generation_mode == "MARKER":
            country_proto = _build_sphere_mesh(
                "GeoCountryPrototypeMesh",
                country_radius,
                settings.resolution,
                settings.add_surface_mesh,
            )
            country_proto_obj = bpy.data.objects.new(
                "GeoCountryPrototype",
                country_proto,
            )
            country_proto_obj.hide_viewport = True
            country_proto_obj.hide_render = True
            prototypes.objects.link(country_proto_obj)

            continent_proto = _build_sphere_mesh(
                "GeoContinentPrototypeMesh",
                continent_radius,
                settings.resolution,
                settings.add_surface_mesh,
            )
            continent_proto_obj = bpy.data.objects.new(
                "GeoContinentPrototype",
                continent_proto,
            )
            continent_proto_obj.hide_viewport = True
            continent_proto_obj.hide_render = True
            prototypes.objects.link(continent_proto_obj)

        created = 0
        missing = []
        anchor_points = []
        anchor_items = []
        country_markers = []
        continent_markers = []
        boundary_mode = settings.generation_mode == "BOUNDARY"

        if settings.countries_enabled:
            for token in country_tokens:
                row = country_lookup.get(token.lower())
                if row is None:
                    missing.append(token)
                    continue

                point = row.geometry.representative_point()
                lat = point.y
                lon = point.x
                location = _scene_location_from_lonlat(
                    lat,
                    lon,
                    coord_settings,
                    bounds=latlon_bounds,
                    radius=overlay_radius,
                )
                name = str(row["name"])
                continent_name = str(row["continent"])
                iso2 = str(row["iso_a2"])
                iso3 = str(row["iso_a3"])
                m49 = m49_lookup.get(iso3.upper(), "")

                if boundary_mode:
                    mesh = _boundary_wire_mesh(
                        f"CountryWF_{name}",
                        _iter_line_coords(row.geometry),
                        overlay_radius,
                        m49=m49,
                        coord_settings=coord_settings,
                        latlon_bounds=latlon_bounds,
                    )
                    if mesh is None:
                        missing.append(token)
                        continue
                else:
                    mesh = country_proto
                country_markers.append(
                    {
                        "name": name,
                        "mesh": mesh,
                        "location": (0.0, 0.0, 0.0) if boundary_mode else location,
                        "kind": "country",
                        "continent": continent_name,
                        "iso2": iso2,
                        "iso3": iso3,
                        "m49": m49,
                    }
                )

                anchor_points.append(location)
                anchor_items.append(
                    {
                        "kind": "country",
                        "name": name,
                        "continent": continent_name,
                        "iso2": iso2,
                        "iso3": iso3,
                        "m49": m49,
                        "uv": _to_uv(lat, lon),
                    }
                )
                created += 1

        if settings.continents_enabled:
            for item in selected_continent_items:
                continent_name = item["name"]
                point = item["point"]
                lat = point.y
                lon = point.x
                location = _scene_location_from_lonlat(
                    lat,
                    lon,
                    coord_settings,
                    bounds=latlon_bounds,
                    radius=overlay_radius,
                )

                if boundary_mode:
                    mesh = _boundary_wire_mesh(
                        f"ContinentWF_{continent_name}",
                        _iter_line_coords(item.get("geometry")),
                        overlay_radius,
                        coord_settings=coord_settings,
                        latlon_bounds=latlon_bounds,
                    )
                    if mesh is None:
                        continue
                else:
                    mesh = continent_proto
                continent_markers.append(
                    {
                        "name": continent_name,
                        "mesh": mesh,
                        "location": (0.0, 0.0, 0.0) if boundary_mode else location,
                        "kind": "continent",
                        "continent": continent_name,
                        "iso2": "",
                        "iso3": "",
                        "m49": "",
                    }
                )

                anchor_points.append(location)
                anchor_items.append(
                    {
                        "kind": "continent",
                        "name": continent_name,
                        "continent": continent_name,
                        "iso2": "",
                        "iso3": "",
                        "m49": "",
                        "uv": _to_uv(lat, lon),
                    }
                )
                created += 1

        duplicates = []
        for coll, prefix, markers in (
            (countries_coll, "CountryWF_", country_markers),
            (continents_coll, "ContinentWF_", continent_markers),
        ):
            # A disabled kind reconciles against no items: cleared when Clear
            # Existing is on, left alone otherwise (as before).
            duplicates += _reconcile_geo_markers(
                coll, prefix, markers, settings.clear_existing, overlay_root
            )
        if duplicates:
            print(
                f"[Geo Wireframes] {len(duplicates)} duplicate marker(s) left over from "
                f"earlier runs (e.g. {duplicates[0]}); safe to delete by hand"
            )

        if anchor_points:
            _create_anchor_object(
                context,
                "GeoAnchorPoints",
                anchor_points,
                anchor_items,
                anchors_coll,
                globe_root=overlay_root,
            )

        if created == 0:
            self.report({"ERROR"}, "No valid countries or continents found.")
            return {"CANCELLED"}

        if missing:
            unknown = ", ".join(missing)
            self.report(
                {"WARNING"},
                f"Created {created} markers. " f"Unrecognized countries: {unknown}",
            )
        else:
            self.report(
                {"INFO"},
                f"Created {created} geo marker instance(s).",
            )

        _collapse_outliner_view()
        return {"FINISHED"}


class OBJECT_OT_build_geo_nodes_source(bpy.types.Operator):
    bl_idname = "object.build_geo_nodes_source"
    bl_label = "Build GeoNodes Source"
    bl_description = "Create a Geometry Nodes source group wired to GeoAnchorPoints"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        anchor = _find_geo_anchor_object()
        if anchor is None:
            self.report(
                {"ERROR"},
                "No GeoAnchorPoints object found. Create geo markers first.",
            )
            return {"CANCELLED"}

        node_group = _ensure_geo_source_group(anchor)
        host = context.active_object
        if host is None:
            host = _ensure_geo_source_host(context)

        modifier = host.modifiers.get("Geo Source")
        if modifier is None or modifier.type != "NODES":
            modifier = host.modifiers.new(name="Geo Source", type="NODES")
        modifier.node_group = node_group

        _apply_node_group_input_defaults(
            modifier,
            node_group,
            {
                "Anchor Object": anchor,
                "As Instance": True,
            },
        )

        self.report(
            {"INFO"},
            f"Geo Source group attached to '{host.name}'.",
        )
        return {"FINISHED"}


class VIEW3D_PT_country_wireframes(bpy.types.Panel):
    bl_label = "Geo Wireframes"
    bl_idname = "VIEW3D_PT_country_wireframes"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Create"

    def draw(self, context):
        layout = self.layout
        coord = context.scene.geo_coord_settings

        layout.prop(coord, "coordinate_space")
        if coord.coordinate_space == "GLOBE":
            layout.prop(coord, "globe_radius")
        else:
            layout.prop(coord, "flat_unit_mode")
            if coord.flat_unit_mode == "FIXED":
                layout.prop(coord, "flat_scale")
                layout.prop(coord, "flat_fit_to_bbox")
            layout.prop(coord, "flat_aspect_mode")
        layout.prop(coord, "overlay_offset")


class VIEW3D_PT_country_wireframes_build(bpy.types.Panel):
    bl_label = "Country Wireframes"
    bl_idname = "VIEW3D_PT_country_wireframes_build"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Create"
    bl_parent_id = "VIEW3D_PT_country_wireframes"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.country_wireframe_settings

        row = layout.row(align=True)
        row.prop(settings, "countries_enabled", toggle=True)
        row.prop(settings, "continents_enabled", toggle=True)

        layout.prop(settings, "generation_mode")
        layout.prop(settings, "world_dataset_scale")

        row = layout.row()
        row.template_list(
            "VIEW3D_UL_geo_country_list",
            "",
            settings,
            "country_items",
            settings,
            "country_index",
            rows=10,
        )
        col = row.column(align=True)
        col.operator("object.refresh_geo_country_list", text="", icon="FILE_REFRESH")
        col.operator("object.select_all_geo_countries", text="", icon="CHECKBOX_HLT")
        col.operator(
            "object.select_none_geo_countries",
            text="",
            icon="CHECKBOX_DEHLT",
        )

        layout.prop(settings, "continents")

        layout.separator()

        layout.prop(settings, "resolution")
        layout.prop(settings, "create_reference_globe")

        layout.separator()

        layout.prop(settings, "marker_scale")
        layout.prop(settings, "continent_scale")
        layout.prop(settings, "add_surface_mesh")

        layout.separator()

        layout.prop(settings, "clear_existing")
        layout.operator(
            "object.create_country_wireframes",
            icon="MESH_UVSPHERE",
        )

        layout.separator()

        layout.operator(
            "object.build_geo_nodes_source",
            icon="NODETREE",
        )


class VIEW3D_PT_country_wireframes_geojson(bpy.types.Panel):
    bl_label = "GeoJSON Import"
    bl_idname = "VIEW3D_PT_country_wireframes_geojson"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Create"
    bl_parent_id = "VIEW3D_PT_country_wireframes"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.country_wireframe_settings
        coord = context.scene.geo_coord_settings

        layout.prop(settings, "geojson_output_mode")
        if settings.geojson_output_mode == "CURVE":
            layout.prop(settings, "geojson_curve_spline_type")
            layout.prop(settings, "geojson_curve_step_deg")

        layout.separator()

        layout.prop(settings, "geojson_point_mode")
        if settings.geojson_point_mode == "SPHERE":
            layout.prop(settings, "geojson_point_scale")
            layout.prop(settings, "geojson_point_resolution")

        layout.separator()

        layout.prop(settings, "geojson_simplify_tolerance_deg")
        if coord.coordinate_space == "GLOBE":
            layout.prop(settings, "geojson_spherical_tolerance_deg")
        layout.prop(settings, "geojson_split_collections")

        layout.separator()

        layout.prop(settings, "geojson_clear_existing")
        if settings.geojson_clear_existing:
            layout.prop(settings, "geojson_existing_data_mode")
        layout.operator(
            "object.import_geojson_wireframes",
            icon="IMPORT",
        )


class VIEW3D_PT_country_wireframes_osm(bpy.types.Panel):
    bl_label = "OSM Import"
    bl_idname = "VIEW3D_PT_country_wireframes_osm"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Create"
    bl_parent_id = "VIEW3D_PT_country_wireframes"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.country_wireframe_settings

        row = layout.row(align=True)
        row.prop(settings, "osm_preset")
        row.operator("object.apply_osm_preset", text="Apply", icon="PRESET")
        layout.prop(settings, "osm_tag_filters")
        layout.prop(settings, "osm_bbox")

        layout.separator()

        layout.prop(settings, "osm_query_mode")
        if settings.osm_query_mode == "CUSTOM":
            layout.prop(settings, "osm_endpoint")
            layout.prop(settings, "osm_custom_query")

        layout.separator()

        layout.prop(settings, "osm_timeout_seconds")
        layout.prop(settings, "osm_max_features")
        layout.prop(settings, "osm_max_response_mb")
        layout.prop(settings, "osm_simplify_tolerance_deg")

        layout.separator()

        layout.prop(settings, "osm_require_confirmation")
        if settings.osm_require_confirmation:
            layout.prop(settings, "osm_warn_bbox_area_deg2")
            layout.prop(settings, "osm_hard_bbox_area_deg2")

        layout.separator()

        layout.prop(settings, "osm_clear_existing")
        if settings.osm_clear_existing:
            layout.prop(settings, "osm_existing_data_mode")
        layout.operator(
            "object.import_osm_overpass",
            icon="URL",
        )


class VIEW3D_PT_country_wireframes_imagery(bpy.types.Panel):
    bl_label = "Imagery"
    bl_idname = "VIEW3D_PT_country_wireframes_imagery"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Create"
    bl_parent_id = "VIEW3D_PT_country_wireframes"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.country_wireframe_settings

        layout.prop(settings, "imagery_quality")
        if settings.imagery_quality == "CUSTOM":
            layout.prop(settings, "imagery_blue_marble_url")
        layout.prop(settings, "imagery_max_size")

        layout.separator()

        layout.prop(settings, "imagery_use_clouds")
        if settings.imagery_use_clouds:
            layout.prop(settings, "imagery_clouds_url")
            layout.prop(settings, "imagery_animate_clouds")
            if settings.imagery_animate_clouds:
                layout.prop(settings, "imagery_cloud_rotation_period_frames")

        layout.separator()

        layout.prop(settings, "imagery_require_confirmation")
        if settings.imagery_require_confirmation:
            layout.prop(settings, "imagery_max_download_mb")
        layout.prop(settings, "imagery_max_local_file_mb")
        layout.prop(settings, "imagery_max_source_pixels")

        layout.separator()

        layout.operator(
            "object.apply_blue_marble_texture",
            icon="IMAGE_DATA",
        )
        layout.operator(
            "object.apply_texture_from_raster",
            icon="FILE_IMAGE",
        )


CLASSES = (
    GeoCountryListItem,
    VIEW3D_UL_geo_country_list,
    OBJECT_OT_refresh_geo_country_list,
    OBJECT_OT_select_all_geo_countries,
    OBJECT_OT_select_none_geo_countries,
    CountryWireframeSettings,
    OBJECT_OT_import_geojson_wireframes,
    OBJECT_OT_apply_osm_preset,
    OBJECT_OT_import_osm_overpass,
    OBJECT_OT_apply_blue_marble_texture,
    OBJECT_OT_apply_texture_from_raster,
    OBJECT_OT_create_country_wireframes,
    OBJECT_OT_build_geo_nodes_source,
    VIEW3D_PT_country_wireframes,
    VIEW3D_PT_country_wireframes_build,
    VIEW3D_PT_country_wireframes_geojson,
    VIEW3D_PT_country_wireframes_osm,
    VIEW3D_PT_country_wireframes_imagery,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.country_wireframe_settings = bpy.props.PointerProperty(
        type=CountryWireframeSettings
    )


def unregister():
    if hasattr(bpy.types.Scene, "country_wireframe_settings"):
        del bpy.types.Scene.country_wireframe_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()

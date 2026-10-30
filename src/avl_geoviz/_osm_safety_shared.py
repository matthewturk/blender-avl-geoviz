# Shared OSM/Overpass safety checks used by both country_wireframes.py and
# flat_gis_importer.py - both independently fetch OSM data through Overpass,
# and both need the same "don't silently fire a potentially huge/slow query"
# safety net in front of their OSM-import operators: bbox sanity checks
# (range, order, size), area limits scaled to how dense the requested
# features are, and warnings about feature caps and timeouts. Kept here so
# there is exactly one implementation; each caller adapts its own settings
# shape into the plain arguments below rather than sharing a settings
# PropertyGroup.
#
# Areas are in km^2 rather than deg^2: a degree of longitude shrinks with
# latitude, so a deg^2 limit means very different ground areas in Quito and
# Oslo, and km^2 is what people can picture.

import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request

OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"

_KM_PER_DEG_LAT = 111.32

# Anything smaller than 10 m x 10 m is almost certainly a typo (a dropped
# digit), not an area someone wants to import.
_MIN_AREA_KM2 = 1.0e-4

# OSM has next to no data past these latitudes; a bbox up there usually
# means lon,lat order was pasted into a lat,lon field.
_POLAR_LAT = 84.0

_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_NUMBER_RE = re.compile(_NUMBER)
_INLINE_BBOX_RE = re.compile(r"\(\s*(" + r"\s*,\s*".join([_NUMBER] * 4) + r")\s*\)")
_BBOX_SETTING_RE = re.compile(r"\[\s*bbox\s*:\s*([^\]]+)\]")


def parse_bbox_text(raw_text):
    """Pull exactly four numbers out of pasted text (any separators), as
    (south, west, north, east). Doesn't validate them; see validate_bbox."""
    numbers = _NUMBER_RE.findall(str(raw_text))
    if len(numbers) != 4:
        raise RuntimeError(
            f"BBox needs 4 numbers (south, west, north, east); found {len(numbers)}."
        )
    return tuple(float(n) for n in numbers)


def bbox_dims_km(south, west, north, east):
    """(width, height) of the bbox in km, on a sphere - plenty for limits."""
    mid_lat = math.radians((south + north) / 2.0)
    width = max(0.0, east - west) * _KM_PER_DEG_LAT * math.cos(mid_lat)
    height = max(0.0, north - south) * _KM_PER_DEG_LAT
    return width, height


def bbox_area_km2(south, west, north, east):
    width, height = bbox_dims_km(south, west, north, east)
    return width * height


def format_dims(south, west, north, east):
    width, height = bbox_dims_km(south, west, north, east)
    area = width * height
    if max(width, height) < 1.0:
        return f"{width * 1000:.0f} x {height * 1000:.0f} m ({area:.3f} km²)"
    return f"{width:.2f} x {height:.2f} km ({area:.2f} km²)"


def bbox_around(lat, lon, width_km, height_km):
    """(south, west, north, east) of a width x height km box centered on
    lat, lon."""
    half_h = (height_km / 2.0) / _KM_PER_DEG_LAT
    half_w = (width_km / 2.0) / (_KM_PER_DEG_LAT * max(0.01, math.cos(math.radians(lat))))
    return (lat - half_h, lon - half_w, lat + half_h, lon + half_w)


def validate_bbox(south, west, north, east):
    """Raise RuntimeError, with a hint at the likely fix, if (south, west,
    north, east) isn't a usable bbox."""
    values = (south, west, north, east)
    if not all(math.isfinite(v) for v in values):
        raise RuntimeError("BBox contains a non-finite number.")

    lats_ok = all(-90.0 <= v <= 90.0 for v in (south, north))
    lons_ok = all(-180.0 <= v <= 180.0 for v in (west, east))
    if not (lats_ok and lons_ok):
        message = "Latitudes must be within ±90 and longitudes within ±180."
        swapped_ok = all(-90.0 <= v <= 90.0 for v in (west, east)) and all(
            -180.0 <= v <= 180.0 for v in (south, north)
        )
        if swapped_ok:
            message += (
                " These look like west,south,east,north (lon,lat) order; "
                "this expects south,west,north,east."
            )
        raise RuntimeError(message)

    if south >= north:
        raise RuntimeError(f"South ({south}) must be less than north ({north}).")
    if west >= east:
        message = f"West ({west}) must be less than east ({east})."
        if west > 0.0 > east:
            message += (
                " A box crossing the antimeridian (±180°) isn't supported; "
                "import each side separately."
            )
        raise RuntimeError(message)

    width, height = bbox_dims_km(south, west, north, east)
    if width * height < _MIN_AREA_KM2:
        raise RuntimeError(
            f"BBox is only {width * 1000:.1f} x {height * 1000:.1f} m; "
            "check for a dropped digit."
        )


def bbox_hints(south, west, north, east):
    """Non-fatal oddities worth a second look before querying."""
    hints = []
    if max(abs(south), abs(north)) > _POLAR_LAT:
        hints.append(
            "Latitude is near a pole, where OSM has almost no data. If you "
            "pasted west,south,east,north (lon,lat), reorder to south,west,north,east."
        )
    return hints


def bboxes_in_overpass_query(query):
    """(south, west, north, east) tuples written inline in Overpass QL, or
    in a global [bbox:...] setting, so a custom query's scope can be checked
    too. Areas built from area/around filters can't be sized up front."""
    found = []
    for match in _INLINE_BBOX_RE.finditer(str(query)):
        found.append(tuple(float(n) for n in _NUMBER_RE.findall(match.group(1))))
    setting = _BBOX_SETTING_RE.search(str(query))
    if setting:
        numbers = _NUMBER_RE.findall(setting.group(1))
        if len(numbers) == 4:
            found.append(tuple(float(n) for n in numbers))
    return found


def osm_preflight_messages(
    area_km2,
    recommended_km2,
    limit_factor,
    max_features,
    is_custom_query,
    timeout_seconds,
    what="this request",
):
    """Return a list of non-fatal warning strings to show in a confirmation
    dialog before firing a potentially expensive Overpass query.

    recommended_km2 is the largest area that's comfortable for the features
    being requested (dense features like buildings get a small one, sparse
    ones like motorways a big one). Above it is a warning; above
    limit_factor times it is a hard block (RuntimeError) that a
    confirmation dialog can't wave through. area_km2 may be None when the
    area isn't known (a custom query without an inline bbox).
    """
    messages = []

    if area_km2 is not None:
        hard_km2 = float(recommended_km2) * float(limit_factor)
        if area_km2 > hard_km2:
            raise RuntimeError(
                f"Area {area_km2:.2f} km² is over the {hard_km2:.1f} km² limit "
                f"for {what} ({float(limit_factor):g}x the recommended "
                f"{float(recommended_km2):g} km²). Shrink the box, request "
                "fewer kinds of features, or raise Area Limit."
            )
        if area_km2 > float(recommended_km2):
            messages.append(
                f"Area {area_km2:.2f} km² is above the {float(recommended_km2):g} km² "
                f"recommended for {what}; Overpass may time out or refuse it."
            )
    elif is_custom_query:
        messages.append("No bbox found in the custom query, so its size wasn't checked.")

    if int(max_features) == 0:
        messages.append(
            "Max Features is 0 (unbounded). This can pull a very large dataset."
        )
    elif int(max_features) > 10000:
        messages.append(f"Max Features is high ({int(max_features)}).")

    if is_custom_query:
        messages.append(
            "Custom Overpass query is enabled; server-side scope may exceed UI caps."
        )

    if int(timeout_seconds) > 120:
        messages.append(
            f"Timeout is high ({int(timeout_seconds)}s), "
            "indicating potentially heavy queries."
        )

    return messages


def fetch_overpass_json(endpoint, query, timeout_seconds, max_response_bytes, user_agent, log_prefix):
    """POST an Overpass QL query and return the decoded JSON, turning the
    ways Overpass says "too big" into errors that say what to do."""
    print(f"{log_prefix} Fetching OSM data from Overpass endpoint {endpoint} ...")
    encoded = urllib.parse.urlencode({"data": query}).encode("utf-8")
    request = urllib.request.Request(endpoint, data=encoded, headers={"User-Agent": user_agent})

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
                        "Overpass response exceeded safety limit "
                        f"({max_response_bytes // (1024 * 1024)} MB). "
                        "Shrink the bbox or request fewer kinds of features."
                    )

            content = b"".join(chunks).decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError(
                "Overpass is rate-limiting this address (HTTP 429). Wait a "
                "minute and retry."
            ) from exc
        if exc.code in (502, 503, 504):
            raise RuntimeError(
                f"Overpass timed out or is at capacity (HTTP {exc.code}). Try a "
                "smaller bbox or fewer kinds of features, retry later, or use "
                "another Overpass Endpoint."
            ) from exc
        detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
        raise RuntimeError(
            f"Overpass request failed with HTTP {exc.code}. {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Overpass request failed: {exc}") from exc
    except TimeoutError as exc:
        raise RuntimeError(
            f"Overpass didn't answer within {timeout_seconds}s. Try a smaller "
            "bbox or fewer kinds of features."
        ) from exc

    print(f"{log_prefix} Overpass fetch complete ({total} bytes).")

    try:
        data = json.loads(content)
    except Exception as exc:
        raise RuntimeError(f"Overpass returned invalid JSON: {exc}") from exc

    # Overpass reports a query that ran out of time or memory as HTTP 200
    # with a "remark" and whatever partial data it had, which would
    # otherwise import silently as an incomplete area.
    remark = data.get("remark", "") if isinstance(data, dict) else ""
    if "runtime error" in remark:
        raise RuntimeError(
            f"Overpass stopped early: {remark.strip()} Shrink the bbox or "
            "request fewer kinds of features."
        )
    return data

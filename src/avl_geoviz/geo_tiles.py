"""Area imagery from public-domain tile services: USGS The National Map and
NASA GIBS.

Given a south, west, north, east box and a resolution, this works out the
tiles that cover the box, downloads them (a few at a time, cached on disk),
stitches them and warps the mosaic to a north-up longitude/latitude grid
cropped to the box. The result is an RGBA array, or a GeoTIFF that the
GeoTIFF layer tools (geo_raster) place like any other.

Sources come in two tilings:

* "xyz3857": the usual web-map tiles (EPSG:3857, 256 px, zoom z has 2^z by
  2^z tiles). Only reaches +-85.05 degrees latitude.
* "gibs4326": NASA GIBS's longitude/latitude tiles (EPSG:4326, 512 px,
  level 0 pixels are 0.5625 degrees, halving per level, from -180, 90).

Only public-domain or NASA sources belong here.
"""

import concurrent.futures
import hashlib
import io
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

USER_AGENT = "AVL-GeoViz-Blender-Extension (+https://github.com/matthewturk/blender-avl-geoviz)"

# Per request (all patches together), and per image.
MAX_TILES = 1000
MAX_PIXELS = 256 * 2 ** 20
MAX_PATCHES = 64
MAX_SIDE = 8192
# Patches overlap their neighbours by this many pixels, so linear filtering
# at a patch's edge never blends with the transparent outside.
_PATCH_MARGIN = 2
_WORKERS = 6
_TIMEOUT = 20

_WEB_MERCATOR_HALF = 20037508.342789244  # metres from the center to an edge
_WEB_MERCATOR_MAX_LAT = 85.0511287798066
_METRES_PER_DEGREE = 111319.49079327357  # along the equator, as GIBS counts
_GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg4326/best"
_GIBS_CREDIT = "NASA EOSDIS GIBS"


@dataclass(frozen=True)
class Source:
    key: str
    label: str
    short: str
    description: str
    scheme: str  # "xyz3857" or "gibs4326"
    url: str  # {level} {row} {col} {time}
    max_level: int
    time: str  # "none", "daily" or a fixed date
    attribution: str
    coverage: str
    tile_size: int = 256
    ext: str = "jpg"


SOURCES = (
    Source(
        key="USGS_IMAGERY",
        label="USGS Imagery (US, ~1-2 m)",
        short="USGS",
        description="USDA NAIP aerial imagery from USGS The National Map. Public domain",
        scheme="xyz3857",
        url="https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{level}/{row}/{col}",
        max_level=16,
        time="none",
        attribution="USGS The National Map: Orthoimagery (USDA NAIP)",
        coverage="United States only. Elsewhere tiles are missing.",
    ),
    Source(
        key="LANDSAT_2000",
        label="Landsat 2000 Composite (global, 30 m)",
        short="Landsat2000",
        description="Cloud-free Landsat (WELD) true-color annual composite for 2000, from NASA GIBS",
        scheme="gibs4326",
        url=_GIBS + "/Landsat_WELD_CorrectedReflectance_TrueColor_Global_Annual/default/{time}/31.25m/{level}/{row}/{col}.jpeg",
        max_level=11,
        time="2000-12-01",
        attribution=f"NASA/USGS Landsat (WELD), via {_GIBS_CREDIT}",
        coverage="Global land, as of about 2000.",
        tile_size=512,
    ),
    Source(
        key="HLS_S30",
        label="HLS Sentinel-2 Daily (global, 30 m)",
        short="HLS-S30",
        description=(
            "Harmonized Landsat Sentinel-2, Sentinel-2 passes, one day at a time, from "
            "NASA GIBS. Most days cover only part of an area or are cloudy"
        ),
        scheme="gibs4326",
        url=_GIBS + "/HLS_S30_Nadir_BRDF_Adjusted_Reflectance/default/{time}/31.25m/{level}/{row}/{col}.png",
        max_level=11,
        time="daily",
        attribution=f"NASA HLS (contains modified Copernicus Sentinel data), via {_GIBS_CREDIT}",
        coverage="Global land, one pass per day: pick a date with a clear pass.",
        tile_size=512,
        ext="png",
    ),
    Source(
        key="HLS_L30",
        label="HLS Landsat Daily (global, 30 m)",
        short="HLS-L30",
        description=(
            "Harmonized Landsat Sentinel-2, Landsat passes, one day at a time, from NASA "
            "GIBS. Most days cover only part of an area or are cloudy"
        ),
        scheme="gibs4326",
        url=_GIBS + "/HLS_L30_Nadir_BRDF_Adjusted_Reflectance/default/{time}/31.25m/{level}/{row}/{col}.png",
        max_level=11,
        time="daily",
        attribution=f"NASA/USGS HLS Landsat, via {_GIBS_CREDIT}",
        coverage="Global land, one pass per day: pick a date with a clear pass.",
        tile_size=512,
        ext="png",
    ),
    Source(
        key="VIIRS_DAILY",
        label="VIIRS True Color Daily (global, 250 m)",
        short="VIIRS",
        description="Suomi NPP VIIRS corrected reflectance, one day at a time, from NASA GIBS",
        scheme="gibs4326",
        url=_GIBS + "/VIIRS_SNPP_CorrectedReflectance_TrueColor/default/{time}/250m/{level}/{row}/{col}.jpeg",
        max_level=8,
        time="daily",
        attribution=f"NASA VIIRS (Suomi NPP), via {_GIBS_CREDIT}",
        coverage="Global, daily, with clouds.",
        tile_size=512,
    ),
    Source(
        key="MODIS_DAILY",
        label="MODIS Terra True Color Daily (global, 250 m)",
        short="MODIS",
        description="Terra MODIS corrected reflectance, one day at a time, from NASA GIBS",
        scheme="gibs4326",
        url=_GIBS + "/MODIS_Terra_CorrectedReflectance_TrueColor/default/{time}/250m/{level}/{row}/{col}.jpeg",
        max_level=8,
        time="daily",
        attribution=f"NASA MODIS (Terra), via {_GIBS_CREDIT}",
        coverage="Global, daily since 2000, with clouds.",
        tile_size=512,
    ),
    Source(
        key="BLUE_MARBLE",
        label="Blue Marble Next Generation (global, 500 m)",
        short="BlueMarble",
        description="NASA's cloud-free Blue Marble Next Generation mosaic, from NASA GIBS",
        scheme="gibs4326",
        url=_GIBS + "/BlueMarble_NextGeneration/default/500m/{level}/{row}/{col}.jpeg",
        max_level=7,
        time="none",
        attribution=f"NASA Blue Marble Next Generation, via {_GIBS_CREDIT}",
        coverage="Global, cloud-free.",
        tile_size=512,
    ),
)
SOURCES_BY_KEY = {s.key: s for s in SOURCES}


class TileError(RuntimeError):
    pass


class NoImagery(TileError):
    """The source has none of the tiles a request needs."""


class Cancelled(TileError):
    """A fetch was cancelled."""


# ---------------------------------------------------------------------------
#  Tiling math
# ---------------------------------------------------------------------------


def _mercator_lat(y):
    return math.degrees(math.atan(math.sinh(y * math.pi / _WEB_MERCATOR_HALF)))


def _mercator_y(lat):
    """Web Mercator northing in metres."""
    lat = max(-_WEB_MERCATOR_MAX_LAT, min(_WEB_MERCATOR_MAX_LAT, lat))
    return _WEB_MERCATOR_HALF / math.pi * math.asinh(math.tan(math.radians(lat)))


def resolution_m(source, level, lat):
    """Ground size of one pixel at *level*, in metres (north-south for
    GIBS tiles, which are coarser east-west only away from the equator)."""
    if source.scheme == "xyz3857":
        return 2 * _WEB_MERCATOR_HALF * math.cos(math.radians(lat)) / (source.tile_size * 2 ** level)
    return 0.5625 / 2 ** level * _METRES_PER_DEGREE


def level_for(source, metres_per_pixel, lat):
    """The coarsest level at least as fine as *metres_per_pixel*, or the
    source's finest if none is. 0 (or less) asks for the finest."""
    if metres_per_pixel > 0:
        for level in range(source.max_level + 1):
            if resolution_m(source, level, lat) <= metres_per_pixel * 1.0001:
                return level
    return source.max_level


@dataclass
class Plan:
    source: Source
    bbox: tuple  # south, west, north, east
    level: int
    cols: range
    rows: range
    src_transform: object  # affine of the mosaic's top-left
    src_crs: str
    size: tuple  # output width, height
    resolution_m: float

    @property
    def tiles(self):
        return len(self.cols) * len(self.rows)

    def problem(self):
        """Why this plan can't be fetched as one image (None if it can)."""
        if max(self.size) > MAX_SIDE:
            return (f"A {self.size[0]}x{self.size[1]} px image is over the limit of {MAX_SIDE} "
                    "px a side: use a coarser resolution or a smaller area.")
        return None


def plan(source, bbox, level):
    """The tiles and output grid for *bbox* (south, west, north, east) at
    *level*. Raises TileError if the source can't cover the box."""
    from rasterio.transform import Affine

    south, west, north, east = bbox
    if not (west < east and south < north):
        raise TileError("The area must have west < east and south < north.")
    level = max(0, min(source.max_level, int(level)))
    eps = 1e-9
    if source.scheme == "xyz3857":
        if south >= _WEB_MERCATOR_MAX_LAT or north <= -_WEB_MERCATOR_MAX_LAT:
            raise TileError(f"{source.short} tiles stop at {_WEB_MERCATOR_MAX_LAT:.2f} degrees latitude.")
        n = 2 ** level
        tile_m = 2 * _WEB_MERCATOR_HALF / n
        fx = lambda lon: (lon + 180.0) / 360.0 * n  # noqa: E731
        fy = lambda lat: (_WEB_MERCATOR_HALF - _mercator_y(lat)) / tile_m  # noqa: E731
        cols = range(max(0, math.floor(fx(west))), min(n - 1, math.floor(fx(east) - eps)) + 1)
        rows = range(max(0, math.floor(fy(north))), min(n - 1, math.floor(fy(south) - eps)) + 1)
        px = tile_m / source.tile_size
        transform = Affine(px, 0, -_WEB_MERCATOR_HALF + cols.start * tile_m,
                           0, -px, _WEB_MERCATOR_HALF - rows.start * tile_m)
        width = (fx(east) - fx(west)) * source.tile_size
        height = (fy(south) - fy(north)) * source.tile_size
        crs = "EPSG:3857"
    else:
        px = 0.5625 / 2 ** level
        tile_deg = px * source.tile_size
        matrix_w, matrix_h = math.ceil(360.0 / tile_deg - eps), math.ceil(180.0 / tile_deg - eps)
        cols = range(max(0, math.floor((west + 180.0) / tile_deg)),
                     min(matrix_w - 1, math.floor((east + 180.0) / tile_deg - eps)) + 1)
        rows = range(max(0, math.floor((90.0 - north) / tile_deg)),
                     min(matrix_h - 1, math.floor((90.0 - south) / tile_deg - eps)) + 1)
        transform = Affine(px, 0, -180.0 + cols.start * tile_deg, 0, -px, 90.0 - rows.start * tile_deg)
        width = (east - west) / px
        height = (north - south) / px
        crs = "EPSG:4326"
    size = (max(1, int(round(width))), max(1, int(round(height))))
    lat = (south + north) / 2.0
    return Plan(source, (south, west, north, east), level, cols, rows, transform, crs, size,
                resolution_m(source, level, lat))


@dataclass
class Patch:
    row: int  # from the north
    col: int  # from the west
    bbox: tuple  # south, west, north, east this patch covers
    plan: Plan  # its fetch: bbox plus an overlap with its neighbours

    @property
    def extent(self):
        """bbox as west, south, east, north."""
        south, west, north, east = self.bbox
        return (west, south, east, north)


@dataclass
class Split:
    whole: Plan
    patches: list
    nx: int
    ny: int

    @property
    def count(self):
        return self.nx * self.ny

    @property
    def tiles(self):
        return self.whole.tiles

    @property
    def pixels(self):
        return self.whole.size[0] * self.whole.size[1]

    def problem(self):
        """Why this request shouldn't be fetched (None if it's fine)."""
        if self.count > MAX_PATCHES:
            return (f"{self.nx}x{self.ny} = {self.count} patches is over the limit of "
                    f"{MAX_PATCHES}: use a coarser resolution, a larger Patch Size or a "
                    "smaller area.")
        if self.tiles > MAX_TILES:
            return (f"{self.tiles} tiles is over the limit of {MAX_TILES}: use a coarser "
                    "resolution or a smaller area.")
        if self.pixels > MAX_PIXELS:
            return (f"{self.pixels / 2 ** 20:.0f} Mpx is over the limit of "
                    f"{MAX_PIXELS // 2 ** 20} Mpx: use a coarser resolution or a smaller area.")
        return None


def split(source, bbox, level, patch_size):
    """Split the request for *bbox* at *level* into a grid of patches no
    larger than *patch_size* pixels a side. Patches are equal in pixels (so
    web-map patches are split evenly in Mercator, not latitude) and overlap
    by a couple of pixels; each patch's bbox is the part it's meant to show.
    One patch if the whole fits. Patches are left out when there would be
    more than MAX_PATCHES (see Split.problem)."""
    whole = plan(source, bbox, level)
    width, height = whole.size
    usable = max(1, patch_size - 2 * _PATCH_MARGIN)
    nx = 1 if width <= patch_size else math.ceil(width / usable)
    ny = 1 if height <= patch_size else math.ceil(height / usable)
    if nx * ny > MAX_PATCHES:
        return Split(whole, [], nx, ny)
    if nx * ny == 1:
        return Split(whole, [Patch(0, 0, whole.bbox, whole)], 1, 1)

    south, west, north, east = whole.bbox
    lons = [west + (east - west) * i / nx for i in range(nx + 1)]
    if source.scheme == "xyz3857":
        y_n, y_s = _mercator_y(north), _mercator_y(south)
        lats = [north] + [_mercator_lat(y_n + (y_s - y_n) * j / ny) for j in range(1, ny)] + [south]
    else:
        lats = [north - (north - south) * j / ny for j in range(ny + 1)]
    margin_lon = _PATCH_MARGIN * (east - west) / width
    margin_lat = _PATCH_MARGIN * (north - south) / height
    patches = []
    for j in range(ny):
        for i in range(nx):
            box = (lats[j + 1], lons[i], lats[j], lons[i + 1])
            grown = (max(south, box[0] - margin_lat), max(west, box[1] - margin_lon),
                     min(north, box[2] + margin_lat), min(east, box[3] + margin_lon))
            patches.append(Patch(j, i, box, plan(source, grown, level)))
    return Split(whole, patches, nx, ny)


# ---------------------------------------------------------------------------
#  Downloading
# ---------------------------------------------------------------------------


def online_access():
    """Blender's Allow Online Access preference (or --online-mode)."""
    import bpy

    return bool(getattr(bpy.app, "online_access", True))


def _http_get(url):
    """The body at *url*, or None if the tile doesn't exist (404, or 400 /
    204 for tiles out of a layer's range). Retries transient errors."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                if response.status == 204:
                    return None
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404):
                return None
            if exc.code < 500 and exc.code != 429:
                raise
            error = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            error = exc
        if attempt < 2:
            time.sleep(1.0 + 2.0 * attempt)
    raise error


def cache_dir():
    import bpy

    return bpy.utils.extension_path_user(__package__, path="imagery", create=True)


def _tile_url(source, level, row, col, when):
    return source.url.format(level=level, row=row, col=col, time=when)


def resolve_time(source, date):
    """The {time} value for a request: the fixed date for composites, *date*
    (YYYY-MM-DD) or GIBS's latest ("default") for daily layers."""
    if source.time == "none":
        return ""
    if source.time != "daily":
        return source.time
    date = (date or "").strip()
    if not date:
        return "default"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise TileError(f"Date {date!r} isn't YYYY-MM-DD.")
    return date


def _fetch_tile(source, level, row, col, when, cache_root):
    """Tile bytes (from the cache if there), or None if it doesn't exist.
    "default" (latest) dates change daily, so they aren't cached."""
    cached = None
    if cache_root and when != "default":
        cached = os.path.join(cache_root, "tiles", source.key, when or "-", str(level),
                              str(row), f"{col}.{source.ext}")
        if os.path.exists(cached):
            with open(cached, "rb") as f:
                return f.read()
    data = _http_get(_tile_url(source, level, row, col, when))
    if data is not None and cached:
        os.makedirs(os.path.dirname(cached), exist_ok=True)
        tmp = cached + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, cached)
    return data


def has_tile_at(source, lat, lon, level, date="", cache_root=None):
    """Whether the source has a tile at a point, as a cheap coverage check."""
    p = plan(source, (lat - 1e-6, lon - 1e-6, lat + 1e-6, lon + 1e-6), level)
    return _fetch_tile(source, p.level, p.rows.start, p.cols.start,
                       resolve_time(source, date), cache_root) is not None


@dataclass
class Mosaic:
    rgba: object  # (height, width, 4) uint8, north-up
    bounds: tuple  # west, south, east, north
    plan: Plan
    time: str
    missing: int  # tiles the source doesn't have

    @property
    def attribution(self):
        return self.plan.source.attribution


def fetch(source, bbox, level, date="", cache_root=None, progress=None, cancel=None):
    """Download and stitch the tiles for *bbox* at *level* and warp them to
    a longitude/latitude grid cropped to it. *progress* is called with
    (done, total) as tiles arrive (from this thread). Setting *cancel* (a
    threading.Event) stops it: queued tiles are dropped, the few in flight
    finish, and it raises Cancelled. Raises TileError if the plan is over
    the limits, NoImagery if no tile exists."""
    import numpy as np
    from PIL import Image
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    from rasterio.warp import reproject

    p = plan(source, bbox, level)
    problem = p.problem()
    if problem:
        raise TileError(problem)
    when = resolve_time(source, date)
    ts = source.tile_size
    mosaic = np.zeros((4, len(p.rows) * ts, len(p.cols) * ts), dtype=np.uint8)
    jobs = [(r, c) for r in p.rows for c in p.cols]
    missing = 0
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_tile, source, p.level, r, c, when, cache_root): (r, c)
            for r, c in jobs
        }
        for future in concurrent.futures.as_completed(futures):
            if cancel is not None and cancel.is_set():
                for pending in futures:
                    pending.cancel()
                raise Cancelled("Cancelled")
            r, c = futures[future]
            data = future.result()
            done += 1
            if progress:
                progress(done, len(jobs))
            if data is None:
                missing += 1
                continue
            tile = np.asarray(Image.open(io.BytesIO(data)).convert("RGBA").resize((ts, ts)))
            y, x = (r - p.rows.start) * ts, (c - p.cols.start) * ts
            mosaic[:, y:y + ts, x:x + ts] = np.moveaxis(tile, -1, 0)
    if missing == len(jobs):
        raise NoImagery(f"{source.short} has no imagery here ({source.coverage})")

    south, west, north, east = p.bbox
    width, height = p.size
    out = np.zeros((4, height, width), dtype=np.uint8)
    reproject(
        source=mosaic, destination=out,
        src_transform=p.src_transform, src_crs=p.src_crs,
        dst_transform=from_bounds(west, south, east, north, width, height), dst_crs="EPSG:4326",
        resampling=Resampling.bilinear,
    )
    return Mosaic(np.ascontiguousarray(np.moveaxis(out, 0, -1)), (west, south, east, north), p, when, missing)


def write_geotiff(path, mosaic):
    """Save a Mosaic as an RGBA, EPSG:4326 GeoTIFF."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    height, width = mosaic.rgba.shape[:2]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part.tif"
    with rasterio.open(
        tmp, "w", driver="GTiff", width=width, height=height, count=4, dtype="uint8",
        crs="EPSG:4326", transform=from_bounds(*mosaic.bounds, width, height),
        photometric="RGB", alpha="YES", compress="deflate",
    ) as dst:
        dst.write(np.moveaxis(mosaic.rgba, -1, 0))
        dst.update_tags(SOURCE=mosaic.plan.source.label, ATTRIBUTION=mosaic.attribution,
                        LEVEL=str(mosaic.plan.level), TIME=mosaic.time)
    os.replace(tmp, path)
    return path


def request_name(source, bbox, level, when):
    """A short, stable name for a request; the same request gets the same
    name, so fetching it again updates what it made before."""
    key = "|".join([source.key, str(level), when] + [f"{v:.6f}" for v in bbox])
    digest = hashlib.sha1(key.encode()).hexdigest()[:6]
    parts = [source.short, f"L{level}"]
    if source.time == "daily":
        parts.append(when)
    return " ".join(parts + [digest])

# AVL GeoViz

A Blender extension from NCSA's Advanced Visualization Lab for the I-GUIDE
geospatial work: country boundaries on a globe or flat map, OpenStreetMap
and GeoJSON import, GPX tracks, and trade-flow emission objects. Its Python
dependencies (geopandas, rasterio, osmnx, scipy, databpy, nodebpy,
d3blender, ...) are bundled, so it works on a stock Blender install.

Requires Blender 5.2 or later; on macOS, Apple Silicon and macOS 15 or
later (rasterio's macOS wheels need it).

## Installing

Download the zip for your platform (`linux_x64`, `macos_arm64` or
`windows_x64`) from the [releases page](https://github.com/matthewturk/blender-avl-geoviz/releases)
and install it with **Edit > Preferences > Get Extensions > Install from
Disk**.

## Tools

**3D Viewport sidebar > AVL GIS**

Rarely changed options are in collapsed sub-panels.

- **Area & Features**: the area and the OpenStreetMap features that both
  importers below fetch.
  - **Area**: a south,west,north,east box (paste it with any separators),
    or **From Center**, which takes a point and a size in km. The panel
    shows the box's size as you edit it. It catches swapped lat/lon
    order, reversed or antimeridian-crossing boxes and typos before
    anything is fetched.
  - **Features**: presets from Everything through Buildings + Streets,
    Buildings, Streets, Major Roads, Rail, Water, Parks and more, or your
    own tag filters (`key`, `key=value`, `key=a|b`). Each preset has a
    recommended area, sized to how dense its features are (about 1 km² for
    Everything, 4 km² for buildings, 500 km² for major roads). Imports ask
    first above it and refuse above Area Limit (10x by default) times it.
  - **Request Limits**: Max Features, response size, timeout and the
    Overpass endpoint. If the main Overpass server is busy, use another
    public instance.
- **Globe & World Map**: where the importers under it place data (globe or
  flat world map).
  - **Country Wireframes**: country and continent boundaries from Natural
    Earth, with marker objects carrying each country's names and ISO/M49
    codes.
  - **GeoJSON Import**.
  - **OpenStreetMap**: imports the Area & Features selection as
    one wireframe or curve object per feature, carrying its OSM tags. A
    raw Overpass query can be used instead under Advanced Query.
  - **Imagery**: Blue Marble or a raster file on the globe.
    - **Add GeoTIFF Layer** drapes a GeoTIFF over the selected objects
      (the reference globe if nothing is selected), on the globe or a flat
      map, by longitude/latitude. No UVs are needed. The GeoTIFF must be
      north-up and in longitude/latitude. Reproject other files first,
      e.g. `gdalwarp -t_srs EPSG:4326 in.tif out.tif`, or turn on
      **Reproject to Lon/Lat** in the file browser's options to warp them
      to EPSG:4326 in memory (fine for typical projected rasters; do
      datum shifts and antimeridian or polar data outside Blender). Each file becomes
      a `GeoTIFF: <name>` layer in front of the material's Base Color, so
      layers stack and show what's below outside the raster and at
      nodata. Three-band files are used as color; single-band files keep
      their raw values (the layer's Value output) and are colored with an
      editable color ramp. Flat objects only line up if they came from
      one of the importers here, which store each object's map mapping on
      it.
  - **Longitude/latitude on imports**: meshes made by the importers here
    (GeoJSON/OSM wireframes, country boundaries, GPX, the Local Flat Map's
    source geometry and the reference globe) have per-point `geo_lon` and
    `geo_lat` attributes in degrees, for Named Attribute in Geometry Nodes
    or an Attribute node in shaders. Point markers have them as object
    properties. Curve-mode imports can't hold attributes, but like every
    import they carry the per-object mapping Add GeoTIFF Layer uses.
  - **Flat-space node groups**: creating the reference globe also adds
    two geometry-node groups, **Geo Globe To Flat** and **Geo Flat To
    Globe**. Use them as nodes or as modifiers around operations that need
    local Z = 0, such as Fill Curve on an imported polygon: Globe To Flat
    → Fill Curve → Flat To Globe. Flat space is an equirectangular map
    in globe units centered on Center Latitude/Longitude (set both groups
    to the same values, near the feature), with height above the globe in
    Z. Only points move, so subdivide or resample in flat space if large
    faces need to follow the curve of the globe.
  - **GPX Import**: GPS tracks, routes and waypoints. Re-importing a file
    updates its objects in place.
- **Local Flat Map**: builds a metric scene of the area, centered on it:
  extruded buildings, roads with widths and point markers from the
  selected features (other kinds of features in the preset are skipped),
  plus terrain, elevation and satellite imagery. Satellite imagery is
  saved next to the .blend (or in the extension's user folder for an
  unsaved file).

The default area is about 1 km² around the UIUC Main Quad.

**Add > Data Visualization**
- **Constant-Mass Emission Generator**: turns a time series stored on a
  mesh or curve into emission times, one point per fixed amount of mass,
  and keeps itself up to date when the source changes.
- **Country Pair Batch Emitter**: the same for every country pair in a
  trade dataset, as one multi-curve object.

Their settings are in **Properties > Object** and in the Node Editor
sidebar's **Data Viz** tab.

## Moving from blender-config's startup scripts

These tools used to be loaded from blender-config's `scripts/startup`.
Operator and panel names are unchanged, and existing .blend files keep their
settings. The OSM area, preset and request limits are now shared by both
importers; a file saved with them in the old per-importer settings has them
copied into the shared ones when it's opened. The two copies can't be active at once; the extension
refuses to enable while the startup modules are loaded.

## Building

```bash
scripts/build_extension.sh <path/to/blender> dist                       # all three platforms
PLATFORMS=linux-x64 scripts/build_extension.sh <path/to/blender> dist   # just one
tests/run_smoke.sh <path/to/blender> dist/avl_geoviz-*-linux_x64.zip [--network]
```

The build needs [uv](https://docs.astral.sh/uv/). Dependencies are listed
in `requirements.in`; a `name @ git+URL` entry is built into a wheel from
its repository. `tests/run_smoke.sh` installs the zip into a throwaway
Blender profile and runs each tool once on small inline data.

Pushing a `v*` tag (e.g. `v1.0.1`) builds all three zips on GitHub Actions,
smoke-tests the Linux one and attaches them to a GitHub release. The tag has
to match `version` in `src/avl_geoviz/blender_manifest.toml`, or the build
fails. Pull requests and manual runs only build the zips, as a downloadable
workflow artifact.

License: GPL-3.0-or-later.

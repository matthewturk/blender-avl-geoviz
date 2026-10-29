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

**3D Viewport sidebar > Create**
- **Country Wireframes**: country and continent boundaries from Natural
  Earth, projected onto a globe or flat map, with marker objects carrying
  each country's names and ISO/M49 codes. Sub-panels import GeoJSON files,
  query OpenStreetMap (Overpass), and texture the globe with Blue Marble or
  a raster file.
- **GPX Import**: GPS tracks, routes and waypoints. Re-importing a file
  updates its objects in place.

**3D Viewport sidebar > Flat GIS**: a local, metric flat map of an area,
from OpenStreetMap features, satellite tiles and elevation. Satellite
imagery is saved next to the .blend (or in the extension's user folder for
an unsaved file).

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
Operator, panel and property names are unchanged, so existing .blend files
keep their settings. The two copies can't be active at once; the extension
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

"""AVL GeoViz: globe and flat-map GIS import, GPX tracks, and trade-flow
emission tools for NCSA AVL / I-GUIDE work."""

import bpy

from . import (
    geo_coord,
    country_wireframes,
    flat_gis_importer,
    gpx_import,
    data_visualization_menu,
    constant_mass_emission_generator,
    country_pair_batch_emitter,
)

# Registration order: data_visualization_menu before the two generators,
# which add their entries to its Add-menu category.
_MODULES = (
    geo_coord,
    country_wireframes,
    flat_gis_importer,
    gpx_import,
    data_visualization_menu,
    constant_mass_emission_generator,
    country_pair_batch_emitter,
)

# Registered by the same modules when they're loaded from blender-config's
# scripts/startup instead; both copies can't be active at once.
_OLD_STARTUP_PROPS = (
    (bpy.types.Scene, "geo_coord_settings"),
    (bpy.types.Scene, "country_wireframe_settings"),
    (bpy.types.Scene, "flat_gis_settings"),
    (bpy.types.Scene, "gpx_import_settings"),
    (bpy.types.Object, "cmet_settings"),
    (bpy.types.Object, "cpbe_settings"),
)


def register():
    clashes = [name for owner, name in _OLD_STARTUP_PROPS if name in owner.bl_rna.properties]
    if clashes:
        raise RuntimeError(
            "AVL GeoViz: these tools are already loaded from blender-config's "
            f"scripts/startup ({', '.join(clashes)}). Remove them from its "
            "__init__.py and restart Blender before enabling this extension."
        )
    for module in _MODULES:
        module.register()


def unregister():
    for module in reversed(_MODULES):
        module.unregister()

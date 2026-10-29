"""Shared "Data Visualization" Add-menu category.

Individual data-viz object generators (constant_mass_emission_generator.py,
and whatever comes after it) register their own "Add" operator here via
add_entry() instead of each adding its own separate top-level Add-menu entry
or inventing a new sibling category - one shared home for this whole class
of "object that queries some data and builds/keeps-in-sync a representation
of it" tools.
"""

import bpy

_ENTRIES = []  # list of (operator_bl_idname, text, icon) tuples


def add_entry(operator_bl_idname, text=None, icon="NONE"):
    if not any(e[0] == operator_bl_idname for e in _ENTRIES):
        _ENTRIES.append((operator_bl_idname, text, icon))


def remove_entry(operator_bl_idname):
    _ENTRIES[:] = [e for e in _ENTRIES if e[0] != operator_bl_idname]


class VIEW3D_MT_add_data_visualization(bpy.types.Menu):
    bl_idname = "VIEW3D_MT_add_data_visualization"
    bl_label = "Data Visualization"

    def draw(self, context):
        layout = self.layout
        if not _ENTRIES:
            layout.label(text="(none registered)")
        for bl_idname, text, icon in _ENTRIES:
            layout.operator(bl_idname, text=text, icon=icon)


def _draw_add_menu_item(self, context):
    self.layout.menu(VIEW3D_MT_add_data_visualization.bl_idname, icon="OUTLINER_OB_POINTCLOUD")


def register():
    bpy.utils.register_class(VIEW3D_MT_add_data_visualization)
    bpy.types.VIEW3D_MT_add.append(_draw_add_menu_item)


def unregister():
    bpy.types.VIEW3D_MT_add.remove(_draw_add_menu_item)
    bpy.utils.unregister_class(VIEW3D_MT_add_data_visualization)
    _ENTRIES.clear()

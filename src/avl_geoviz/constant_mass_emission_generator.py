# Live-updating counterpart to user_scripts/constant_mass_emission_times.py:
# instead of manually rerunning a script whenever the source time-series
# object's data changes, a dedicated object (created via Add > Data Visualization >
# Constant-Mass Emission Generator, NOT any arbitrary object - see below) carries its own
# "recipe" (source object, X/Y attribute names, mass-per-sphere or
# sphere-count) and turns *itself* into the emission-time point cloud. A
# depsgraph_update_post handler watches the small set of objects that have
# been explicitly created this way and reruns the computation automatically
# whenever their source object's data actually changes - no button, no
# polling, no rebuild of anything in Geometry Nodes (which has no
# monotonic-Hermite-interpolation or equation-solving nodes to do this
# natively - see constant_mass_emission_times.py's docstring).
#
# This is intentionally NOT a Geometry Nodes modifier: the computation needs
# scipy (PchipInterpolator + its root solver), which only exists in Python,
# not in the node graph's evaluation. What you get here is the next best
# thing - an object that keeps itself in sync the same way a modifier would,
# just driven by a depsgraph handler instead of node evaluation.
#
# Scoping - a "new class of object", not a property on everything: Blender's
# add-on API has no way to give only *some* objects a new RNA property - a
# PointerProperty registered on bpy.types.Object technically exists (with an
# inert default) on every Object instance, full stop, that's unavoidable.
# What IS avoidable is every other visible/behavioral trace of this system:
# the Properties-tab panel only shows for objects with cmet_settings.
# is_cmet_generator = True, the depsgraph handler only ever tracks objects
# with that flag set, and the ONLY way to set that flag is the dedicated
# "Add > Data Visualization > Constant-Mass Emission Generator" menu operator below (there's no
# "convert an arbitrary object" path on purpose) - so in practice this reads
# as its own distinct kind of object, created deliberately, not something
# that could silently appear on anything else.
#
# Composability with Geometry Nodes downstream: a generator object is just a
# normal object with real Mesh/Curves data (emission_time/cumulative_mass/
# sphere_index point attributes, positions all zero - this is a data table,
# not spatial geometry). Any other object's Geometry Nodes modifier can read
# it via an Object Info node (Geometry output) + Named Attribute, the usual
# way - that creates a real depsgraph dependency, so when this generator
# recomputes, anything downstream referencing it via Object Info
# re-evaluates automatically too. No extra plumbing needed for that chain:
# [weird curves] -> PCHIP generator (this file) -> Object Info -> your path-
# building Geometry Nodes modifier on some other object.
#
# The handler keeps an in-memory registry of which objects to watch (populated
# by the Add operator, rebuilt from scene data on file load) rather than
# scanning every object in bpy.data.objects on every depsgraph tick -
# depsgraph_update_post can fire many times per second during interactive
# work, so the per-tick cost needs to stay proportional to the number of
# *tracked* generator objects, not the number of objects in the scene.

import bpy
import numpy as np
import databpy as db
from bpy.app.handlers import persistent

from . import constant_mass_core as core
from . import constant_mass_geometry_io as geo
from . import _attr_io_shared as _attrs
from . import data_visualization_menu as dv_menu

_TRACKED = set()  # object names with live auto-update enabled


# ---------------------------------------------------------------------------
#  Settings - technically registered on bpy.types.Object (so RNA-wise every
#  object has a dormant cmet_settings block), but is_cmet_generator gates
#  every visible/behavioral consequence of that - see module docstring.
# ---------------------------------------------------------------------------

class CMET_Settings(bpy.types.PropertyGroup):
    source_object: bpy.props.PointerProperty(
        name="Source Object",
        description="Object (Mesh or Curves) whose point attributes hold the time series",
        type=bpy.types.Object,
    )
    x_attribute: bpy.props.StringProperty(
        name="Time Attribute", default="x",
        description="Point attribute holding the timestamp/year for each row",
    )
    y_attribute: bpy.props.StringProperty(
        name="Mass Attribute", default="y",
        description="Point attribute holding the amount shipped at each timestamp (must be >= 0 everywhere)",
    )
    mass_interval: bpy.props.FloatProperty(
        name="Mass Per Sphere", default=1.0, min=0.0,
        description="Constant mass represented by each emitted sphere. Ignored if 'Total Spheres' is > 0",
    )
    num_spheres: bpy.props.IntProperty(
        name="Total Spheres", default=100, min=0,
        description=(
            "If > 0, derive the mass-per-sphere as total_mass / this count "
            "instead of 'Mass Per Sphere' - the default (100) is scale-"
            "invariant regardless of what units/magnitude your mass data is "
            "in, unlike a fixed 'Mass Per Sphere', which can silently imply "
            "millions of spheres (and a very long, blocking computation) if "
            "left at a value far too small for your data's actual scale"
        ),
    )
    debug: bpy.props.BoolProperty(
        name="Debug Output",
        description="Print step-by-step progress and timing to the system console while recomputing",
        default=False,
    )
    result_type: bpy.props.EnumProperty(
        name="Output Type",
        items=(
            ("MESH", "Mesh", "Represent this object as a Mesh, one vertex per emitted sphere"),
            ("CURVES", "Curves", "Represent this object as a single-spline Curves, one point per emitted sphere"),
        ),
        default="CURVES",
    )
    auto_update: bpy.props.BoolProperty(
        name="Auto-Update",
        description="Automatically recompute whenever the source object's data changes",
        default=True,
    )
    last_status: bpy.props.StringProperty(name="Status", default="", options={"SKIP_SAVE"})
    is_cmet_generator: bpy.props.BoolProperty(
        name="Is Constant-Mass Emission Generator",
        description="Internal marker - only set by Add > Data Visualization > Constant-Mass Emission Generator",
        default=False,
    )

    # --- Country Pair Filter mode: an alternative to Source Object above -
    # instead of reading an already-filtered object, filter a big shared
    # multi-reporter dataset by two country objects (plus item/element
    # codes) internally, in Python. This is what lets one CMET object serve
    # as the single "which two countries" selection point for a connection,
    # instead of needing a separate GN filter object per path.
    mode: bpy.props.EnumProperty(
        name="Mode",
        items=(
            ("DIRECT", "Direct Source", "Read Time/Mass attributes directly from Source Object, as before"),
            ("COUNTRY_PAIR", "Country Pair Filter", (
                "Filter a shared raw dataset by two country objects plus item/"
                "element codes, computed internally - no separate filter "
                "object needed"
            )),
        ),
        default="DIRECT",
    )
    raw_data_object: bpy.props.PointerProperty(
        name="Raw Data Object",
        description=(
            "The big, unfiltered multi-reporter Curves dataset (one spline "
            "per reporter country) - shared across every connection, only "
            "used in Country Pair Filter mode"
        ),
        type=bpy.types.Object,
    )
    country_a_object: bpy.props.PointerProperty(
        name="Country A (Reporter)",
        description="Reporter country wireframe object - only used in Country Pair Filter mode",
        type=bpy.types.Object,
    )
    country_b_object: bpy.props.PointerProperty(
        name="Country B (Partner)",
        description="Partner country wireframe object - only used in Country Pair Filter mode",
        type=bpy.types.Object,
    )
    code_property_name: bpy.props.StringProperty(
        name="Code Property", default="m49",
        description="Custom property on Country A/B objects holding their own country code",
    )
    reporter_attribute_name: bpy.props.StringProperty(
        name="Reporter Attribute", default="m49",
        description=(
            "CURVE-domain attribute on Raw Data Object identifying which "
            "spline belongs to which reporter country"
        ),
    )
    partner_attribute_name: bpy.props.StringProperty(
        name="Partner Attribute", default="Partner Country Code",
        description="POINT-domain attribute on Raw Data Object holding each row's partner country code",
    )
    item_code_attribute: bpy.props.StringProperty(name="Item Code Attribute", default="Item Code")
    element_code_attribute: bpy.props.StringProperty(name="Element Code Attribute", default="Element Code")
    item_code_value: bpy.props.IntProperty(name="Item Code", default=0)
    element_code_value: bpy.props.IntProperty(name="Element Code", default=0)


# ---------------------------------------------------------------------------
#  Core recompute, writing in place into the tracked object itself
# ---------------------------------------------------------------------------

def _filter_country_pair(settings, depsgraph, log):
    raw_obj = settings.raw_data_object
    country_a = settings.country_a_object
    country_b = settings.country_b_object
    if raw_obj is None or country_a is None or country_b is None:
        raise ValueError("Raw Data Object, Country A, and Country B must all be set")

    code_prop = settings.code_property_name.strip()
    reporter_code = country_a.get(code_prop)
    partner_code = country_b.get(code_prop)
    if reporter_code is None:
        raise ValueError(f"'{country_a.name}' has no custom property '{code_prop}'")
    if partner_code is None:
        raise ValueError(f"'{country_b.name}' has no custom property '{code_prop}'")
    log(f"country A ('{country_a.name}') {code_prop}={reporter_code!r}, country B ('{country_b.name}') {code_prop}={partner_code!r}")

    geometry_set = geo.evaluated_geometry_set(raw_obj, depsgraph)
    curves_data = geometry_set.curves
    if curves_data is None:
        raise TypeError(f"'{raw_obj.name}'s evaluated geometry has no Curves component")

    start, length = geo.find_matching_spline_point_range(
        curves_data, raw_obj.name, settings.reporter_attribute_name.strip(), reporter_code, log=log
    )
    end = start + length

    partner_arr = geo.read_full_point_attribute(curves_data, raw_obj.name, settings.partner_attribute_name.strip())[start:end]
    item_arr = geo.read_full_point_attribute(curves_data, raw_obj.name, settings.item_code_attribute.strip())[start:end]
    element_arr = geo.read_full_point_attribute(curves_data, raw_obj.name, settings.element_code_attribute.strip())[start:end]
    x_full = geo.read_full_point_attribute(curves_data, raw_obj.name, settings.x_attribute.strip())[start:end]
    y_full = geo.read_full_point_attribute(curves_data, raw_obj.name, settings.y_attribute.strip())[start:end]

    mask = np.array([
        geo.match_code(p, partner_code) and geo.match_code(i, settings.item_code_value) and geo.match_code(e, settings.element_code_value)
        for p, i, e in zip(partner_arr, item_arr, element_arr)
    ])
    log(f"spline has {length} points, {int(mask.sum())} match partner/item/element filters")
    if not mask.any():
        raise ValueError(
            f"No rows for reporter {reporter_code!r} match partner={partner_code!r}, "
            f"item={settings.item_code_value}, element={settings.element_code_value}"
        )

    return x_full[mask].astype(np.float64), y_full[mask].astype(np.float64)


def _ensure_result_type(obj):
    """If obj's fixed Object.type doesn't match its cmet_settings.result_type
    (the user just switched "Output Type" on an existing generator),
    Object.data can't be reassigned across that type boundary - a MESH
    object can never become a CURVES object (or back) via a `.data` swap,
    since .type is fixed at Object creation. The only way to actually switch
    is to delete this Object and build a fresh one of the right type, the
    same way OBJECT_OT_cmet_add/_new_generator_data do at initial-creation
    time, preserving name/transform/collection membership.

    cmet_settings is itself a PropertyGroup living on the Object, so it dies
    along with the old Object - every field is read off into a plain dict
    before removing anything, then copied onto the new Object's own (freshly
    default-initialized) cmet_settings afterward.

    Returns obj unchanged if no rebuild was needed, otherwise the new object
    to use in its place - the passed-in obj may already be removed from
    bpy.data by the time this returns, so callers must switch to the
    returned reference.
    """
    settings = obj.cmet_settings
    desired_type = "MESH" if settings.result_type == "MESH" else "CURVES"
    if obj.type == desired_type:
        return obj

    old_name = obj.name
    old_matrix = obj.matrix_world.copy()
    collections = list(obj.users_collection) or [bpy.context.scene.collection]
    old_data = obj.data
    saved_settings = {
        prop.identifier: getattr(settings, prop.identifier)
        for prop in settings.bl_rna.properties
        if prop.identifier != "rna_type"
    }

    bpy.data.objects.remove(obj, do_unlink=True)
    if old_data is not None and old_data.users == 0:
        if isinstance(old_data, bpy.types.Mesh):
            bpy.data.meshes.remove(old_data)
        else:
            bpy.data.hair_curves.remove(old_data)

    new_data = _new_generator_data(old_name, saved_settings["result_type"])
    new_obj = bpy.data.objects.new(old_name, new_data)
    for col in collections:
        col.objects.link(new_obj)
    new_obj.matrix_world = old_matrix

    new_settings = new_obj.cmet_settings
    for identifier, value in saved_settings.items():
        setattr(new_settings, identifier, value)

    return new_obj


def _recompute_and_write(obj, depsgraph):
    settings = obj.cmet_settings
    log = (lambda msg: print(f"[CMET] '{obj.name}': {msg}")) if settings.debug else (lambda msg: None)

    country_a_position = None
    country_b_position = None

    if settings.mode == "COUNTRY_PAIR":
        if settings.raw_data_object is None or settings.country_a_object is None or settings.country_b_object is None:
            settings.last_status = "Raw Data Object / Country A / Country B must all be set"
            return
        log(f"country-pair mode: reporter='{settings.country_a_object.name}' partner='{settings.country_b_object.name}'")
        x, y = _filter_country_pair(settings, depsgraph, log)
        country_a_position = tuple(settings.country_a_object.matrix_world.translation)
        country_b_position = tuple(settings.country_b_object.matrix_world.translation)
    else:
        src = settings.source_object
        if src is None:
            settings.last_status = "No source object set"
            return
        if src == obj:
            settings.last_status = "Source object can't be this object itself"
            return
        log(f"reading '{settings.x_attribute}'/'{settings.y_attribute}' from '{src.name}'")
        x = geo.read_point_attribute(src, depsgraph, settings.x_attribute.strip())
        y = geo.read_point_attribute(src, depsgraph, settings.y_attribute.strip())

    log(f"read {len(x)} rows")

    emission_times, target_masses, particle_mass = core.compute_emission_times(
        x, y, settings.mass_interval, settings.num_spheres, log=log
    )
    k_max = len(emission_times)
    zeros = np.zeros((k_max, 3), dtype=np.float32)
    log("writing geometry/attributes")

    result_type = settings.result_type
    # obj.type is already guaranteed to match result_type here - _safe_recompute
    # calls _ensure_result_type() before this function ever runs, so the
    # isinstance() checks below only ever trigger on a point-count mismatch,
    # never a type mismatch (Object.data can't be reassigned across a
    # MESH<->CURVES type boundary at all, hence _ensure_result_type existing).
    if result_type == "MESH":
        if not isinstance(obj.data, bpy.types.Mesh) or len(obj.data.vertices) != k_max:
            new_data = bpy.data.meshes.new(name=obj.name)
            new_data.from_pydata(zeros.tolist(), [], [])
            new_data.update()
            old_data = obj.data
            obj.data = new_data
            if old_data and old_data.users == 0:
                bpy.data.meshes.remove(old_data)
    else:
        if not isinstance(obj.data, bpy.types.Curves) or len(obj.data.points) != k_max:
            new_data = bpy.data.hair_curves.new(name=obj.name)
            new_data.add_curves([k_max])
            old_data = obj.data
            obj.data = new_data
            if old_data and old_data.users == 0:
                bpy.data.hair_curves.remove(old_data)
        db.store_named_attribute(
            obj, zeros, "position", atype=db.AttributeTypes.FLOAT_VECTOR, domain=db.AttributeDomains.POINT
        )

    _attrs.write_float_attribute(obj, "emission_time", emission_times)
    _attrs.write_float_attribute(obj, "cumulative_mass", target_masses)
    _attrs.write_int_attribute(obj, "sphere_index", np.arange(1, k_max + 1, dtype=np.int32))

    if country_a_position is not None:
        # Constant across every point (broadcast) - lets "Connect Two
        # Countries" read both country positions AND emission_time from one
        # Object Info node on this same object, instead of needing a
        # separate Pair Filter object just for country positions.
        _attrs.write_vector_attribute(obj, "country_a_position", np.tile(country_a_position, (k_max, 1)))
        _attrs.write_vector_attribute(obj, "country_b_position", np.tile(country_b_position, (k_max, 1)))
    else:
        # DIRECT mode writes no country positions - but if this object was
        # previously run in COUNTRY_PAIR mode, its data-block may still carry
        # these attributes from before, and the point-count check above only
        # rebuilds the data-block when k_max actually changes, so a stale
        # DIRECT-mode run with the same k_max as the old COUNTRY_PAIR run
        # would otherwise silently keep the old values forever. Explicitly
        # drop them here - a no-op when they're already absent.
        data = obj.data
        for stale_name in ("country_a_position", "country_b_position"):
            stale_attr = data.attributes.get(stale_name)
            if stale_attr is not None:
                data.attributes.remove(stale_attr)

    total_mass = float(np.sum(y))
    leftover = total_mass - k_max * particle_mass
    settings.last_status = (
        f"OK: {k_max} spheres, mass/sphere={particle_mass:.6g}, "
        f"leftover={leftover:.6g}"
    )


def _needs_result_type_rebuild(obj):
    """Cheap, side-effect-free check for whether _ensure_result_type() would
    need to delete+recreate obj (Output Type just switched away from its
    current fixed Object.type). Used by _on_depsgraph_update to decide
    whether it's safe to recompute immediately or must defer - see
    _deferred_recompute below.
    """
    desired_type = "MESH" if obj.cmet_settings.result_type == "MESH" else "CURVES"
    return obj.type != desired_type


def _deferred_recompute(name):
    """One-shot bpy.app.timers callback: performs the actual rebuild
    (_ensure_result_type's delete+recreate Object) and recompute for a
    tracked object whose Output Type was just changed.

    This can only safely run outside bpy.app.handlers.depsgraph_update_post.
    Deleting/creating an Object is a structural scene edit - exactly what
    Blender's API docs warn against doing from inside that handler (unlike
    the data-block/attribute writes _recompute_and_write already did there
    before this rebuild path existed, which is an accepted, much smaller
    risk). _on_depsgraph_update detects the pending type mismatch via
    _needs_result_type_rebuild() and defers here via bpy.app.timers.register
    instead of calling _safe_recompute() directly, so the actual rebuild
    always happens on its own timer tick, never mid-depsgraph-evaluation.
    """
    obj = bpy.data.objects.get(name)
    if obj is None or not obj.cmet_settings.is_cmet_generator:
        return None
    _safe_recompute(obj, bpy.context.evaluated_depsgraph_get())
    return None


def _recompute_or_defer(obj, depsgraph):
    """The only call this module's depsgraph_update_post handler makes -
    routes to a deferred timer when a rebuild is needed (see
    _deferred_recompute), otherwise runs _safe_recompute() immediately as
    before (data-block/attribute writes only in that case, no Object
    add/remove)."""
    if _needs_result_type_rebuild(obj):
        name = obj.name
        bpy.app.timers.register(lambda: _deferred_recompute(name), first_interval=0.0)
        return
    _safe_recompute(obj, depsgraph)


def _safe_recompute(obj, depsgraph):
    """Runs _ensure_result_type() up front (outside the try/except) so that,
    on an Output Type switch, the rebuild itself - and the resulting new
    object reference - is settled before anything exception-prone runs.
    Always returns the object callers should use from here on: the original
    obj if no rebuild happened, otherwise the replacement _ensure_result_type
    built (the original obj may already be removed from bpy.data by then, so
    every caller MUST switch to using the returned reference, not the one it
    passed in).
    """
    obj = _ensure_result_type(obj)
    try:
        _recompute_and_write(obj, depsgraph)
    except Exception as e:
        obj.cmet_settings.last_status = f"ERROR: {e}"
        print(f"[constant_mass_emission_generator] '{obj.name}' failed to update: {e}")
    return obj


# ---------------------------------------------------------------------------
#  Operators
# ---------------------------------------------------------------------------

def _new_generator_data(name, result_type):
    # Deliberately left with zero points/vertices - a placeholder until the
    # first real recompute (_recompute_and_write always rebuilds when the
    # existing data's point count doesn't match k_max, which an empty
    # placeholder never will once a source object is actually set).
    if result_type == "MESH":
        data = bpy.data.meshes.new(name=name)
        data.from_pydata([], [], [])
        data.update()
    else:
        data = bpy.data.hair_curves.new(name=name)
    return data


class OBJECT_OT_cmet_add(bpy.types.Operator):
    """Add a new Constant-Mass Emission Generator object - a dedicated,
    self-updating object type, not a behavior you can bolt onto an arbitrary
    existing one"""
    bl_idname = "object.cmet_add"
    bl_label = "Constant-Mass Emission Generator"
    bl_options = {"REGISTER", "UNDO"}

    object_name: bpy.props.StringProperty(name="Name", default="CMET_Generator")
    # NOT a PointerProperty(type=bpy.types.Object) - Operator properties don't
    # support ID-datablock pointers in Blender 5.3: registering one silently
    # aborts registration of every property declared after it (confirmed via
    # bpy.ops.object.cmet_add.get_rna_type().properties.keys() showing only
    # ['rna_type', 'object_name'] - everything from this property onward was
    # missing). A PropertyGroup attached to an Object (CMET_Settings below)
    # doesn't have this restriction, so the real reference is resolved by
    # name here and stored properly on cmet_settings.source_object instead.
    source_object_name: bpy.props.StringProperty(
        name="Source Object",
        description="Object (Mesh or Curves) whose point attributes hold the time series",
    )
    x_attribute: bpy.props.StringProperty(name="Time Attribute", default="x")
    y_attribute: bpy.props.StringProperty(name="Mass Attribute", default="y")
    mass_interval: bpy.props.FloatProperty(name="Mass Per Sphere", default=1.0, min=0.0)
    num_spheres: bpy.props.IntProperty(name="Total Spheres", default=100, min=0)
    result_type: bpy.props.EnumProperty(
        name="Output Type",
        items=(
            ("MESH", "Mesh", "Represent this object as a Mesh, one vertex per emitted sphere"),
            ("CURVES", "Curves", "Represent this object as a single-spline Curves, one point per emitted sphere"),
        ),
        default="CURVES",
    )
    auto_update: bpy.props.BoolProperty(name="Auto-Update", default=True)
    debug: bpy.props.BoolProperty(name="Debug Output", default=False)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "object_name")
        layout.prop_search(self, "source_object_name", bpy.data, "objects", text="Source Object")
        layout.prop(self, "x_attribute")
        layout.prop(self, "y_attribute")
        layout.prop(self, "mass_interval")
        layout.prop(self, "num_spheres")
        layout.prop(self, "result_type")
        layout.prop(self, "auto_update")
        layout.prop(self, "debug")

    def execute(self, context):
        src = bpy.data.objects.get(self.source_object_name.strip()) if self.source_object_name.strip() else None
        if self.source_object_name.strip() and src is None:
            self.report({"WARNING"}, f"No object named '{self.source_object_name}' - leaving Source Object unset")

        data = _new_generator_data(self.object_name, self.result_type)
        obj = bpy.data.objects.new(self.object_name, data)
        context.collection.objects.link(obj)

        settings = obj.cmet_settings
        settings.is_cmet_generator = True
        settings.source_object = src
        settings.x_attribute = self.x_attribute
        settings.y_attribute = self.y_attribute
        settings.mass_interval = self.mass_interval
        settings.num_spheres = self.num_spheres
        settings.result_type = self.result_type
        settings.auto_update = self.auto_update
        settings.debug = self.debug

        context.view_layer.objects.active = obj
        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)

        if settings.source_object is not None:
            # obj.type already matches settings.result_type here (data was
            # just built from self.result_type above), so _safe_recompute's
            # internal _ensure_result_type() call is a guaranteed no-op on
            # this fresh object - but it always returns the object callers
            # should use from here on, so rebind obj/settings from it anyway
            # rather than assuming that invariant from the caller's side.
            obj = _safe_recompute(obj, context.evaluated_depsgraph_get())
            settings = obj.cmet_settings
            if settings.auto_update:
                _TRACKED.add(obj.name)
            if settings.last_status.startswith("ERROR"):
                self.report({"WARNING"}, settings.last_status)
        else:
            settings.last_status = "No source object set"

        return {"FINISHED"}


class OBJECT_OT_cmet_generate(bpy.types.Operator):
    """Compute now, and start (or refresh) automatic tracking of the source object"""
    bl_idname = "object.cmet_generate"
    bl_label = "Generate / Update Now"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.cmet_settings.is_cmet_generator

    def execute(self, context):
        obj = context.active_object
        settings = obj.cmet_settings
        if settings.mode == "COUNTRY_PAIR":
            if settings.raw_data_object is None or settings.country_a_object is None or settings.country_b_object is None:
                self.report({"ERROR"}, "Set Raw Data Object, Country A, and Country B first")
                return {"CANCELLED"}
        elif settings.source_object is None:
            self.report({"ERROR"}, "Set a Source Object first")
            return {"CANCELLED"}

        # _safe_recompute may delete and replace obj entirely (an Output Type
        # switch since the last run - Object.type is fixed at creation, so
        # that can't be done via a `.data` swap). It always returns the
        # object to keep using, which - unlike the stale obj this method
        # started with - is guaranteed to still be a live bpy.data.objects
        # entry; use that from here on, and make it the active/selected
        # object again since the original one it replaced no longer exists.
        obj = _safe_recompute(obj, context.evaluated_depsgraph_get())
        context.view_layer.objects.active = obj
        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)

        if obj.cmet_settings.auto_update:
            _TRACKED.add(obj.name)
        status = obj.cmet_settings.last_status
        if status.startswith("ERROR"):
            self.report({"ERROR"}, status)
            return {"CANCELLED"}

        self.report({"INFO"}, status)
        return {"FINISHED"}


class OBJECT_OT_cmet_stop_tracking(bpy.types.Operator):
    """Stop automatically recomputing this object when its source changes"""
    bl_idname = "object.cmet_stop_tracking"
    bl_label = "Stop Live Tracking"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.name in _TRACKED

    def execute(self, context):
        _TRACKED.discard(context.active_object.name)
        context.active_object.cmet_settings.auto_update = False
        return {"FINISHED"}


# ---------------------------------------------------------------------------
#  UI
# ---------------------------------------------------------------------------

class OBJECT_PT_cmet_panel(bpy.types.Panel):
    bl_label = "Constant-Mass Emission Times"
    bl_idname = "OBJECT_PT_cmet_panel"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.cmet_settings.is_cmet_generator

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        settings = obj.cmet_settings

        layout.prop(settings, "mode")
        if settings.mode == "COUNTRY_PAIR":
            layout.prop(settings, "raw_data_object")
            layout.prop(settings, "country_a_object")
            layout.prop(settings, "country_b_object")
            layout.prop(settings, "code_property_name")
            layout.prop(settings, "reporter_attribute_name")
            layout.prop(settings, "partner_attribute_name")
            row = layout.row(align=True)
            row.prop(settings, "item_code_attribute")
            row.prop(settings, "item_code_value")
            row = layout.row(align=True)
            row.prop(settings, "element_code_attribute")
            row.prop(settings, "element_code_value")
        else:
            layout.prop(settings, "source_object")

        layout.prop(settings, "x_attribute")
        layout.prop(settings, "y_attribute")
        row = layout.row(align=True)
        row.prop(settings, "mass_interval")
        row.prop(settings, "num_spheres")
        layout.prop(settings, "result_type")
        layout.prop(settings, "auto_update")
        layout.prop(settings, "debug")

        row = layout.row(align=True)
        row.operator("object.cmet_generate", icon="PLAY")
        row.operator("object.cmet_stop_tracking", icon="X", text="")

        is_tracked = obj.name in _TRACKED
        info = layout.box()
        info.label(
            text=f"Live tracking: {'ON' if is_tracked else 'off'}",
            icon="CHECKMARK" if is_tracked else "PAUSE",
        )
        if settings.last_status:
            icon = "ERROR" if settings.last_status.startswith("ERROR") else "INFO"
            info.label(text=settings.last_status, icon=icon)


class NODE_PT_cmet_add_panel(bpy.types.Panel):
    """One-click "Add Constant-Mass Emission Generator" from inside the
    Geometry Nodes editor itself. object.cmet_add is registered in the 3D
    Viewport's Add menu (VIEW3D_MT_add) - a completely different menu system
    than this editor's own Shift+A Add menu, which only ever adds *nodes* to
    the tree being edited, never scene objects (a node tree has no mechanism
    to spawn a new top-level Object - it only produces geometry flowing
    through sockets). This sidebar button is the node-editor-side
    equivalent, so adding one doesn't require switching to the viewport.
    """
    bl_label = "Constant-Mass Emission"
    bl_idname = "NODE_PT_cmet_add_panel"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Data Viz"

    @classmethod
    def poll(cls, context):
        return getattr(context.space_data, "tree_type", None) == "GeometryNodeTree"

    def draw(self, context):
        self.layout.operator(OBJECT_OT_cmet_add.bl_idname, icon="PARTICLES")


# ---------------------------------------------------------------------------
#  The auto-update handler
# ---------------------------------------------------------------------------

@persistent
def _on_depsgraph_update(scene, depsgraph):
    if not _TRACKED:
        return

    # update.id.original points back to the original data-block when update.id
    # is an evaluated copy - but when update.id is ALREADY the original, its
    # own .original is None (not "missing"), so a plain getattr(..., default)
    # fallback would silently collect None instead of update.id itself.
    changed_ids = set()
    for update in depsgraph.updates:
        original = update.id.original if update.id.original is not None else update.id
        changed_ids.add(original)

    # Reuse the depsgraph this handler was already given, rather than calling
    # context.evaluated_depsgraph_get() again from inside a
    # depsgraph_update_post callback - forcing a fresh evaluation while
    # Blender is still finishing this one is exactly the kind of thing the
    # API docs warn is unsafe inside this handler, and re-entrant depsgraph
    # evaluation here is a plausible way to make Blender hang.
    for name in list(_TRACKED):
        obj = bpy.data.objects.get(name)
        if obj is None:
            _TRACKED.discard(name)
            continue
        settings = obj.cmet_settings
        if not settings.is_cmet_generator or not settings.auto_update:
            _TRACKED.discard(name)
            continue

        # obj itself shows up here when cmet_settings changes (source object
        # repointed to a different object entirely, x_attribute/y_attribute
        # edited, mass_interval/num_spheres tweaked, country A/B swapped,
        # etc.) - those live as RNA properties on the Object ID, separate
        # from any watched object's own data. Only checking the watched
        # objects would miss "change which object(s) this points at"
        # entirely, since nothing about the newly-pointed-to object itself
        # changed in that same depsgraph tick.
        settings_changed = obj in changed_ids

        if settings.mode == "COUNTRY_PAIR":
            watched = [w for w in (settings.raw_data_object, settings.country_a_object, settings.country_b_object) if w is not None]
            if not watched:
                continue
            data_changed = any(
                w in changed_ids or (w.data is not None and w.data in changed_ids) for w in watched
            )
            if settings_changed or data_changed:
                _recompute_or_defer(obj, depsgraph)
        else:
            src = settings.source_object
            if src is None:
                continue
            if settings_changed or src in changed_ids or (src.data is not None and src.data in changed_ids):
                _recompute_or_defer(obj, depsgraph)


@persistent
def _rebuild_tracked_registry(dummy=None):
    """After loading a .blend, _TRACKED (plain process memory) is empty -
    rebuild it once from whichever objects already have auto_update enabled
    and a source object set, mirroring chezmoi_sync.py's own load_post
    pattern for restoring non-persistent state.
    """
    _TRACKED.clear()
    for obj in bpy.data.objects:
        settings = getattr(obj, "cmet_settings", None)
        if not settings or not settings.is_cmet_generator or not settings.auto_update:
            continue
        if settings.mode == "COUNTRY_PAIR":
            configured = (
                settings.raw_data_object is not None
                and settings.country_a_object is not None
                and settings.country_b_object is not None
            )
        else:
            configured = settings.source_object is not None
        if configured:
            _TRACKED.add(obj.name)


# ---------------------------------------------------------------------------
#  Register / unregister
# ---------------------------------------------------------------------------

_CLASSES = (
    CMET_Settings,
    OBJECT_OT_cmet_add,
    OBJECT_OT_cmet_generate,
    OBJECT_OT_cmet_stop_tracking,
    OBJECT_PT_cmet_panel,
    NODE_PT_cmet_add_panel,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.cmet_settings = bpy.props.PointerProperty(type=CMET_Settings)

    dv_menu.add_entry(
        OBJECT_OT_cmet_add.bl_idname,
        text="Constant-Mass Emission Generator",
        icon="PARTICLES",
    )
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    bpy.app.handlers.load_post.append(_rebuild_tracked_registry)
    # NOT called directly here - bpy.data is a restricted proxy (_RestrictData)
    # for the whole duration of register(), since the file isn't actually
    # loaded yet at this point; bpy.data.objects raises AttributeError on it.
    # load_post covers the normal "open a .blend" case; this timer covers
    # register() running with a file already open (e.g. after Reload
    # Scripts), by which point the restriction has lifted - the Script
    # Runner extension uses the same deferred-timer pattern for the
    # identical problem.
    bpy.app.timers.register(_rebuild_tracked_registry, first_interval=1.0)


def unregister():
    if _rebuild_tracked_registry in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_rebuild_tracked_registry)
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    dv_menu.remove_entry(OBJECT_OT_cmet_add.bl_idname)

    del bpy.types.Object.cmet_settings
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    _TRACKED.clear()

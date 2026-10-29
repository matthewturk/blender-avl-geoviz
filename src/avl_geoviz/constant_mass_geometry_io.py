"""Shared evaluated-geometry point-attribute reading for the constant-mass
emission-time tools (constant_mass_emission_times.py,
constant_mass_emission_generator.py). Needs bpy - unlike constant_mass_core.
py, which is deliberately bpy-free.

Geometry Nodes can output mesh, curves, and point-cloud components *side by
side* - e.g. a Cube whose modifier keeps the base mesh untouched while also
building separate curve geometry (joined, or just not replacing the
original). Reading obj.data directly never sees any of this (see
curve_to_csv.py's docstring for why evaluated_geometry() is required at
all), and even once you're reading evaluated geometry, blindly preferring
one component over another (mesh over curves, say) can silently read the
wrong - or a nonexistent - attribute instead of raising a clear error. This
happened for real: a Cube's GN modifier produced a Curves component holding
a "Year" attribute, but a mesh component (the unmodified base Cube) was also
present, and preferring `geometry_set.mesh or geometry_set.curves` picked
the mesh and reported only its own builtin attributes as "available".
read_point_attribute searches every present component instead.
"""

import bpy
import numpy as np
import databpy as db


class _StaticGeometrySet:
    """Duck-types bpy.types.GeometrySet's mesh/curves/pointcloud/instances
    interface, but wraps an object's own un-evaluated base data directly -
    used as a fallback for objects with no Geometry Nodes modifier, whose
    "evaluated geometry" is identical to their base data anyway (no modifier
    means nothing to evaluate). Only correct for that case - see
    evaluated_geometry_set below for when this is and isn't used.
    """

    def __init__(self, data):
        self.mesh = data if isinstance(data, bpy.types.Mesh) else None
        self.curves = data if isinstance(data, bpy.types.Curves) else None
        self.pointcloud = data if isinstance(data, bpy.types.PointCloud) else None
        self.instances = None


def evaluated_geometry_set(obj, depsgraph):
    """The evaluated GeometrySet for `obj`, with a fallback for objects that
    can't actually be evaluated by the depsgraph at all.

    Blender's depsgraph evaluates a minimal working set based on what's
    actually needed for display/render/explicit node dependencies - an
    object hidden via "Disable in Viewports" (hide_viewport=True) with
    nothing depending on it may never get its geometry evaluated, and
    .evaluated_geometry() then raises "Object geometry is not yet evaluated,
    is the depsgraph evaluated?". This is a real, deliberate setup for huge
    datasets that would otherwise make the viewport unusable (see
    hdf5_to_curves.py) - hiding a many-hundred-thousand-point Curves object
    to keep the viewport usable shouldn't also make it unreadable from
    Python.

    For an object with NO Geometry Nodes modifier, "evaluated geometry" and
    "base data" are the same thing by construction (there's nothing to
    evaluate), so falling back to obj.data directly is exactly correct, not
    an approximation. For an object that DOES have a GN modifier, this
    distinction matters - its evaluated output can genuinely differ from
    its base data - so that case still requires real depsgraph evaluation
    and raises a clearer error instead of silently reading the wrong thing.
    """
    try:
        evaluated_object = depsgraph.id_eval_get(obj)
        return evaluated_object.evaluated_geometry()
    except Exception as e:
        if "not yet evaluated" not in str(e):
            raise

        has_gn_modifier = any(m.type == "NODES" for m in obj.modifiers)
        if has_gn_modifier:
            raise RuntimeError(
                f"'{obj.name}' has a Geometry Nodes modifier, so its "
                f"evaluated output can genuinely differ from its base data - "
                f"it needs to actually be evaluated by the depsgraph (not "
                f"hidden via 'Disable in Viewports' with nothing else "
                f"depending on it), not just read as-is. Original error: {e}"
            ) from e

        return _StaticGeometrySet(obj.data)


def _components(geometry_set):
    return (
        ("mesh", geometry_set.mesh),
        ("curves", geometry_set.curves),
        ("pointcloud", geometry_set.pointcloud),
    )


def read_point_attribute(obj, depsgraph, name):
    geometry_set = evaluated_geometry_set(obj, depsgraph)

    candidates = []
    for comp_name, data in _components(geometry_set):
        if data is None:
            continue
        attr = data.attributes.get(name)
        if attr is not None and attr.domain == "POINT":
            candidates.append((comp_name, attr))

    if not candidates:
        available = []
        for comp_name, data in _components(geometry_set):
            if data is not None:
                available.extend(f"{comp_name}:{n}" for n in sorted(data.attributes.keys()))

        hint = ""
        # GeometrySet also exposes an Instances component - getattr'd
        # defensively since its exact availability/name isn't something to
        # assume across Blender versions. Un-realized instance geometry (e.g.
        # from "Instance on Points" with no "Realize Instances" before the
        # final Group Output) never shows up as a Mesh/Curves/PointCloud
        # component here at all, which looks identical to "the attribute
        # doesn't exist" - this at least tells you which one it actually is.
        instances = getattr(geometry_set, "instances", None)
        if instances is not None:
            hint = (
                " Note: this object's evaluated geometry also has an Instances "
                "component, which isn't searched (un-realized instance data can't "
                "be read as plain point attributes this way). If your curve "
                "geometry comes from something like 'Instance on Points', add a "
                "'Realize Instances' node before the final Group Output."
            )

        raise ValueError(f"'{obj.name}' has no POINT attribute '{name}'. Available: {available}.{hint}")

    if len(candidates) > 1:
        comp_names = [c[0] for c in candidates]
        raise ValueError(
            f"'{obj.name}' has a POINT attribute '{name}' on more than one geometry "
            f"component ({comp_names}) - ambiguous which one to read"
        )

    _, attr = candidates[0]
    return db.Attribute(attr).as_array().astype(np.float64)


# ---------------------------------------------------------------------------
#  Multi-spline "one curve per reporter country" reading - shared between
#  constant_mass_emission_generator.py's Country Pair Filter mode and
#  country_pair_batch_emitter.py, which both filter the same kind of raw
#  dataset (one spline per reporter, per hdf5_to_curves.py's group-per-curve
#  pattern) down to a single reporter/partner/item/element match.
# ---------------------------------------------------------------------------

def match_code(a, b):
    """Country/item/element codes may be stored as int, or as zero-padded/
    plain strings, inconsistently between an object's own custom property
    and a dataset's attribute values - compare numerically first (so "004"
    matches 4), falling back to string equality only for genuinely
    non-numeric codes.
    """
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def match_code_array(arr, target):
    """Vectorized match_code: compare a whole array against one target code
    in a single numpy operation, instead of one Python-level match_code()
    call (with its own int()/except overhead) per element. This matters a
    lot when called once per candidate country pair, each time against an
    array with potentially thousands of points - a per-element Python loop
    here is the actual bottleneck in a full pairwise scan, not the PCHIP
    solving.

    Numeric-dtype arrays (the expected case for country/item/element code
    columns) get a single vectorized `==`. Only a string/object-dtype array
    falls back to a per-element loop - still correct, just not vectorized,
    for the presumably rare case of non-numeric codes.
    """
    arr = np.asarray(arr)
    if np.issubdtype(arr.dtype, np.number):
        try:
            return arr == float(target)
        except (TypeError, ValueError):
            return np.zeros(arr.shape, dtype=bool)
    return np.array([match_code(v, target) for v in arr])


def read_full_point_attribute(data, obj_name, name):
    """Read a named POINT-domain attribute's full array from an
    already-obtained geometry component (e.g. curves_data) - simpler than
    read_point_attribute() above since the caller already knows which
    component to read, no mesh/curves/pointcloud ambiguity to resolve.
    """
    attr = data.attributes.get(name)
    if attr is None:
        raise ValueError(f"'{obj_name}' has no attribute '{name}'. Available: {sorted(data.attributes.keys())}")
    if attr.domain != "POINT":
        raise ValueError(f"Attribute '{name}' on '{obj_name}' is on the {attr.domain} domain, not POINT")
    if attr.data_type == "STRING":
        return np.array([
            item.value.decode("utf-8") if isinstance(item.value, bytes) else str(item.value)
            for item in attr.data
        ])
    return db.Attribute(attr).as_array()


def read_curve_domain_codes(curves_data, obj_name, attribute_name):
    """Read a CURVE-domain (one value per spline) attribute's full array, in
    curve order - e.g. hdf5_to_curves.py's "group_name" CURVE attribute is
    the same access pattern (attr.data indexed by domain-element count,
    which for CURVE domain means by spline, not by point).
    """
    attr = curves_data.attributes.get(attribute_name)
    if attr is None:
        raise ValueError(
            f"'{obj_name}' has no CURVE attribute '{attribute_name}'. "
            f"Available: {sorted(curves_data.attributes.keys())}"
        )
    if attr.domain != "CURVE":
        raise ValueError(f"'{attribute_name}' on '{obj_name}' is on the {attr.domain} domain, not CURVE")
    if attr.data_type == "STRING":
        return [
            item.value.decode("utf-8") if isinstance(item.value, bytes) else str(item.value)
            for item in attr.data
        ]
    return db.Attribute(attr).as_array().tolist()


def find_matching_spline_point_range(curves_data, obj_name, reporter_attribute_name, reporter_code, spline_codes=None, log=lambda msg: None):
    """Find which spline in a multi-spline Curves object belongs to a given
    reporter code (a CURVE-domain attribute, one value per spline), then
    return that spline's (start, length) point-index range.

    Pass `spline_codes` (already read via read_curve_domain_codes) when
    matching many reporters against the same curves_data in a loop, to avoid
    re-reading the same CURVE attribute on every call.

    The CURVE-domain attribute read is a confirmed, established pattern in
    this codebase (see read_curve_domain_codes). The spline-to-point-range
    mapping (Curves.curves[i].first_point_index/.points_length) is NOT
    independently verified against a live Blender session here - if this
    raises, or if the sanity check below trips, that assumption is the first
    thing to re-examine.
    """
    if spline_codes is None:
        spline_codes = read_curve_domain_codes(curves_data, obj_name, reporter_attribute_name)

    matches = [i for i, code in enumerate(spline_codes) if match_code(code, reporter_code)]
    if not matches:
        raise ValueError(f"No spline in '{obj_name}' has '{reporter_attribute_name}' matching reporter code {reporter_code!r}")
    if len(matches) > 1:
        raise ValueError(f"{len(matches)} splines in '{obj_name}' match reporter code {reporter_code!r} - expected exactly one")
    spline_index = matches[0]

    total_curves = len(curves_data.curves)
    if spline_index >= total_curves:
        raise RuntimeError(f"Matched spline index {spline_index} out of range ('{obj_name}' has {total_curves} splines)")

    curve_slice = curves_data.curves[spline_index]
    start = curve_slice.first_point_index
    length = curve_slice.points_length
    log(f"reporter code {reporter_code!r} -> spline {spline_index}, points [{start}, {start + length})")

    total_points = len(curves_data.points)
    if start < 0 or length < 0 or start + length > total_points:
        raise RuntimeError(
            f"Computed point range [{start}, {start + length}) falls outside "
            f"'{obj_name}'s actual point count ({total_points}) - the assumed "
            f"Curves.curves[i].first_point_index/.points_length API may not "
            f"mean what this code assumes here. Report this back with your "
            f"Blender version so the assumption can be fixed."
        )
    return start, length

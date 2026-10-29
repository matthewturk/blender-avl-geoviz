# Batch counterpart to constant_mass_emission_generator.py's Country Pair
# Filter mode: instead of one object per country-pair connection, this
# builds ONE multi-spline Curves object holding an emission-time curve for
# EVERY ordered pair among a set of countries that actually has matching
# data for a given item/element code - reusing hdf5_to_curves.py's own
# "one group -> one curve, combined, with a CURVE-domain identifying
# attribute" pattern (see its "Group Names" node group trick, mirrored here
# as "Pair Names", since Geometry Nodes can't read STRING attributes
# directly).
#
# Safety - five independent caps, each bounding a DIFFERENT stage, since a
# real crash showed that any one cap alone isn't enough (they can multiply):
#   1. max_countries (N) - hard cap on the Countries collection's size,
#      checked FIRST, before any data is read at all. Also bounds the raw
#      N*(N-1) possible ordered pairs.
#   2. max_reporters (N) - "Top Exporters" mode only: how many of the
#      biggest-total-export reporters get kept.
#   3. max_partners_per_reporter (M) - how many partner curves EACH kept
#      reporter may contribute (ranked by that pair's own mass). In "Top
#      Exporters" mode, total curves can never exceed max_reporters x this,
#      guaranteed - this is the literal "N x M" bound.
#   4. max_pairs - "Top Pairs by Mass" mode only: global cap on how many
#      pairs (across all reporters) get solved.
#   5. max_spheres_per_pair / max_total_spheres - bound the actual output
#      size: a per-pair cap, and a hard running budget on the SUM across
#      the whole object. This second one matters even when every other cap
#      looks reasonable alone - e.g. 500 pairs x 50,000 spheres each is 25
#      million points in one Curves object.
#
# A SEPARATE, previously-real bug (now fixed) could exhaust memory
# regardless of any of the above: reading a POINT-domain attribute's full
# array (read_full_point_attribute) copies the ENTIRE dataset every time
# it's called - if called once per reporter (5 attributes x N reporters)
# instead of once total, that's 5xN full copies of the whole dataset before
# any of the caps above even come into play. Fixed by reading each
# attribute's full array exactly once, up front, and slicing (a cheap numpy
# view) per reporter from that single cached copy.
#
# This runs in stages so it degrades gracefully instead of hard-failing
# outright:
#   - Scan: for every reporter with a valid code, find its spline once and
#     mask by item/element code once, summing its own total export mass -
#     no PCHIP solving yet. This is what ranks/restricts which reporters
#     get expanded to full per-partner detail next, in "Top Exporters" mode
#     - so the expensive per-partner expansion only ever runs for reporters
#     actually kept.
#   - Expand partners: for each kept reporter, scan every partner, rank
#     THAT reporter's own matching partners by pair mass, and keep only the
#     top max_partners_per_reporter (M) of them.
#   - Rank: sort every pair that had matching data by total mass,
#     descending - in "Top Pairs" mode this also truncates to max_pairs; in
#     "Top Exporters" mode nothing is truncated here (the sort just means
#     bigger pairs are tried first if the total-spheres budget runs out).
#   - Dry run (optional): estimate each kept pair's sphere count (the exact
#     total_mass // Mass Per Sphere formula the real solver uses) without
#     running the actual solver or writing anything - see dry_run below.
#   - Solve (expensive): run the real PCHIP computation only for the kept
#     pairs, in descending-mass order, checking the max_total_spheres
#     running budget before each one. A pair that would blow the per-pair
#     cap OR push the running total over budget is skipped (logged) rather
#     than solved anyway - later, smaller pairs are still tried in case
#     they fit what's left of the budget.
#
# Mass control is deliberately a single, fixed "Mass Per Sphere" - NOT a
# per-pair sphere count - shared across every pair in the batch. Different
# pairs have wildly different total trade volumes; a fixed sphere *count*
# per pair (which the per-connection CMET defaults to) would make a huge-
# volume pair and a tiny one look equally dense, misrepresenting relative
# scale. A fixed mass-per-sphere means bigger pairs naturally get more
# spheres, which is the point of this whole representation.
#
# Uses the same "new class of object" scoping discipline as
# constant_mass_emission_generator.py: is_cpbe_generator gates the panel and
# the depsgraph-tracking registry, and the only way to set it is the
# dedicated Add-menu operator below.
#
# Performance: the actual computation lives in _recompute_steps, a
# generator that yields a progress fraction (0..1) periodically instead of
# running start-to-finish in one call. OBJECT_OT_cpbe_generate ("Generate /
# Update Now") is a modal operator that drives this generator through a
# timer, processing a time-boxed chunk per tick so Blender's UI stays
# responsive, updating a real progress bar (Window Manager progress_begin/
# update/end), and cancellable with Esc mid-run. Callers that can't be
# modal (the depsgraph auto-update handler, the Add operator's initial
# compute) just drain the same generator in one go via
# _run_recompute_sync - same logic either way, no duplicated business
# rules between the interactive and automatic paths. The country/item/
# element code matching itself is vectorized (constant_mass_geometry_io.
# match_code_array) rather than a Python-level per-point loop, which is
# where nearly all the wall-clock time was actually going - measured at
# roughly 500x faster on a 200k-point array.

import time

import bpy
import numpy as np
import databpy as db
from bpy.app.handlers import persistent

from . import constant_mass_core as core
from . import constant_mass_geometry_io as geo
from . import _attr_io_shared as _attrs
from . import data_visualization_menu as dv_menu

_TRACKED = set()


# ---------------------------------------------------------------------------
#  Settings
# ---------------------------------------------------------------------------

class CPBE_Settings(bpy.types.PropertyGroup):
    is_cpbe_generator: bpy.props.BoolProperty(
        name="Is Country Pair Batch Emitter",
        description="Internal marker - only set by Add > Data Visualization > Country Pair Batch Emitter",
        default=False,
    )
    raw_data_object: bpy.props.PointerProperty(
        name="Raw Data Object",
        description="The big, unfiltered multi-reporter Curves dataset (one spline per reporter country)",
        type=bpy.types.Object,
    )
    countries_collection: bpy.props.PointerProperty(
        name="Countries",
        description=(
            "Collection of country wireframe objects - every ordered pair "
            "among them that has matching data gets its own curve"
        ),
        type=bpy.types.Collection,
    )
    code_property_name: bpy.props.StringProperty(
        name="Code Property", default="m49",
        description="Custom property on each country object holding its own country code",
    )
    reporter_attribute_name: bpy.props.StringProperty(
        name="Reporter Attribute", default="m49",
        description="CURVE-domain attribute on Raw Data Object identifying which spline belongs to which reporter",
    )
    partner_attribute_name: bpy.props.StringProperty(name="Partner Attribute", default="Partner Country Code")
    item_code_attribute: bpy.props.StringProperty(name="Item Code Attribute", default="Item Code")
    element_code_attribute: bpy.props.StringProperty(name="Element Code Attribute", default="Element Code")
    item_code_value: bpy.props.IntProperty(name="Item Code", default=0)
    element_code_value: bpy.props.IntProperty(name="Element Code", default=0)
    x_attribute: bpy.props.StringProperty(name="Time Attribute", default="Year")
    y_attribute: bpy.props.StringProperty(name="Mass Attribute", default="Value")
    mass_scaling_mode: bpy.props.EnumProperty(
        name="Mass Per Sphere Mode",
        items=(
            ("FRACTION_OF_TOTAL", "Fraction of Total (recommended)", (
                "Derive Mass Per Sphere automatically as Mass Fraction x the "
                "combined total mass of whichever pairs actually survive "
                "scanning/ranking/selection - scale-invariant, so the same "
                "fraction (e.g. 1e-6) gives a sane sphere count regardless "
                "of whether your data's total mass is in the thousands or "
                "billions. Computed fresh each run, logged so you can see "
                "the derived absolute value"
            )),
            ("ABSOLUTE", "Absolute Value", (
                "Use Mass Per Sphere directly as a fixed absolute mass - "
                "you have to know your data's actual scale to pick a good "
                "value, and re-tune it if that scale changes"
            )),
        ),
        default="FRACTION_OF_TOTAL",
    )
    mass_fraction: bpy.props.FloatProperty(
        name="Mass Fraction", default=1e-6, min=1e-12, max=1.0, precision=8,
        description=(
            "Only used in 'Fraction of Total' mode - Mass Per Sphere is "
            "derived as this fraction x the combined total mass of the "
            "pairs actually kept after scanning/ranking/selection. E.g. "
            "1e-6 means roughly 1 million spheres' worth of granularity "
            "spread across everything currently kept, biggest pairs getting "
            "proportionally more of them"
        ),
    )
    mass_interval: bpy.props.FloatProperty(
        name="Mass Per Sphere", default=1.0, min=0.0001,
        description=(
            "Only used in 'Absolute Value' mode - constant mass represented "
            "by each emitted sphere, the SAME value across every pair, so "
            "relative volume stays comparable between pairs. Tune this to "
            "your data's actual scale; see Max Spheres Per Pair below for "
            "what happens if it's too small"
        ),
    )
    selection_mode: bpy.props.EnumProperty(
        name="Selection Mode",
        items=(
            ("TOP_EXPORTERS", "Top Exporters, All Partners", (
                "Rank REPORTERS by their own total export mass (summed across "
                "every partner), keep only the top 'Max Reporters (N)', then "
                "include their partner pairs (up to 'Max Partners Per "
                "Reporter (M)' each, biggest first) - at most N x M curves "
                "in total, guaranteed"
            )),
            ("TOP_PAIRS", "Top Pairs by Mass", (
                "Scan every reporter's every partner, rank ALL resulting "
                "pairs by their own total mass, keep only the top 'Max "
                "Curves To Build' regardless of which reporters they belong to"
            )),
        ),
        default="TOP_EXPORTERS",
    )

    # ---- Stage 1 (checked first, before any data is read at all): bound N,
    # the size of the input. ----
    max_countries: bpy.props.IntProperty(
        name="Max Countries (N)", default=60, min=1,
        description=(
            "Hard cap on how many objects the Countries collection may hold - "
            "checked before any scanning starts, so a too-large collection "
            "fails immediately instead of triggering a long/memory-heavy scan. "
            "This also bounds the raw N*(N-1) possible ordered pairs. Narrow "
            "the collection, or raise this deliberately if you really do have "
            "more countries than it allows"
        ),
    )

    # ---- Stage 2 (Top Exporters mode only): bound N, how many reporters
    # get kept. ----
    max_reporters: bpy.props.IntProperty(
        name="Max Reporters (N)", default=3, min=1,
        description=(
            "Only used in 'Top Exporters, All Partners' mode - how many of "
            "the biggest-total-export reporters to keep. Each kept reporter "
            "then contributes up to 'Max Partners Per Reporter' curves"
        ),
    )

    # ---- Stage 3: bound M, how many partner curves each KEPT reporter may
    # contribute. ----
    max_partners_per_reporter: bpy.props.IntProperty(
        name="Max Partners Per Reporter (M)", default=60, min=1,
        description=(
            "Hard cap on how many partner curves EACH kept reporter may "
            "contribute - ranked by that pair's own mass, biggest first, if "
            "a reporter has more matching partners than this. In 'Top "
            "Exporters' mode, total curves built can never exceed Max "
            "Reporters x this value, guaranteed"
        ),
    )

    # ---- Stage 4 (Top Pairs mode only): bound how many pairs get solved,
    # globally. ----
    max_pairs: bpy.props.IntProperty(
        name="Max Curves To Build", default=500, min=1,
        description=(
            "Only used in 'Top Pairs by Mass' mode - of every pair that "
            "actually has matching data, only the top this many - ranked by "
            "total mass, biggest first - get solved and turned into curves"
        ),
    )

    # ---- Stage 5 (always applies): bound the actual OUTPUT size. ----
    max_spheres_per_pair: bpy.props.IntProperty(
        name="Max Spheres Per Pair", default=50_000, min=1,
        description=(
            "Safety cap - a pair whose own sphere count would exceed this is "
            "skipped (not allowed to grind through a huge solve) rather than "
            "hanging or aborting the whole batch"
        ),
    )
    max_total_spheres: bpy.props.IntProperty(
        name="Max Total Spheres", default=100_000, min=1,
        description=(
            "Hard cap on the SUM of spheres across every curve in the whole "
            "built object, checked before each pair is solved (stopping, not "
            "erroring, once hit - later, smaller pairs are still tried in "
            "case they fit the remaining budget). This bounds total memory "
            "directly, independent of every other cap above - N x M curves "
            "each up to Max Spheres Per Pair can still multiply to something "
            "enormous even when each individual cap looks reasonable"
        ),
    )

    dry_run: bpy.props.BoolProperty(
        name="Dry Run (Estimate Only)",
        description=(
            "Scan and rank exactly as a real run would (same reporter/"
            "partner selection, same safety caps), and ESTIMATE each kept "
            "pair's sphere count using the same total_mass // Mass Per "
            "Sphere formula the real solver uses internally - but never run "
            "the actual PCHIP solver and never write anything to this "
            "object. Use this to safely check how many curves/spheres a "
            "given set of parameters would produce before committing to a "
            "real run"
        ),
        default=False,
    )

    auto_update: bpy.props.BoolProperty(
        name="Auto-Update",
        description="Automatically recompute whenever the raw dataset or countries collection changes",
        default=True,
    )
    debug: bpy.props.BoolProperty(
        name="Debug Output",
        description="Print step-by-step progress, per-pair skip reasons, and a final summary to the system console",
        default=False,
    )
    last_status: bpy.props.StringProperty(name="Status", default="", options={"SKIP_SAVE"})


# ---------------------------------------------------------------------------
#  Write helpers (generic attribute writers live in _attr_io_shared.py)
# ---------------------------------------------------------------------------

def _codes_to_int(values, label):
    """Convert a list of country codes (possibly int, float, or str - e.g.
    a zero-padded "004") to plain Python ints, for storage as an INT
    CURVE-domain attribute. Raises a clear error naming which field failed
    if any value isn't numeric, rather than silently writing a nonsensical
    array or falling back to some other type.
    """
    try:
        return [int(v) for v in values]
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"Could not convert one or more '{label}' values to integers "
            f"({e}) - these need to be numeric country codes to store as "
            f"an INT attribute"
        )


def _build_pair_names_node_group(name, pair_names):
    """(Re)build a freestanding node group exposing `pair_names` as an
    ordered, per-curve field - identical trick to hdf5_to_curves.py's Group
    Names node group, since Named Attribute can't read STRING attributes.
    """
    from nodebpy import geometry as g

    existing = bpy.data.node_groups.get(name)
    if existing is not None:
        existing.interface.clear()
        existing.nodes.clear()

    with g.tree(existing or name) as tree:
        geometry = tree.inputs.geometry("Geometry")
        tree.outputs.geometry("Output") >> geometry

        pair_name_out = tree.outputs.string("Pair Name")
        pair_name_out._interface_socket.force_non_field = False

        string_node = tree.nodes.new("FunctionNodeInputString")
        string_node.name = "Pair Names"
        string_node.label = "Pair Names"
        string_node.string = "\n".join(pair_names)
        string_node.location = (-400, 0)

        special_chars = tree.nodes.new("FunctionNodeInputSpecialCharacters")
        special_chars.location = (-400, -150)

        split_node = tree.nodes.new("FunctionNodeSplitString")
        split_node.location = (-180, -50)
        tree.link(string_node.outputs[0], split_node.inputs[0])
        tree.link(special_chars.outputs[0], split_node.inputs[1])

        tree.link(split_node.outputs[0], pair_name_out.socket)

    node_group = bpy.data.node_groups[name]
    node_group.use_fake_user = True
    return node_group


# ---------------------------------------------------------------------------
#  Core recompute
# ---------------------------------------------------------------------------

def _write_curves_object(
    obj, curve_lengths, all_emission_times, all_cumulative_mass, all_sphere_index,
    reporter_codes, partner_codes, pair_names, country_a_positions, country_b_positions,
):
    """Build/replace obj's Curves data from accumulated per-pair results and
    write all the point/curve-domain attributes plus the Pair Names node
    group. Shared between _recompute_steps' normal end-of-run write and a
    cancelled-mid-run partial write (see OBJECT_OT_cpbe_generate.modal) -
    whatever was accumulated so far is exactly as valid an input here as a
    fully-completed run's results, just with fewer curves. Returns the total
    point count written.
    """
    total_points = int(sum(curve_lengths))
    zeros = np.zeros((total_points, 3), dtype=np.float32)

    old_data = obj.data
    new_data = bpy.data.hair_curves.new(name=obj.name)
    new_data.add_curves(curve_lengths)
    obj.data = new_data
    if old_data is not None and old_data.users == 0:
        if isinstance(old_data, bpy.types.Curves):
            bpy.data.hair_curves.remove(old_data)
        elif isinstance(old_data, bpy.types.Mesh):
            bpy.data.meshes.remove(old_data)

    db.store_named_attribute(
        obj, zeros, "position", atype=db.AttributeTypes.FLOAT_VECTOR, domain=db.AttributeDomains.POINT
    )
    _attrs.write_float_attribute(obj, "emission_time", np.concatenate(all_emission_times))
    _attrs.write_float_attribute(obj, "cumulative_mass", np.concatenate(all_cumulative_mass))
    _attrs.write_int_attribute(obj, "sphere_index", np.concatenate(all_sphere_index))

    _attrs.write_string_attribute(new_data, "pair_name", pair_names, domain="CURVE")
    _attrs.write_int_attribute(obj, "reporter_code", _codes_to_int(reporter_codes, "reporter_code"), domain=db.AttributeDomains.CURVE)
    _attrs.write_int_attribute(obj, "partner_code", _codes_to_int(partner_codes, "partner_code"), domain=db.AttributeDomains.CURVE)
    _attrs.write_vector_attribute(obj, "country_a_position", country_a_positions, domain=db.AttributeDomains.CURVE)
    _attrs.write_vector_attribute(obj, "country_b_position", country_b_positions, domain=db.AttributeDomains.CURVE)

    _build_pair_names_node_group(f"{obj.name} Pair Names", pair_names)
    return total_points


def _recompute_steps(obj, depsgraph, partial=None):
    """Generator form of the batch recompute: does the exact same work as a
    plain function would, but yields a progress fraction (0..1) periodically
    - after each reporter scanned in phase 1, and after each kept pair
    solved in phase 3 - so a caller can drive this incrementally (a modal
    operator processing a time-boxed chunk per timer tick, updating a
    progress bar and checking for Esc) instead of blocking Blender's UI
    thread for the whole computation in one call. _run_recompute_sync below
    just drains this generator in a tight loop for callers that can't be
    modal (the depsgraph auto-update handler, the Add operator).

    `partial`, if given, is a dict this generator populates (once, at the
    start of phase 3) with references to its own accumulator lists
    (curve_lengths, all_emission_times, etc.) - not copies, the SAME list
    objects phase 3 keeps appending to. A caller holding onto `partial` can
    therefore read the current in-progress state at any time (e.g. right
    after being interrupted by Esc) and pass it straight to
    _write_curves_object to keep whatever was already solved, instead of
    discarding it just because the run didn't reach the end.
    """
    settings = obj.cpbe_settings
    log = (lambda msg: print(f"[CPBE] '{obj.name}': {msg}")) if settings.debug else (lambda msg: None)

    raw_obj = settings.raw_data_object
    collection = settings.countries_collection
    if raw_obj is None or collection is None:
        settings.last_status = "Raw Data Object and Countries must both be set"
        return

    countries = list(collection.objects)
    n = len(countries)
    if n < 2:
        settings.last_status = f"Countries collection has {n} object(s) - need at least 2"
        return

    # Stage 1 safety check - bound N before ANY data is read. Checked here,
    # first, so a too-large collection fails immediately with the real
    # numbers instead of triggering a scan at all.
    if n > settings.max_countries:
        settings.last_status = (
            f"Countries collection has {n} objects, over Max Countries (N) = "
            f"{settings.max_countries} - narrow the collection, or raise "
            f"this deliberately if you really do have this many countries"
        )
        return

    total_pairs = n * (n - 1)
    mode_label = "Top Exporters, All Partners" if settings.selection_mode == "TOP_EXPORTERS" else "Top Pairs by Mass"
    log(f"{n} countries ({total_pairs} possible ordered pairs) - mode: {mode_label}" + (" [DRY RUN]" if settings.dry_run else ""))

    geometry_set = geo.evaluated_geometry_set(raw_obj, depsgraph)
    curves_data = geometry_set.curves
    if curves_data is None:
        settings.last_status = f"'{raw_obj.name}'s evaluated geometry has no Curves component"
        return

    reporter_attr = settings.reporter_attribute_name.strip()
    partner_attr = settings.partner_attribute_name.strip()
    item_attr = settings.item_code_attribute.strip()
    element_attr = settings.element_code_attribute.strip()
    x_attr = settings.x_attribute.strip()
    y_attr = settings.y_attribute.strip()
    code_prop = settings.code_property_name.strip()

    try:
        spline_codes = geo.read_curve_domain_codes(curves_data, raw_obj.name, reporter_attr)
    except ValueError as e:
        settings.last_status = f"ERROR: {e}"
        return

    # Read each attribute's FULL array exactly ONCE, here, before the
    # per-reporter loop - not once per reporter inside it. This was a real
    # bug: read_full_point_attribute copies the entire dataset's attribute
    # array from Blender into a fresh numpy array every time it's called: if
    # it's called per-reporter (5 attributes x N reporters), that's 5xN full
    # copies of the WHOLE dataset, regardless of how small each reporter's
    # own slice actually is - almost certainly the real cause of an out-of-
    # memory crash that every sphere-count safety cap downstream couldn't
    # have prevented, since it happens before any of those caps are even
    # reached. Slicing a cached array with [start:end] below is a cheap
    # numpy view, not a copy.
    partner_full = geo.read_full_point_attribute(curves_data, raw_obj.name, partner_attr)
    item_full = geo.read_full_point_attribute(curves_data, raw_obj.name, item_attr)
    element_full = geo.read_full_point_attribute(curves_data, raw_obj.name, element_attr)
    x_full_all = geo.read_full_point_attribute(curves_data, raw_obj.name, x_attr)
    y_full_all = geo.read_full_point_attribute(curves_data, raw_obj.name, y_attr)

    # ---- Scan (0% - 30% of progress): for every reporter with a valid
    # code, find its spline once and slice+mask its data once - cheap (one
    # spline lookup, a few array slices, vectorized comparisons) - and
    # compute the reporter's OWN total export mass (summed across every
    # partner). This ranks/restricts which reporters get expanded to full
    # per-partner detail below, in "Top Exporters" mode - so the expensive
    # per-partner expansion only ever runs for reporters actually being
    # kept, never for ones about to be discarded. ----
    reporter_info = {}  # str(reporter_code) -> None (no spline), or (a, reporter_code, partner_arr, x_full, y_full, item_element_mask, reporter_total_mass)
    n_no_code_reporters = 0

    for reporter_i, a in enumerate(countries):
        reporter_code = a.get(code_prop)
        if reporter_code is None:
            n_no_code_reporters += 1
            yield 0.3 * (reporter_i + 1) / n
            continue

        cache_key = str(reporter_code)
        if cache_key in reporter_info:
            yield 0.3 * (reporter_i + 1) / n
            continue

        try:
            start, length = geo.find_matching_spline_point_range(
                curves_data, raw_obj.name, reporter_attr, reporter_code,
                spline_codes=spline_codes, log=log,
            )
        except ValueError:
            reporter_info[cache_key] = None
        else:
            end = start + length
            # Attribute-name typos would already have failed above, at the
            # single upfront full-array read for each attribute - nothing
            # left here that could fail per-reporter for that reason.
            partner_arr = partner_full[start:end]
            item_arr = item_full[start:end]
            element_arr = element_full[start:end]
            x_full = x_full_all[start:end]
            y_full = y_full_all[start:end]
            item_element_mask = geo.match_code_array(item_arr, settings.item_code_value) & geo.match_code_array(element_arr, settings.element_code_value)
            reporter_total_mass = float(np.sum(y_full[item_element_mask]))
            reporter_info[cache_key] = (a, reporter_code, partner_arr, x_full, y_full, item_element_mask, reporter_total_mass)

        yield 0.3 * (reporter_i + 1) / n

    valid_reporters = [info for info in reporter_info.values() if info is not None]
    n_no_reporter_spline = sum(1 for info in reporter_info.values() if info is None)
    log(f"{len(valid_reporters)} of {n} countries have a matching reporter spline ({n_no_code_reporters} missing code, {n_no_reporter_spline} no spline)")

    if not valid_reporters:
        settings.last_status = (
            f"No reporters produced data (of {n} countries): "
            f"{n_no_code_reporters} missing code, {n_no_reporter_spline} no reporter spline"
        )
        return

    if settings.selection_mode == "TOP_EXPORTERS":
        valid_reporters.sort(key=lambda info: info[6], reverse=True)
        kept_reporters = valid_reporters[: settings.max_reporters]
        n_reporters_dropped = len(valid_reporters) - len(kept_reporters)
        # Unconditional (not gated behind Debug Output) - this is exactly the
        # "what is it actually doing" info that matters most, and it's
        # decided here, before the expensive per-partner expansion even
        # starts - worth seeing immediately, not just in a debug-only log or
        # buried in the final summary once everything else has also run.
        names_and_masses = ", ".join(f"{info[0].name} ({info[6]:.6g})" for info in kept_reporters)
        print(
            f"[CPBE] '{obj.name}': selected top {len(kept_reporters)} of "
            f"{len(valid_reporters)} exporters by total mass "
            f"({n_reporters_dropped} smaller reporters excluded), at most "
            f"{len(kept_reporters)} x {n - 1} = {len(kept_reporters) * (n - 1)} pairs: "
            f"{names_and_masses}"
        )
        settings.last_status = f"Selected exporters: {names_and_masses} - computing..."
    else:
        kept_reporters = valid_reporters
        n_reporters_dropped = 0

    # ---- Expand partners (30% - 55% of progress): expand each KEPT
    # reporter to full per-partner detail, one vectorized comparison per
    # partner (not a per-point Python loop - that was the actual
    # bottleneck: match_code_array is ~500x faster than calling match_code()
    # once per point in a list comprehension, measured on a 200k-point
    # array). No PCHIP here yet - that's the expensive part, deferred to the
    # solve stage below. Each reporter's own matching partners are ranked by
    # pair mass and truncated to Max Partners Per Reporter (M) HERE, per
    # reporter - not globally - so total curves in "Top Exporters" mode can
    # never exceed Max Reporters (N) x Max Partners Per Reporter (M),
    # guaranteed, regardless of how many partners any one reporter has. ----
    candidates = []  # dicts: a, b, reporter_code, partner_code, x, y, total_mass
    n_no_code = 0
    n_dropped_by_partner_cap = 0

    for kept_i, info in enumerate(kept_reporters):
        a, reporter_code, partner_arr, x_full, y_full, item_element_mask, _ = info
        reporter_candidates = []
        for b in countries:
            if b is a:
                continue
            partner_code = b.get(code_prop)
            if partner_code is None:
                n_no_code += 1
                continue

            mask = geo.match_code_array(partner_arr, partner_code) & item_element_mask
            if not mask.any():
                continue

            x = x_full[mask].astype(np.float64)
            y = y_full[mask].astype(np.float64)
            total_mass = float(np.sum(y))
            log(f"{a.name} -> {b.name}: {int(mask.sum())} rows, total_mass={total_mass:.6g}")
            reporter_candidates.append({
                "a": a, "b": b, "reporter_code": reporter_code, "partner_code": partner_code,
                "x": x, "y": y, "total_mass": total_mass,
            })

        reporter_candidates.sort(key=lambda c: c["total_mass"], reverse=True)
        kept_partner_candidates = reporter_candidates[: settings.max_partners_per_reporter]
        n_dropped_by_partner_cap += len(reporter_candidates) - len(kept_partner_candidates)
        candidates.extend(kept_partner_candidates)

        yield 0.3 + 0.25 * (kept_i + 1) / max(len(kept_reporters), 1)

    n_candidates = len(candidates)
    log(
        f"{n_candidates} pairs kept after per-reporter Max Partners Per "
        f"Reporter cap ({n_dropped_by_partner_cap} smaller partners dropped "
        f"by that cap)"
    )

    if n_candidates == 0:
        settings.last_status = (
            f"{len(kept_reporters)} reporter(s) kept but no partner pairs had "
            f"matching data ({n_no_code} missing partner code)"
        )
        return

    # ---- Phase 2: rank by total mass. In "Top Pairs by Mass" mode this also
    # truncates to Max Curves To Build; in "Top Exporters" mode the sort
    # still happens (so if Max Total Spheres runs out mid-phase-3, bigger
    # pairs are tried first) but nothing is truncated here - the whole point
    # of that mode is "show ALL of these reporters' partners". ----
    candidates.sort(key=lambda c: c["total_mass"], reverse=True)
    if settings.selection_mode == "TOP_PAIRS":
        kept = candidates[: settings.max_pairs]
        n_dropped_by_rank = n_candidates - len(kept)
        log(f"keeping top {len(kept)} of {n_candidates} by total mass ({n_dropped_by_rank} smaller pairs dropped)")
    else:
        kept = candidates
        n_dropped_by_rank = 0

    if kept:
        masses = [c["total_mass"] for c in kept]
        log(f"kept pairs' total_mass range: min={min(masses):.6g}, max={max(masses):.6g}, median={sorted(masses)[len(masses) // 2]:.6g}")

    # ---- Derive the effective Mass Per Sphere. In "Fraction of Total" mode
    # this is Mass Fraction x the combined total mass of whichever pairs
    # actually survived scanning/ranking/selection above - computed HERE
    # (not from settings.mass_interval directly) so it automatically scales
    # to this dataset/selection's actual magnitude, rather than needing you
    # to guess an absolute number that also has to stay compatible with Max
    # Spheres Per Pair / Max Total Spheres. In "Absolute Value" mode this is
    # just settings.mass_interval, unchanged. ----
    if settings.mass_scaling_mode == "FRACTION_OF_TOTAL":
        grand_total_mass = sum(c["total_mass"] for c in kept)
        effective_mass_interval = settings.mass_fraction * grand_total_mass
        if effective_mass_interval <= 0:
            settings.last_status = (
                f"Fraction of Total mode: combined total mass of kept pairs "
                f"is {grand_total_mass:.6g}, giving a Mass Per Sphere of "
                f"{effective_mass_interval:.6g} - must be > 0. Check Mass "
                f"Fraction and that your data has positive mass values"
            )
            return
        log(
            f"Mass Per Sphere (derived): Mass Fraction {settings.mass_fraction:.3g} "
            f"x combined total mass {grand_total_mass:.6g} = {effective_mass_interval:.6g}"
        )
    else:
        effective_mass_interval = settings.mass_interval
        log(f"Mass Per Sphere (absolute): {effective_mass_interval:.6g}")

    # ---- Dry run: estimate what a real run would build, using the SAME
    # total_mass // Mass Per Sphere formula the real solver uses internally
    # for k_max (not an approximation), applying the SAME Max Total Spheres
    # budget check - but never call the actual PCHIP solver and never touch
    # obj.data at all. Use this to sanity-check parameters cheaply before
    # committing to a real (and potentially slow/memory-heavy) run. ----
    if settings.dry_run:
        n_capped_estimate = n_over_total_cap = 0
        running_total_estimate = 0
        per_pair_estimates = []
        for pair_i, c in enumerate(kept):
            estimated_k = int(c["total_mass"] // effective_mass_interval)
            if estimated_k > settings.max_spheres_per_pair:
                n_capped_estimate += 1
            elif running_total_estimate + max(estimated_k, 0) > settings.max_total_spheres:
                n_over_total_cap += 1
            else:
                running_total_estimate += estimated_k
                per_pair_estimates.append((c["a"].name, c["b"].name, estimated_k))
            yield 0.55 + 0.45 * (pair_i + 1) / len(kept)

        for name_a, name_b, estimated_k in per_pair_estimates:
            log(f"[dry run] {name_a} -> {name_b}: ~{estimated_k} spheres (estimated)")

        settings.last_status = (
            f"DRY RUN: Mass Per Sphere={effective_mass_interval:.6g} -> would "
            f"build {len(per_pair_estimates)} curves, ~{running_total_estimate} "
            f"total spheres (estimated) - {n_capped_estimate} pairs would "
            f"exceed Max Spheres Per Pair, {n_over_total_cap} would exceed "
            f"Max Total Spheres. Nothing was written to this object."
        )
        return

    # ---- Solve (55% - 100% of progress): the expensive part - one PCHIP
    # solve per kept pair, with a running total-spheres budget checked
    # BEFORE each solve (estimated cheaply as total_mass // mass_interval -
    # the exact formula compute_emission_times itself uses for k_max when
    # num_spheres=0, not an approximation) so a pair that would blow the
    # budget is skipped without even paying for the solve. Keeps checking
    # smaller pairs afterward rather than stopping outright, since they
    # might still fit the remaining budget. This is one of the direct fixes
    # for a real crash: Max Curves To Build x Max Spheres Per Pair could
    # multiply to something enormous (500 x 50,000 = 25 million points)
    # even though each cap looked reasonable alone - Max Total Spheres
    # bounds the sum directly instead. ----
    curve_lengths = []
    all_emission_times = []
    all_cumulative_mass = []
    all_sphere_index = []
    reporter_codes = []
    partner_codes = []
    pair_names = []
    country_a_positions = []
    country_b_positions = []
    n_capped = n_other_error = n_over_total_cap = 0
    running_total_spheres = 0

    if partial is not None:
        # References, not copies - the solve loop keeps appending to these
        # same list objects below, so `partial` automatically reflects the
        # latest in-progress state without needing to be updated again.
        partial["curve_lengths"] = curve_lengths
        partial["all_emission_times"] = all_emission_times
        partial["all_cumulative_mass"] = all_cumulative_mass
        partial["all_sphere_index"] = all_sphere_index
        partial["reporter_codes"] = reporter_codes
        partial["partner_codes"] = partner_codes
        partial["pair_names"] = pair_names
        partial["country_a_positions"] = country_a_positions
        partial["country_b_positions"] = country_b_positions

    for pair_i, c in enumerate(kept):
        estimated_k = int(c["total_mass"] // effective_mass_interval)
        if running_total_spheres + max(estimated_k, 0) > settings.max_total_spheres:
            n_over_total_cap += 1
            log(
                f"skipping {c['a'].name} -> {c['b'].name}: would exceed Max "
                f"Total Spheres cap ({running_total_spheres} + ~{estimated_k} "
                f"> {settings.max_total_spheres})"
            )
            yield 0.55 + 0.45 * (pair_i + 1) / len(kept)
            continue

        pair_log = (lambda msg, c=c: log(f"{c['a'].name} -> {c['b'].name}: {msg}")) if settings.debug else (lambda msg: None)
        try:
            emission_times, target_masses, particle_mass = core.compute_emission_times(
                c["x"], c["y"], effective_mass_interval, 0, max_spheres=settings.max_spheres_per_pair, log=pair_log,
            )
        except ValueError as e:
            n_capped += 1
            log(f"skipping {c['a'].name} -> {c['b'].name}: {e}")
            yield 0.55 + 0.45 * (pair_i + 1) / len(kept)
            continue
        except Exception as e:
            n_other_error += 1
            log(f"skipping {c['a'].name} -> {c['b'].name}: unexpected error: {e}")
            yield 0.55 + 0.45 * (pair_i + 1) / len(kept)
            continue

        k = len(emission_times)
        running_total_spheres += k
        curve_lengths.append(int(k))
        all_emission_times.append(emission_times)
        all_cumulative_mass.append(target_masses)
        all_sphere_index.append(np.arange(1, k + 1, dtype=np.int32))
        reporter_codes.append(c["reporter_code"])
        partner_codes.append(c["partner_code"])
        pair_names.append(f"{c['a'].name} -> {c['b'].name}")
        country_a_positions.append(tuple(c["a"].matrix_world.translation))
        country_b_positions.append(tuple(c["b"].matrix_world.translation))
        yield 0.55 + 0.45 * (pair_i + 1) / len(kept)

    n_included = len(curve_lengths)
    log(f"done: {n_included} curves built, {running_total_spheres} total spheres, {n_capped} over per-pair cap, {n_over_total_cap} over total cap, {n_other_error} other errors")

    if n_included == 0:
        settings.last_status = (
            f"{n_candidates} pairs had data but none built successfully: "
            f"{n_capped} over per-pair sphere cap, {n_over_total_cap} over "
            f"total sphere cap, {n_other_error} other errors"
        )
        return

    total_points = _write_curves_object(
        obj, curve_lengths, all_emission_times, all_cumulative_mass, all_sphere_index,
        reporter_codes, partner_codes, pair_names, country_a_positions, country_b_positions,
    )

    if settings.selection_mode == "TOP_EXPORTERS":
        reporter_names = ", ".join(info[0].name for info in kept_reporters)
        mode_desc = f"exporters [{reporter_names}] ({n_reporters_dropped} smaller reporters excluded)"
    else:
        mode_desc = f"top {len(kept)} pairs by mass ({n_dropped_by_rank} smaller pairs dropped)"

    settings.last_status = (
        f"OK: Mass Per Sphere={effective_mass_interval:.6g} -> {n_included} "
        f"curves built, {total_points} total spheres. "
        f"{n_candidates} pairs had data -> kept via {mode_desc} "
        f"({n_capped} over per-pair cap, {n_over_total_cap} over total cap, "
        f"{n_other_error} other errors)"
    )


def _run_recompute_sync(obj, depsgraph):
    """Drain _recompute_steps in one go, ignoring its progress yields - for
    callers that can't be modal (the depsgraph auto-update handler, the Add
    operator's initial compute). Still gets the vectorized speedup; just no
    progress bar or cancellation, since there's no interactive context to
    show one in.
    """
    for _ in _recompute_steps(obj, depsgraph):
        pass


def _safe_recompute(obj, depsgraph):
    try:
        _run_recompute_sync(obj, depsgraph)
    except Exception as e:
        obj.cpbe_settings.last_status = f"ERROR: {e}"
        print(f"[country_pair_batch_emitter] '{obj.name}' failed to update: {e}")


# ---------------------------------------------------------------------------
#  Operators
# ---------------------------------------------------------------------------

class OBJECT_OT_cpbe_add(bpy.types.Operator):
    """Add a new Country Pair Batch Emitter object - a dedicated,
    self-updating object type, not a behavior you can bolt onto an arbitrary
    existing one"""
    bl_idname = "object.cpbe_add"
    bl_label = "Country Pair Batch Emitter"
    bl_options = {"REGISTER", "UNDO"}

    object_name: bpy.props.StringProperty(name="Name", default="CPBE_Generator")
    # Name-resolved, not PointerProperty(type=Object/Collection) - ID-type
    # pointer properties on Operators silently break registration of every
    # property declared after them in Blender 5.3 (see
    # constant_mass_emission_generator.py's OBJECT_OT_cmet_add for the
    # confirmed incident). Keeping this operator's own properties minimal on
    # purpose either way - everything else is configured via the Properties
    # panel after creation, same as OBJECT_OT_cmet_add.
    raw_data_object_name: bpy.props.StringProperty(name="Raw Data Object")
    countries_collection_name: bpy.props.StringProperty(name="Countries Collection")
    auto_update: bpy.props.BoolProperty(name="Auto-Update", default=True)
    debug: bpy.props.BoolProperty(name="Debug Output", default=False)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "object_name")
        layout.prop_search(self, "raw_data_object_name", bpy.data, "objects", text="Raw Data Object")
        layout.prop_search(self, "countries_collection_name", bpy.data, "collections", text="Countries")
        layout.prop(self, "auto_update")
        layout.prop(self, "debug")

    def execute(self, context):
        raw_obj = bpy.data.objects.get(self.raw_data_object_name.strip()) if self.raw_data_object_name.strip() else None
        collection = bpy.data.collections.get(self.countries_collection_name.strip()) if self.countries_collection_name.strip() else None

        data = bpy.data.hair_curves.new(name=self.object_name)
        obj = bpy.data.objects.new(self.object_name, data)
        context.collection.objects.link(obj)

        settings = obj.cpbe_settings
        settings.is_cpbe_generator = True
        settings.raw_data_object = raw_obj
        settings.countries_collection = collection
        settings.auto_update = self.auto_update
        settings.debug = self.debug

        context.view_layer.objects.active = obj
        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)

        if raw_obj is not None and collection is not None:
            _safe_recompute(obj, context.evaluated_depsgraph_get())
            if settings.auto_update:
                _TRACKED.add(obj.name)
            if settings.last_status.startswith("ERROR"):
                self.report({"WARNING"}, settings.last_status)
        else:
            settings.last_status = "Raw Data Object and Countries must both be set"

        return {"FINISHED"}


class OBJECT_OT_cpbe_generate(bpy.types.Operator):
    """Compute now (shows a progress bar; press Esc to cancel), and start
    (or refresh) automatic tracking"""
    bl_idname = "object.cpbe_generate"
    bl_label = "Generate / Update Now"
    bl_options = {"REGISTER", "UNDO"}

    _timer = None
    _generator = None
    _obj_name = None
    _partial = None

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.cpbe_settings.is_cpbe_generator

    def invoke(self, context, event):
        obj = context.active_object
        settings = obj.cpbe_settings
        if settings.raw_data_object is None or settings.countries_collection is None:
            self.report({"ERROR"}, "Set Raw Data Object and Countries first")
            return {"CANCELLED"}

        self._obj_name = obj.name
        self._partial = {}
        self._generator = _recompute_steps(obj, context.evaluated_depsgraph_get(), partial=self._partial)

        wm = context.window_manager
        wm.progress_begin(0.0, 1.0)
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        wm = context.window_manager
        obj = bpy.data.objects.get(self._obj_name)
        if obj is None:
            self._cleanup(context)
            return {"CANCELLED"}
        settings = obj.cpbe_settings

        if event.type == "ESC":
            n_partial = len(self._partial.get("curve_lengths") or [])
            if n_partial > 0:
                total_points = _write_curves_object(
                    obj,
                    self._partial["curve_lengths"], self._partial["all_emission_times"],
                    self._partial["all_cumulative_mass"], self._partial["all_sphere_index"],
                    self._partial["reporter_codes"], self._partial["partner_codes"],
                    self._partial["pair_names"], self._partial["country_a_positions"],
                    self._partial["country_b_positions"],
                )
                settings.last_status = f"Cancelled: kept {n_partial} curves ({total_points} spheres) completed before cancellation"
                self.report({"WARNING"}, settings.last_status)
            else:
                settings.last_status = "Cancelled before any curves were completed - object left as it was before this run"
                self.report({"WARNING"}, settings.last_status)
            self._cleanup(context)
            return {"CANCELLED"}

        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        # Process a time-boxed chunk of steps per tick, rather than exactly
        # one step per tick - a step (one reporter scanned, or one pair
        # solved) can be cheap enough that single-stepping would needlessly
        # slow down completion, or expensive enough that even one step
        # blocks noticeably; either way, budgeting wall-clock time per tick
        # keeps the UI responsive without adding artificial delay.
        deadline = time.perf_counter() + 0.05
        try:
            while time.perf_counter() < deadline:
                progress = next(self._generator)
                wm.progress_update(progress)
        except StopIteration:
            if settings.auto_update:
                _TRACKED.add(obj.name)
            status = settings.last_status
            self._cleanup(context)
            if status.startswith("ERROR"):
                self.report({"ERROR"}, status)
                return {"CANCELLED"}
            self.report({"INFO"}, status)
            return {"FINISHED"}
        except Exception as e:
            print(f"[country_pair_batch_emitter] '{obj.name}' failed to update: {e}")
            n_partial = len(self._partial.get("curve_lengths") or [])
            if n_partial > 0:
                total_points = _write_curves_object(
                    obj,
                    self._partial["curve_lengths"], self._partial["all_emission_times"],
                    self._partial["all_cumulative_mass"], self._partial["all_sphere_index"],
                    self._partial["reporter_codes"], self._partial["partner_codes"],
                    self._partial["pair_names"], self._partial["country_a_positions"],
                    self._partial["country_b_positions"],
                )
                settings.last_status = f"ERROR: {e} (kept {n_partial} curves / {total_points} spheres completed before the error)"
            else:
                settings.last_status = f"ERROR: {e}"
            self._cleanup(context)
            self.report({"ERROR"}, settings.last_status)
            return {"CANCELLED"}

        return {"RUNNING_MODAL"}

    def _cleanup(self, context):
        wm = context.window_manager
        wm.progress_end()
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        self._generator = None
        self._partial = None


class OBJECT_OT_cpbe_stop_tracking(bpy.types.Operator):
    """Stop automatically recomputing this object"""
    bl_idname = "object.cpbe_stop_tracking"
    bl_label = "Stop Live Tracking"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.name in _TRACKED

    def execute(self, context):
        _TRACKED.discard(context.active_object.name)
        context.active_object.cpbe_settings.auto_update = False
        return {"FINISHED"}


# ---------------------------------------------------------------------------
#  UI
# ---------------------------------------------------------------------------

class OBJECT_PT_cpbe_panel(bpy.types.Panel):
    bl_label = "Country Pair Batch Emitter"
    bl_idname = "OBJECT_PT_cpbe_panel"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.cpbe_settings.is_cpbe_generator

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        settings = obj.cpbe_settings

        layout.prop(settings, "raw_data_object")
        layout.prop(settings, "countries_collection")
        layout.prop(settings, "code_property_name")
        layout.prop(settings, "reporter_attribute_name")
        layout.prop(settings, "partner_attribute_name")
        row = layout.row(align=True)
        row.prop(settings, "item_code_attribute")
        row.prop(settings, "item_code_value")
        row = layout.row(align=True)
        row.prop(settings, "element_code_attribute")
        row.prop(settings, "element_code_value")
        layout.prop(settings, "x_attribute")
        layout.prop(settings, "y_attribute")

        layout.prop(settings, "mass_scaling_mode")
        if settings.mass_scaling_mode == "FRACTION_OF_TOTAL":
            layout.prop(settings, "mass_fraction")
        else:
            layout.prop(settings, "mass_interval")

        layout.prop(settings, "selection_mode")
        if settings.selection_mode == "TOP_EXPORTERS":
            row = layout.row(align=True)
            row.prop(settings, "max_reporters")
            row.prop(settings, "max_partners_per_reporter")

        box = layout.box()
        box.label(text="Safety Limits", icon="ERROR")
        box.prop(settings, "max_countries")
        if settings.selection_mode == "TOP_PAIRS":
            box.prop(settings, "max_pairs")
        box.prop(settings, "max_spheres_per_pair")
        box.prop(settings, "max_total_spheres")

        layout.prop(settings, "dry_run", icon="HIDE_OFF" if not settings.dry_run else "HIDE_ON")
        layout.prop(settings, "auto_update")
        layout.prop(settings, "debug")

        row = layout.row(align=True)
        row.operator("object.cpbe_generate", icon="PLAY")
        row.operator("object.cpbe_stop_tracking", icon="X", text="")

        is_tracked = obj.name in _TRACKED
        info = layout.box()
        info.label(
            text=f"Live tracking: {'ON' if is_tracked else 'off'}",
            icon="CHECKMARK" if is_tracked else "PAUSE",
        )
        if settings.last_status:
            icon = "ERROR" if settings.last_status.startswith("ERROR") else "INFO"
            info.label(text=settings.last_status, icon=icon)


class NODE_PT_cpbe_add_panel(bpy.types.Panel):
    """One-click "Add Country Pair Batch Emitter" from inside the Geometry
    Nodes editor - same reasoning as constant_mass_emission_generator.py's
    NODE_PT_cmet_add_panel: this operator lives in the 3D Viewport's Add
    menu, a different menu system than this editor's own node-only Add menu.
    """
    bl_label = "Country Pair Batch Emitter"
    bl_idname = "NODE_PT_cpbe_add_panel"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Data Viz"

    @classmethod
    def poll(cls, context):
        return getattr(context.space_data, "tree_type", None) == "GeometryNodeTree"

    def draw(self, context):
        self.layout.operator(OBJECT_OT_cpbe_add.bl_idname, icon="PARTICLES")


# ---------------------------------------------------------------------------
#  The auto-update handler
# ---------------------------------------------------------------------------

@persistent
def _on_depsgraph_update(scene, depsgraph):
    if not _TRACKED:
        return

    changed_ids = set()
    for update in depsgraph.updates:
        original = update.id.original if update.id.original is not None else update.id
        changed_ids.add(original)

    for name in list(_TRACKED):
        obj = bpy.data.objects.get(name)
        if obj is None:
            _TRACKED.discard(name)
            continue
        settings = obj.cpbe_settings
        if not settings.is_cpbe_generator or not settings.auto_update:
            _TRACKED.discard(name)
            continue

        settings_changed = obj in changed_ids

        raw_obj = settings.raw_data_object
        collection = settings.countries_collection
        if raw_obj is None or collection is None:
            continue

        # Watch the raw dataset itself, and every object currently in the
        # countries collection - if any of them (or their data) changed,
        # recompute. Adding/removing objects from the collection itself
        # shows up as a change to the Collection ID, which the settings_
        # changed / obj-level check doesn't cover, so check the collection
        # explicitly too.
        watched = [raw_obj, collection] + list(collection.objects)
        data_changed = any(
            w in changed_ids or (getattr(w, "data", None) is not None and w.data in changed_ids)
            for w in watched
        )
        if settings_changed or data_changed:
            _safe_recompute(obj, depsgraph)


@persistent
def _rebuild_tracked_registry(dummy=None):
    _TRACKED.clear()
    for obj in bpy.data.objects:
        settings = getattr(obj, "cpbe_settings", None)
        if (
            settings
            and settings.is_cpbe_generator
            and settings.auto_update
            and settings.raw_data_object is not None
            and settings.countries_collection is not None
        ):
            _TRACKED.add(obj.name)


# ---------------------------------------------------------------------------
#  Register / unregister
# ---------------------------------------------------------------------------

_CLASSES = (
    CPBE_Settings,
    OBJECT_OT_cpbe_add,
    OBJECT_OT_cpbe_generate,
    OBJECT_OT_cpbe_stop_tracking,
    OBJECT_PT_cpbe_panel,
    NODE_PT_cpbe_add_panel,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.cpbe_settings = bpy.props.PointerProperty(type=CPBE_Settings)

    dv_menu.add_entry(
        OBJECT_OT_cpbe_add.bl_idname,
        text="Country Pair Batch Emitter",
        icon="PARTICLES",
    )
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    bpy.app.handlers.load_post.append(_rebuild_tracked_registry)
    bpy.app.timers.register(_rebuild_tracked_registry, first_interval=1.0)


def unregister():
    if _rebuild_tracked_registry in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_rebuild_tracked_registry)
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    dv_menu.remove_entry(OBJECT_OT_cpbe_add.bl_idname)

    del bpy.types.Object.cpbe_settings
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    _TRACKED.clear()

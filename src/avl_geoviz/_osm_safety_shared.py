# Shared OSM/Overpass safety-preflight checks used by both
# country_wireframes.py and flat_gis_importer.py - both independently fetch
# OSM data through Overpass, and both need the same "don't silently fire a
# potentially huge/slow query" safety net (bbox-area caps, a feature-count
# warning, and a timeout warning) in front of their respective OSM-import
# operators. Kept here so there is exactly one implementation of the
# threshold logic; each caller adapts its own settings shape (property
# names/availability differ between the two add-ons) into the plain
# arguments below rather than sharing a settings PropertyGroup.
#
# This is a normal package-relative import (`from . import _osm_safety_
# shared`) since country_wireframes.py and flat_gis_importer.py are both
# loaded as members of the scripts/startup package (see __init__.py) -
# unlike user_scripts/ or node_scripts/ files, which are loaded one at a
# time via importlib with no parent package context.


def bbox_area_deg2(south, west, north, east):
    return max(0.0, north - south) * max(0.0, east - west)


def osm_preflight_messages(
    area_deg2,
    warn_bbox_area_deg2,
    hard_bbox_area_deg2,
    max_features,
    is_custom_query,
    timeout_seconds,
):
    """Return a list of non-fatal warning strings to show in a confirmation
    dialog before firing a potentially expensive Overpass query.

    Raises RuntimeError instead of returning when area_deg2 exceeds the hard
    cap - that case is a hard block, not something a confirmation dialog can
    wave through.
    """
    messages = []

    if area_deg2 > float(hard_bbox_area_deg2):
        raise RuntimeError(
            "OSM bbox is too large for safe import. "
            f"Area={area_deg2:.3f} deg^2 exceeds hard limit "
            f"{float(hard_bbox_area_deg2):.3f} deg^2."
        )

    if area_deg2 > float(warn_bbox_area_deg2):
        messages.append(
            "BBox area is large and may trigger heavy OSM server load "
            f"({area_deg2:.3f} deg^2)."
        )

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

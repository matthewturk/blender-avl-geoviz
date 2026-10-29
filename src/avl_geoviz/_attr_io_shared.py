"""Shared attribute-writing and node-interface helpers, used by the
constant-mass emission tools (constant_mass_emission_generator.py,
country_pair_batch_emitter.py, user_scripts/constant_mass_emission_times.py),
the tabular importers (user_scripts/hdf5_to_curves.py,
user_scripts/sqlite_to_object.py) and the menu-switch builders
(user_scripts/csv_menu_select.py, user_scripts/attribute_menu_switch.py).

Package modules import this as `from . import _attr_io_shared`; files in
user_scripts/ load it with the usual _load_sibling pattern (see
_collection_to_lists_shared.py for why). Kept one directory up from
user_scripts/ so the Script Runner extension's scan never lists it.
"""

import numpy as np
import databpy as db


def write_float_attribute(obj, name, values, domain=db.AttributeDomains.POINT):
    db.store_named_attribute(
        obj, np.asarray(values, dtype=np.float32), name, atype=db.AttributeTypes.FLOAT, domain=domain
    )


def write_int_attribute(obj, name, values, domain=db.AttributeDomains.POINT):
    db.store_named_attribute(
        obj, np.asarray(values, dtype=np.int32), name, atype=db.AttributeTypes.INT, domain=domain
    )


def write_vector_attribute(obj, name, values, domain=db.AttributeDomains.POINT):
    db.store_named_attribute(
        obj, np.asarray(values, dtype=np.float32), name, atype=db.AttributeTypes.FLOAT_VECTOR, domain=domain
    )


def write_string_attribute(obj_data, name, values, domain):
    """Write a STRING attribute directly through bpy's attributes API.

    databpy's `store_named_attribute` doesn't handle STRING attributes on
    every installed version (some route straight through `foreach_set`,
    which can't take a sequence of strings), so this is done by hand instead.
    """
    attr = obj_data.attributes.get(name)
    if attr is None or attr.data_type != "STRING" or attr.domain != domain:
        if attr is not None:
            obj_data.attributes.remove(attr)
        attr = obj_data.attributes.new(name=name, type="STRING", domain=domain)
    for item, value in zip(attr.data, values):
        item.value = str(value).encode("utf-8")


def ensure_socket(tree, in_out, name, socket_type):
    """Return the interface socket `name`, creating it if missing. An
    existing socket keeps its links across reruns; one whose type no longer
    matches is replaced (a socket can't change type while staying connected
    to something expecting the old one).
    """
    for item in tree.interface.items_tree:
        if (
            getattr(item, "item_type", None) == "SOCKET"
            and item.in_out == in_out
            and item.name == name
        ):
            if item.socket_type == socket_type:
                return item
            tree.interface.remove(item)
            break
    return tree.interface.new_socket(name=name, in_out=in_out, socket_type=socket_type)

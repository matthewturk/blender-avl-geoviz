"""Pure numeric core for constant-mass emission-time computation.

No bpy dependency on purpose, so it can be unit-tested standalone and reused
unchanged by both user_scripts/constant_mass_emission_times.py (the one-shot
script) and constant_mass_emission_generator.py (the live-tracking version) -
one place to fix, if it ever needs fixing again.

See pchip.py in the repo root for the reference algorithm this mirrors, and
either consumer file's module docstring for the full explanation of the
anchor-boundary and midpoint-centering choices.
"""

import numpy as np
from scipy.interpolate import PchipInterpolator


def resolve_particle_mass(total_mass, mass_interval, num_spheres):
    if num_spheres > 0:
        return total_mass / num_spheres
    return mass_interval


def solve_emission_time(m_interp, target, t_nodes):
    """Invert M(t) == target for t, restricted to the curve's actual domain.

    PPoly.solve() extrapolates by default (extrapolate=True), which can
    return a spurious out-of-domain "root" from the extended boundary
    polynomial - picking the first result blind would silently grab one of
    those instead of the real, in-domain crossing (confirmed with a
    synthetic dataset during development: extrapolated roots landed years
    outside the data's actual range). It can also return NaN entries for a
    perfectly flat (zero-slope) plateau of the interpolant, e.g. a stretch of
    zero-mass years - filtered out below; any one point within such a
    plateau is an equally valid emission time for that target.
    """
    roots = m_interp.solve(y=target, extrapolate=False)
    roots = roots[np.isfinite(roots) & (roots >= t_nodes[0]) & (roots <= t_nodes[-1])]
    if len(roots) == 0:
        raise RuntimeError(f"No in-domain time found for target cumulative mass {target}")
    return float(roots[0])


def compute_emission_times(x, y, mass_interval, num_spheres, max_spheres=50_000, log=None):
    """x, y: 1D arrays of equal length (need not be pre-sorted or evenly
    spaced; y must be >= 0 everywhere, and x must have no duplicate values
    after sorting).

    max_spheres is a hard safety cap on k_max, checked *before* the solve
    loop runs: solving one PCHIP root per sphere is a synchronous Python
    loop with no cancellation, so a k_max in the millions (typically from
    "Mass Per Sphere" being left at a default that's tiny relative to the
    source data's actual mass scale) doesn't error, it just blocks for a
    very long time - which looks exactly like Blender hanging. Raising
    immediately, with the actual computed numbers, is far more useful than
    letting that loop run.

    log, if given, is called with human-readable progress strings (e.g. a
    print-based debug callback) at each stage - this function stays silent
    (and the caller does whatever it wants with the messages) when log is
    None.

    Returns (emission_times, target_masses, particle_mass): the first two are
    1D float64 arrays of length k_max (one entry per emitted sphere), the
    third is the resolved scalar mass-per-sphere actually used.
    """
    if log is None:
        log = lambda msg: None
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)

    if len(x) < 2:
        raise ValueError("Need at least 2 data points to build a cumulative-mass curve")
    if np.any(y < 0):
        raise ValueError("Mass values must be >= 0 everywhere - cumulative mass must be non-decreasing")

    order = np.argsort(x, kind="stable")
    x_sorted, y_sorted = x[order], y[order]
    if np.any(np.diff(x_sorted) <= 0):
        raise ValueError("Time values have duplicate entries after sorting - each row needs a distinct time")

    # Anchor the cumulative curve at M=0 one step before the first data
    # point - without this, any target mass below y_sorted[0] has no valid
    # root, silently dropping the first stretch of shipments.
    t_start = 2 * x_sorted[0] - x_sorted[1]
    t_nodes = np.concatenate(([t_start], x_sorted))
    m_nodes = np.concatenate(([0.0], np.cumsum(y_sorted)))

    total_mass = float(m_nodes[-1])
    log(f"total_mass={total_mass:.6g} (from {len(y)} data points, x range [{x_sorted[0]:.6g}, {x_sorted[-1]:.6g}])")
    if total_mass <= 0:
        raise ValueError(f"Total mass is {total_mass}, nothing to emit")

    particle_mass = resolve_particle_mass(total_mass, mass_interval, num_spheres)
    if particle_mass <= 0:
        raise ValueError("Mass per sphere must be > 0")

    if num_spheres > 0:
        # particle_mass was derived as total_mass / num_spheres, so re-deriving
        # k_max via floor division on that same float round-trips through a
        # division and a multiplication back - unreliable near exact integer
        # boundaries (confirmed off-by-one roughly half the time). num_spheres
        # is already the exact answer the user asked for; use it directly.
        k_max = num_spheres
    else:
        k_max = int(total_mass // particle_mass)
    log(f"particle_mass={particle_mass:.6g} -> k_max={k_max}")
    if k_max < 1:
        raise ValueError(
            f"Mass per sphere ({particle_mass}) is larger than the total mass "
            f"({total_mass}) - nothing would be emitted"
        )
    if k_max > max_spheres:
        raise ValueError(
            f"Computed {k_max} spheres (total_mass={total_mass:.6g}, "
            f"particle_mass={particle_mass:.6g}), which is over the safety cap "
            f"of {max_spheres}. Solving one PCHIP root per sphere in a plain "
            f"Python loop for that many would take a very long time and block "
            f"Blender the whole way through - this is almost always 'Mass Per "
            f"Sphere' left far too small (or 'Total Spheres' too large) for "
            f"this data's actual mass scale. Raise 'Mass Per Sphere' (or lower "
            f"'Total Spheres') so k_max lands in the thousands, not millions."
        )

    m_interp = PchipInterpolator(t_nodes, m_nodes)
    target_masses = (np.arange(1, k_max + 1) - 0.5) * particle_mass
    emission_times = np.empty(k_max, dtype=np.float64)
    for i, target in enumerate(target_masses):
        emission_times[i] = solve_emission_time(m_interp, target, t_nodes)
        if k_max >= 1000 and (i + 1) % 1000 == 0:
            log(f"solved {i + 1}/{k_max} emission times...")
    log(f"done: {k_max} emission times solved")
    return emission_times, target_masses, particle_mass

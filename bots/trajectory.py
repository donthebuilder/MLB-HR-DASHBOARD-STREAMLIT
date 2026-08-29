#!/usr/bin/env python3
"""trajectory.py -- ball flight, reconstructed server-side.

Mirrors lib/trajectory.js in the moonshot-push site repo EXACTLY (same
constants, same RK4-with-quadratic-drag integration, same bisection on drag
coefficient). If one changes, change both -- a site solve and a bot solve that
quietly drift apart is worse than either one alone, because "which number is
right" becomes a real question with no visible tie-breaker.

WHY THIS EXISTS (2026-08-29, Donovan: "moving the ball-flight math server-side
... that's the one remaining step"). Until now every browser that opened a
spray chart re-solved this same physics for every ball on screen -- correct,
but 20+ batted balls x a bisection-refined RK4 integration, every single page
load, for a number that never changes once the ball has landed. This module
lets the nightly bot solve it ONCE per batted ball and publish the answer
(`apex_ft`, `hang_time_s`, `traj_poly`) so the client can just read it.

WHAT THIS IS, AND IS NOT (identical caveat to the JS side, repeated here
because this file will get read on its own). Statcast tracks PITCHES, not
batted balls, so there is no measured trajectory to check this against -- the
arc is FIT: real projectile motion with quadratic drag, solved so the ball
lands at the plotted distance, leaving the bat at the true exit velo and
launch angle. The solved drag coefficient is an EFFECTIVE drag that absorbs
backspin lift (Statcast doesn't publish batted-ball spin). Good for "how high
was this ball at the wall" and for a hover animation. Not something to present
as measured trajectory. Reference: Alan Nathan, "The Physics of Baseball".

Measured cost: ~9.5 ms/ball (RK4 + bisection) -- about 2s for a full slate's
worth of nightly batted balls, ~16 minutes for a 100k-ball historical backfill
run separately (not part of the nightly path).
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple, TypedDict

G = 9.80665
MPH_TO_MS = 0.44704
M_TO_FT = 3.280839895
FT_TO_M = 1.0 / M_TO_FT
CONTACT_H_FT = 3.0

# Same figures as lib/trajectory.js, same reasoning: dt=0.02 sits 0.0016 ft
# from a dt=0.002 reference on height-at-the-fence, at a tenth of the cost.
# 22 bisection passes at a 0.25 ft range tolerance -- looser (the "obvious"
# 1 ft) measurably moves height-at-the-fence by half a foot. Don't loosen
# either without re-measuring against the JS solver, the way that file's own
# comment insists on for itself.
DT = 0.02
MAX_T = 12.0
BISECT = 22

# traj_poly resolution. The 2026-08-29 spray-3d repo audit measured 32 points
# holding linear-interpolation error to 0.15 ft (12 points would be 0.99 ft
# and would mislabel wall balls) -- so this is a measured choice, not a guess.
POLY_POINTS = 32


class Flight(TypedDict):
    distance_ft: float
    apex_ft: float
    hang_time_s: float
    # 32 evenly TIME-spaced [distance_ft, height_ft] pairs, home plate to
    # landing. This is what the client plots/animates -- it does not need to
    # re-run RK4 to draw the arc or to drive a hover animation.
    traj_poly: List[Tuple[float, float]]


def _fly(v0: float, th: float, k: float, y0: float) -> Tuple[float, float, float, List[Tuple[float, float]]]:
    """RK4 integrate one flight. Returns (range_m, apex_m, hang_s, samples_m)."""
    x, y = 0.0, y0
    vx, vy = v0 * math.cos(th), v0 * math.sin(th)
    samp: List[Tuple[float, float]] = [(0.0, y0)]
    apex = y0
    t = 0.0
    steps = round(MAX_T / DT)

    def deriv(svx: float, svy: float) -> Tuple[float, float, float, float]:
        sp = math.hypot(svx, svy)
        return svx, svy, -k * sp * svx, -G - k * sp * svy

    for _ in range(steps):
        a = deriv(vx, vy)
        b = deriv(vx + 0.5 * DT * a[2], vy + 0.5 * DT * a[3])
        c = deriv(vx + 0.5 * DT * b[2], vy + 0.5 * DT * b[3])
        e = deriv(vx + DT * c[2], vy + DT * c[3])
        px, py = x, y
        x += (DT / 6) * (a[0] + 2 * b[0] + 2 * c[0] + e[0])
        y += (DT / 6) * (a[1] + 2 * b[1] + 2 * c[1] + e[1])
        vx += (DT / 6) * (a[2] + 2 * b[2] + 2 * c[2] + e[2])
        vy += (DT / 6) * (a[3] + 2 * b[3] + 2 * c[3] + e[3])
        t += DT
        if y > apex:
            apex = y
        samp.append((x, y))
        if y <= 0:
            f = py / (py - y or 1)
            xh = px + f * (x - px)
            samp[-1] = (xh, 0.0)
            return xh, apex, t - DT + f * DT, samp
    return x, apex, t, samp


def _to_ft(samp: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    return [(x * M_TO_FT, y * M_TO_FT) for x, y in samp]


def _poly(samples_ft: List[Tuple[float, float]], hang_s: float, n: int = POLY_POINTS) -> List[Tuple[float, float]]:
    """Resample the (distance-indexed) RK4 samples into n evenly TIME-spaced
    points. Mirrors lib/trajectory.js's `timeFrames`, in feet instead of
    fractions since this is what gets published and plotted directly."""
    S = samples_ft
    out: List[Tuple[float, float]] = []
    last = len(S) - 1
    for i in range(n + 1):
        idx = min(last, (i / n) * last)
        lo = int(math.floor(idx))
        hi = min(last, lo + 1)
        f = idx - lo
        x0, y0 = S[lo]
        x1, y1 = S[hi]
        out.append((round(x0 + f * (x1 - x0), 2), round(max(0.0, y0 + f * (y1 - y0)), 2)))
    return out


# Mirrors SprayField.js's own `toPolar()` EXACTLY -- same origin, same 2.5
# scale factor. This is the radius the client actually plots the dot at, and
# lib/trajectory.js's own header is explicit that a flight MUST be fit to this
# radius, not to hit_distance_sc ("carry"): fitting to carry would end the
# solved arc somewhere the dot on screen is not. So the caller here must pass
# a distance computed by this function -- NOT the raw Statcast hit_distance_sc
# column -- or the published apex/hang/traj_poly will describe a ball that
# lands in a different place than the one drawn.
def plotted_radius_ft(hc_x: Optional[float], hc_y: Optional[float]) -> Optional[float]:
    if hc_x is None or hc_y is None:
        return None
    dx = hc_x - 125.42
    dy = 198.27 - hc_y
    dist = math.sqrt(dx * dx + dy * dy) * 2.5
    return dist if math.isfinite(dist) else None


def solve_flight(ev_mph: Optional[float], la_deg: Optional[float], dist_ft: Optional[float]) -> Optional[Flight]:
    """Fit a flight whose range equals dist_ft. dist_ft MUST be the plotted
    radius (plotted_radius_ft(), from hc_x/hc_y) -- NOT hit_distance_sc/carry,
    see plotted_radius_ft's own docstring above for why. None when the inputs
    cannot describe one -- mirrors lib/trajectory.js's own refusal: a ball with
    no launch angle, no exit velo, or no distance gets no honest arc, and
    callers must fall back rather than publish a fabricated one."""
    if not (ev_mph and ev_mph > 0):
        return None
    if not (la_deg and la_deg > 0.5):
        return None
    if not (dist_ft and dist_ft > 0):
        return None

    v0 = ev_mph * MPH_TO_MS
    th = math.radians(la_deg)
    y0 = CONTACT_H_FT * FT_TO_M
    target = dist_ft * FT_TO_M

    # k=0 is the vacuum case -- the longest possible carry. If the plotted
    # radius exceeds it, no drag reproduces it (wind, altitude, or a
    # coordinate that disagrees with the launch data); use the vacuum arc.
    vac_range, vac_apex, vac_hang, vac_samp = _fly(v0, th, 0.0, y0)
    if target >= vac_range:
        ft_samp = _to_ft(vac_samp)
        return {
            "distance_ft": round(ft_samp[-1][0], 1),
            "apex_ft": round(vac_apex * M_TO_FT, 1),
            "hang_time_s": round(vac_hang, 2),
            "traj_poly": _poly(ft_samp, vac_hang),
        }

    lo, hi = 0.0, 0.02
    for _ in range(20):
        if _fly(v0, th, hi, y0)[0] <= target:
            break
        hi *= 2
        if hi > 5:
            break

    best_range, best_apex, best_hang, best_samp = vac_range, vac_apex, vac_hang, vac_samp
    for _ in range(BISECT):
        mid = 0.5 * (lo + hi)
        r, apex, hang, samp = _fly(v0, th, mid, y0)
        best_range, best_apex, best_hang, best_samp = r, apex, hang, samp
        if abs(r - target) * M_TO_FT < 0.25:
            break
        if r > target:
            lo = mid
        else:
            hi = mid

    ft_samp = _to_ft(best_samp)
    return {
        "distance_ft": round(ft_samp[-1][0], 1),
        "apex_ft": round(best_apex * M_TO_FT, 1),
        "hang_time_s": round(best_hang, 2),
        "traj_poly": _poly(ft_samp, best_hang),
    }

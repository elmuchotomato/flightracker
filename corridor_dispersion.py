"""
corridor_dispersion.py
======================

Lateral-dispersion + Allschwil-overflight analysis for LFSB runway-15
south departures.

This module mirrors the framing of PPBE 2024-2028 action O.7 ("Etude des
possibilités d'adaptation du SID 15 ELBEG"): it quantifies how far each
departure track sits from a *nominal corridor axis*, and produces the
fleet-wide dispersion distribution that O.7 is built on.

IMPORTANT — legal framing
-------------------------
At LFSB there is currently NO formally defined lateral protection volume
(unlike the VPE at Paris-CDG/Orly). A wide track over Allschwil is therefore
NOT, by itself, a sanctionable manquement under the Arrêté du 6 août 2021.
This tool is a transparency / monitoring instrument, not a penalty
calculator. Treat its "violation" flag as "outside the expected corridor",
not "owes a fine".

Geometry sourcing
-----------------
The nominal corridor axis is a polyline you must anchor to real procedure
geometry. The ONLY authoritative source is the French eAIP:
    https://www.sia.aviation-civile.gouv.fr/  ->  AD 2 LFSB  ->  SID charts
Fill CORRIDOR_15 below with the published waypoints (threshold, the
noise-abatement turn point, ELBEG and onward). The defaults are flagged
estimates so the pipeline runs end-to-end; CONFIRM them before trusting
absolute offsets.

No third-party dependencies (pure stdlib math) so it drops straight into
your existing poller.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import mean, median
from typing import Iterable, Sequence

R_EARTH_M = 6_371_008.8  # mean Earth radius, metres


# --------------------------------------------------------------------------
# Geometry config  —  VERIFY values marked [eAIP] against the live SID chart
# --------------------------------------------------------------------------

# LFSB runway 15 threshold (NW end; rwy 15 points ~SE). [eAIP – confirm]
RWY15_THRESHOLD = (47.6033, 7.5158)

# Runway 15 true bearing. Designator 15 => ~153° magnetic; magnetic
# variation at Basel is ~+3°E, so ~156° true. [eAIP – confirm QFU]
RWY15_TRUE_BEARING_DEG = 156.0

# Allschwil town centre — the population centre you're protecting. [confirm]
ALLSCHWIL = (47.5519, 7.5328)
ALLSCHWIL_RADIUS_M = 1500.0  # "overflew Allschwil" if a track passes inside this

# Nominal corridor axis as an ordered list of (lat, lon) waypoints.
# Point 0 should be the threshold; the following points trace the
# noise-preferential south routing (the turn that keeps traffic OFF
# Allschwil). Replace the intermediate/exit points with eAIP values.
#
# The placeholder below is a straight runway-heading segment ONLY — it does
# NOT yet encode the protective turn, so until you add the real turn point,
# every track will read as "on axis" and Allschwil overflights will look
# like corridor compliance. Add the turn waypoint(s) from the SID first.
def _project(origin, bearing_deg, dist_m):
    """Point at distance/bearing from origin (great-circle)."""
    lat1, lon1 = map(math.radians, origin)
    brg = math.radians(bearing_deg)
    dr = dist_m / R_EARTH_M
    lat2 = math.asin(math.sin(lat1) * math.cos(dr) +
                     math.cos(lat1) * math.sin(dr) * math.cos(brg))
    lon2 = lon1 + math.atan2(math.sin(brg) * math.sin(dr) * math.cos(lat1),
                             math.cos(dr) - math.sin(lat1) * math.sin(lat2))
    return (math.degrees(lat2), math.degrees(lon2))


CORRIDOR_15: list[tuple[float, float]] = [
    RWY15_THRESHOLD,          # confirmed vs eAIP
    (47.5885, 7.5256),        # turn point — map-derived placeholder, replace with eAIP SID fix
    (47.5503, 7.5006),        # post-turn — map-derived placeholder, replace with eAIP/ELBEG
]


# --------------------------------------------------------------------------
# Core geodesy
# --------------------------------------------------------------------------

def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R_EARTH_M * math.asin(math.sqrt(h))


def bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def cross_track_m(start, end, point) -> tuple[float, float]:
    """Signed cross-track distance of `point` from great-circle start->end,
    and along-track distance from `start`.
    Sign: +ve = right of the corridor (looking start->end), -ve = left.
    """
    d13 = haversine_m(start, point) / R_EARTH_M
    th13 = math.radians(bearing_deg(start, point))
    th12 = math.radians(bearing_deg(start, end))
    dxt = math.asin(math.sin(d13) * math.sin(th13 - th12))
    dat = math.acos(max(-1.0, min(1.0, math.cos(d13) / math.cos(dxt))))
    return dxt * R_EARTH_M, dat * R_EARTH_M


def offset_from_corridor(point, corridor: Sequence[tuple[float, float]]) -> float:
    """Signed lateral offset (m) of a point from the corridor polyline.
    Projects onto whichever segment the point falls alongside; clamps to
    vertices outside the polyline ends. +ve = right of axis (toward
    Allschwil for a SE-pointing rwy15 corridor), -ve = left.
    """
    best_abs = math.inf
    best_signed = 0.0
    for s, e in zip(corridor, corridor[1:]):
        seg_len = haversine_m(s, e)
        dxt, dat = cross_track_m(s, e, point)
        if 0 <= dat <= seg_len:                  # alongside this segment
            cand_signed, cand_abs = dxt, abs(dxt)
        else:                                    # past an endpoint -> use nearer vertex
            d_s, d_e = haversine_m(point, s), haversine_m(point, e)
            sign = 1.0 if dxt >= 0 else -1.0
            cand_abs = min(d_s, d_e)
            cand_signed = sign * cand_abs
        if cand_abs < best_abs:
            best_abs, best_signed = cand_abs, cand_signed
    return best_signed


# --------------------------------------------------------------------------
# Track classification
# --------------------------------------------------------------------------

@dataclass
class TrackPoint:
    lat: float
    lon: float
    alt_ft: float | None = None
    ts: float | None = None


@dataclass
class FlightResult:
    icao: str
    callsign: str
    n_points: int
    max_abs_offset_m: float
    mean_abs_offset_m: float
    signed_offset_at_max_m: float      # signed value at the widest point
    overflew_allschwil: bool
    min_dist_allschwil_m: float

    @property
    def side(self) -> str:
        if self.signed_offset_at_max_m > 0:
            return "right (Allschwil side)"
        if self.signed_offset_at_max_m < 0:
            return "left"
        return "on axis"


def analyse_flight(icao: str, callsign: str, points: Iterable[TrackPoint],
                   corridor: Sequence[tuple[float, float]] = CORRIDOR_15,
                   allschwil: tuple[float, float] = ALLSCHWIL,
                   allschwil_radius_m: float = ALLSCHWIL_RADIUS_M,
                   max_alt_ft: float | None = 6000.0) -> FlightResult | None:
    """Reduce one departure track to its dispersion + overflight summary.
    `max_alt_ft` ignores high-altitude points already established en route;
    set None to use every point.
    """
    offs, dists = [], []
    signed_at_max, max_abs = 0.0, -1.0
    for p in points:
        if max_alt_ft is not None and p.alt_ft is not None and p.alt_ft > max_alt_ft:
            continue
        signed = offset_from_corridor((p.lat, p.lon), corridor)
        offs.append(abs(signed))
        d_all = haversine_m((p.lat, p.lon), allschwil)
        dists.append(d_all)
        if abs(signed) > max_abs:
            max_abs, signed_at_max = abs(signed), signed
    if not offs:
        return None
    min_d = min(dists)
    return FlightResult(
        icao=icao, callsign=callsign, n_points=len(offs),
        max_abs_offset_m=max(offs), mean_abs_offset_m=mean(offs),
        signed_offset_at_max_m=signed_at_max,
        overflew_allschwil=min_d <= allschwil_radius_m,
        min_dist_allschwil_m=min_d,
    )


# --------------------------------------------------------------------------
# Fleet-wide dispersion summary  (the O.7 metric)
# --------------------------------------------------------------------------

def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


@dataclass
class DispersionSummary:
    n_flights: int
    n_overflew_allschwil: int
    pct_overflew_allschwil: float
    p50_max_offset_m: float
    p90_max_offset_m: float
    p95_max_offset_m: float
    worst: list[FlightResult] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"Flights analysed : {self.n_flights}\n"
            f"Overflew Allschwil: {self.n_overflew_allschwil} "
            f"({self.pct_overflew_allschwil:.1f} %)\n"
            f"Per-flight max lateral offset from corridor axis:\n"
            f"  median (p50): {self.p50_max_offset_m:6.0f} m\n"
            f"  p90         : {self.p90_max_offset_m:6.0f} m\n"
            f"  p95         : {self.p95_max_offset_m:6.0f} m"
        )


def summarise(results: Iterable[FlightResult], n_worst: int = 10) -> DispersionSummary:
    res = [r for r in results if r is not None]
    if not res:
        return DispersionSummary(0, 0, 0.0, *(float("nan"),) * 3)
    maxes = sorted(r.max_abs_offset_m for r in res)
    n_over = sum(r.overflew_allschwil for r in res)
    return DispersionSummary(
        n_flights=len(res),
        n_overflew_allschwil=n_over,
        pct_overflew_allschwil=100 * n_over / len(res),
        p50_max_offset_m=_percentile(maxes, 0.50),
        p90_max_offset_m=_percentile(maxes, 0.90),
        p95_max_offset_m=_percentile(maxes, 0.95),
        worst=sorted(res, key=lambda r: r.max_abs_offset_m, reverse=True)[:n_worst],
    )


# --------------------------------------------------------------------------
# Demo with synthetic tracks (replace with your ADS-B poller output)
# --------------------------------------------------------------------------

if __name__ == "__main__":
    # Compliant track: follows the corridor axis.
    compliant = [TrackPoint(lat, lon, alt) for (lat, lon), alt in (
        (_project(RWY15_THRESHOLD, RWY15_TRUE_BEARING_DEG, d), 1000 + d / 3)
        for d in range(0, 1800, 200))]

    # Overshoot: keeps runway heading straight over Allschwil instead of turning.
    overshoot = [TrackPoint(lat, lon, alt) for (lat, lon), alt in (
        (_project(RWY15_THRESHOLD, RWY15_TRUE_BEARING_DEG, d), 1000 + d / 4)
        for d in range(0, 6000, 300))]

    results = [
        analyse_flight("3c6dd1", "BSL101", compliant),
        analyse_flight("4b1820", "BSL202", overshoot),
    ]
    for r in results:
        print(f"{r.callsign:8} maxoff={r.max_abs_offset_m:6.0f} m  "
              f"{r.side:22} Allschwil={'YES' if r.overflew_allschwil else 'no':3} "
              f"(min {r.min_dist_allschwil_m:.0f} m)")
    print("-" * 60)
    print(summarise(results))

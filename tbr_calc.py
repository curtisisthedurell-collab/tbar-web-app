"""
tbr_calc.py

Engineering-unit processing for T-bar/CPT penetrometer data from .cdf files.

Input data (from rdf_parser.parse_cdf_*) is already in engineering units:
  - Penetration:      Pen (m)          → stored in dataset.position_raw
  - Tip resistance:   Tip (MPa Qc)     → stored in dataset.load_raw

The calculations applied here are:

Depth zeroing:
    Depth [m] = Pen [m] - Pen_first_sample [m]

    Pen values in the .cdf file are absolute readings from the start of the
    test; zeroing at the first sample makes depth relative to the surface.

Overburden correction:
    Overburden [MPa]
        = Unit Weight [kN/m³] × Depth [m] × (Rod Area [m²] / Tip Area [m²]) / 1000

    Unit weight × depth gives in-situ overburden stress in kPa; the rod/tip
    area ratio accounts for the push-rod displacing soil above the probe
    (analogous to the unequal end-area correction for cone penetrometers);
    /1000 converts kPa → MPa to match the resistance units.

Net tip resistance (qn,T-bar):
    qn,T-bar [MPa] = Tip Resistance [MPa] - Overburden Correction [MPa]

Undrained shear strength (Su):
    Su [kPa] = qn,T-bar [MPa] / Nk × 1000

    Su is reported in kPa (standard geotechnical convention); ×1000 converts
    MPa → kPa before dividing by the bearing factor Nk.  Nk defaults to the
    value in the file's "N Value" header field (typically 10–12 for T-bar
    tests); the user can override it in the UI.

Tip Area is read directly from the "Tip Area (mm)" header field and converted
to m²; it replaces the old diameter × length projected-area calculation.
Rod Diameter is user-editable and drives the overburden correction only.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import List, Optional, Sequence

from tbr_parser import TbarDataset

DEFAULT_UNIT_WEIGHT_KN_M3 = 16.0
DEFAULT_NK_FACTOR = 10.5
DEFAULT_TIP_AREA_MM2 = 2500.0
DEFAULT_ROD_DIAMETER_MM = 16.0


@dataclass
class CalibrationSettings:
    """User-editable geometry and analysis parameters.

    For .cdf files the tip resistance is already in MPa and penetration
    already in metres, so no sensor calibration factors are needed — only the
    geometry required for the overburden correction and Su derivation.
    """

    tip_area_mm2: float = DEFAULT_TIP_AREA_MM2
    rod_diameter_mm: float = DEFAULT_ROD_DIAMETER_MM
    unit_weight_kn_m3: float = DEFAULT_UNIT_WEIGHT_KN_M3
    nk_factor: float = DEFAULT_NK_FACTOR
    depth_reference_index: int = 0

    @classmethod
    def from_dataset(cls, dataset: TbarDataset) -> "CalibrationSettings":
        """Build defaults from parsed dataset header fields, falling back to
        sensible values so the tool never crashes on an incomplete file."""
        tip_area = dataset.header_get_float(
            "Tip Area (mm)", "Tip Area", default=DEFAULT_TIP_AREA_MM2
        )
        n_value = dataset.header_get_float("N Value", default=DEFAULT_NK_FACTOR)
        rod_dia = dataset.header_get_float(
            "Rod Diameter (mm)", "Rod Diameter", default=DEFAULT_ROD_DIAMETER_MM
        )
        return cls(
            tip_area_mm2=tip_area if tip_area else DEFAULT_TIP_AREA_MM2,
            rod_diameter_mm=rod_dia if rod_dia else DEFAULT_ROD_DIAMETER_MM,
            unit_weight_kn_m3=DEFAULT_UNIT_WEIGHT_KN_M3,
            nk_factor=n_value if n_value else DEFAULT_NK_FACTOR,
        )

    @property
    def projected_area_m2(self) -> float:
        """Tip projected area in m², converted from the mm² header value."""
        return self.tip_area_mm2 * 1e-6

    @property
    def rod_area_m2(self) -> float:
        """Push-rod circular cross-sectional area in m²."""
        return math.pi * (self.rod_diameter_mm / 2.0) ** 2 * 1e-6


def compute_overburden_correction_mpa(
    depth_m: Sequence[float], cal: CalibrationSettings
) -> List[float]:
    """Overburden correction [MPa]
    = UnitWeight [kN/m³] × Depth [m] × (RodArea / TipArea) / 1000."""
    area = cal.projected_area_m2
    if not area:
        return [0.0] * len(list(depth_m))
    ratio = cal.rod_area_m2 / area
    return [cal.unit_weight_kn_m3 * d * ratio / 1000.0 for d in depth_m]


def compute_qnt_mpa(
    resistance_mpa: Sequence[float], overburden_mpa: Sequence[float]
) -> List[float]:
    """qn,T-bar [MPa] = Tip Resistance [MPa] − Overburden Correction [MPa]."""
    return [r - o for r, o in zip(resistance_mpa, overburden_mpa)]


def compute_su_kpa(qnt_mpa: Sequence[float], cal: CalibrationSettings) -> List[float]:
    """Su [kPa] = qn,T-bar [MPa] / Nk × 1000.

    ``cal.nk_factor`` must be > 0 (validated by the caller — see
    ``web_app.read_calibration``); this is a physical bearing factor and
    silently substituting a fallback for an invalid value (e.g. 0) would
    produce a plausible-looking but wrong Su with no indication anything
    was overridden.
    """
    if cal.nk_factor <= 0:
        raise ValueError(
            f"Nk Factor must be greater than 0 (got {cal.nk_factor:g})."
        )
    return [(q * 1000.0) / cal.nk_factor for q in qnt_mpa]


@dataclass
class CycleSegment:
    """One labeled monotonic segment of the penetration trace:
    the Initial push-down, numbered remoulding Cycle N, or the Final
    withdrawal. ``start_idx``/``end_idx`` are inclusive sample indices."""

    label: str
    start_idx: int
    end_idx: int
    kind: str  # "initial", "cycle", "final"


def detect_cycles(
    depth_m: Sequence[float], noise_floor_m: float = 0.003
) -> List[CycleSegment]:
    """Split the depth trace into labeled segments based on direction changes.

    Returns Initial push, alternating Cycle N pairs, and a Final withdrawal.
    ``noise_floor_m`` filters out jitter-induced direction reversals (a real
    turning point must move at least this far in the new direction).
    """
    n = len(depth_m)
    if n < 2:
        return [CycleSegment("Initial", 0, max(n - 1, 0), "initial")]

    turning_idx = [0]
    candidate_idx = 0
    candidate_val = depth_m[0]
    direction = None
    for i in range(1, n):
        val = depth_m[i]
        if direction is None:
            if val != candidate_val:
                direction = 1 if val > candidate_val else -1
                candidate_idx, candidate_val = i, val
            continue
        if direction == 1:
            if val >= candidate_val:
                candidate_idx, candidate_val = i, val
            elif candidate_val - val >= noise_floor_m:
                turning_idx.append(candidate_idx)
                direction = -1
                candidate_idx, candidate_val = i, val
        else:
            if val <= candidate_val:
                candidate_idx, candidate_val = i, val
            elif val - candidate_val >= noise_floor_m:
                turning_idx.append(candidate_idx)
                direction = 1
                candidate_idx, candidate_val = i, val
    turning_idx.append(n - 1)
    turning_points = sorted(set(turning_idx))

    raw_segments = [
        (turning_points[i], turning_points[i + 1])
        for i in range(len(turning_points) - 1)
    ]
    if not raw_segments:
        return [CycleSegment("Initial", 0, n - 1, "initial")]
    if len(raw_segments) == 1:
        start, end = raw_segments[0]
        return [CycleSegment("Initial", start, end, "initial")]

    segments: List[CycleSegment] = []
    first_start, first_end = raw_segments[0]
    segments.append(CycleSegment("Initial", first_start, first_end, "initial"))

    middle = raw_segments[1:-1]
    last_start, last_end = raw_segments[-1]

    cycle_num = 1
    i = 0
    while i < len(middle):
        seg_start = middle[i][0]
        if i + 1 < len(middle):
            seg_end = middle[i + 1][1]
            i += 2
        else:
            seg_end = middle[i][1]
            i += 1
        segments.append(CycleSegment(f"Cycle {cycle_num}", seg_start, seg_end, "cycle"))
        cycle_num += 1

    segments.append(CycleSegment("Final", last_start, last_end, "final"))

    if len(raw_segments) == 2:
        segments = [
            CycleSegment("Initial", first_start, first_end, "initial"),
            CycleSegment("Final", last_start, last_end, "final"),
        ]
    return segments


def initial_withdrawal_end_index(
    depth_m: Sequence[float], cycles: Sequence[CycleSegment]
) -> int:
    """Index marking the end of the initial push + first withdrawal -- the
    turning point where the first remolding push-down begins, or the last
    sample if the test never gets that far (a single push, or a push and
    one withdrawal with no further cycling).

    Used to isolate the "virgin" (un-remolded) portion of a cyclic test --
    the initial penetration and the pull-back immediately after it -- from
    the remolding cycles that follow, so the initial (peak) Su reads
    cleanly on its own without the cyclic trace overlapping it.
    """
    n = len(depth_m)
    if not cycles:
        return max(n - 1, 0)
    if len(cycles) == 1:
        return cycles[0].end_idx

    second = cycles[1]
    if second.kind != "cycle":
        # Initial push + Final withdrawal only (no remolding cycles) --
        # Final *is* exactly the first withdrawal in this case.
        return second.end_idx

    # "Cycle 1" bundles the first withdrawal together with the push-down
    # that follows it, so isolate the withdrawal-only portion by finding
    # the shallowest point within it -- the turning point where the
    # withdrawal ends and the next push begins.
    lo, hi = second.start_idx, min(second.end_idx, n - 1)
    if lo >= hi:
        return lo
    window = depth_m[lo:hi + 1]
    return lo + window.index(min(window))


def compute_elapsed_seconds(timestamps) -> List[Optional[float]]:
    """Elapsed time [s] from the first valid timestamp; None for missing.

    The .cdf timestamp column only has whole-second resolution, but the
    acquisition software samples much faster (commonly ~10 Hz), so several
    consecutive rows share the same timestamp. Assigning them all the same
    elapsed time produces a "staircase" depth-vs-time plot -- flat treads
    and vertical risers -- even though the underlying signal changes
    smoothly from sample to sample.

    To recover the true sample spacing, consecutive rows sharing a
    timestamp are treated as one "bin" and spread evenly across it at a
    single nominal per-sample interval, derived (via median, so a handful
    of outliers can't skew it) from every other bin in the file rather
    than from that bin's own span to the next label. Real acquisitions
    occasionally have a bin whose label is several seconds stale even
    though the signal kept changing at the usual rate (a clock/logging
    hiccup, not an actual pause) -- spacing that bin's few samples across
    its own bogus multi-second span would smear them out and flatten the
    curve right at that point. A bin with unusually many samples for the
    nominal rate (more than would fit before the next label) falls back
    to its own span so consecutive bins never overlap in time.
    """
    n = len(timestamps)
    result: List[Optional[float]] = [None] * n

    valid_idx = [i for i, t in enumerate(timestamps) if t is not None]
    if not valid_idx:
        return result

    t0 = timestamps[valid_idx[0]]

    # Group consecutive valid samples that share the same timestamp.
    groups: List[List[int]] = []
    for i in valid_idx:
        if groups and timestamps[groups[-1][-1]] == timestamps[i]:
            groups[-1].append(i)
        else:
            groups.append([i])

    group_seconds = [(timestamps[g[0]] - t0).total_seconds() for g in groups]

    spacing_samples = [
        (group_seconds[gi + 1] - group_seconds[gi]) / len(groups[gi])
        for gi in range(len(groups) - 1)
        if len(groups[gi]) > 1 and group_seconds[gi + 1] > group_seconds[gi]
    ]
    nominal_spacing = statistics.median(spacing_samples) if spacing_samples else 0.0

    for gi, (indices, t_sec) in enumerate(zip(groups, group_seconds)):
        n_g = len(indices)
        spacing = nominal_spacing
        if gi + 1 < len(groups):
            dt = group_seconds[gi + 1] - t_sec
            if dt <= 0:
                spacing = 0.0
            elif n_g > 1 and (n_g - 1) * spacing >= dt:
                # More samples in this bin than the nominal rate can fit
                # before the next label -- spread across its own span
                # instead, to avoid overlapping the next bin.
                spacing = dt / n_g
        for k, idx in enumerate(indices):
            result[idx] = t_sec + k * spacing

    return result


@dataclass
class ComputedSeries:
    """Fully processed engineering-unit series ready for plotting/export."""

    depth_m: List[float]
    resistance_mpa: List[float]
    elapsed_s: List[Optional[float]]
    overburden_mpa: List[float]
    qnt_mpa: List[float]
    su_kpa: List[float]
    cycles: List[CycleSegment]


def compute_series(dataset: TbarDataset, cal: CalibrationSettings) -> ComputedSeries:
    """Compute the full engineering series from a parsed .cdf dataset."""
    resistance_mpa = list(dataset.load_raw)   # Tip (MPa Qc) from .cdf

    pen = list(dataset.position_raw)           # Pen (m) from .cdf
    if pen:
        ref_idx = min(max(cal.depth_reference_index, 0), len(pen) - 1)
        ref = pen[ref_idx]
        depth_m = [p - ref for p in pen]
    else:
        depth_m = []

    elapsed_s = compute_elapsed_seconds(dataset.timestamps)
    overburden_mpa = compute_overburden_correction_mpa(depth_m, cal)
    qnt_mpa = compute_qnt_mpa(resistance_mpa, overburden_mpa)
    su_kpa = compute_su_kpa(qnt_mpa, cal)
    cycles = detect_cycles(depth_m)

    return ComputedSeries(
        depth_m=depth_m,
        resistance_mpa=resistance_mpa,
        elapsed_s=elapsed_s,
        overburden_mpa=overburden_mpa,
        qnt_mpa=qnt_mpa,
        su_kpa=su_kpa,
        cycles=cycles,
    )

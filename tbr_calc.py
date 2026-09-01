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


def compute_elapsed_seconds(timestamps) -> List[Optional[float]]:
    """Elapsed time [s] from the first valid timestamp; None for missing."""
    valid = [t for t in timestamps if t is not None]
    if not valid:
        return [None] * len(timestamps)
    t0 = valid[0]
    return [
        (t - t0).total_seconds() if t is not None else None
        for t in timestamps
    ]


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

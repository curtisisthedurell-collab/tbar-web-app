"""
tbar_plotting.py

Shared matplotlib plotting helpers used by both the live GUI preview
(tbar_gui.py, Qt canvas) and the PDF export renderer (tbar_pdf.py, headless
Agg backend). Kept separate so the cycle-color-coding logic is defined once
and stays visually identical between the on-screen preview and the exported
PDF.

Color coding follows standard mini T-bar cyclic remolding test convention:
    - "Initial" push (first push to test depth): distinct accent color.
    - "Cycle 1", "Cycle 2", ...: each pull-up + push-down round trip gets
      its own color from a repeating, colorblind-friendly palette.
    - "Final" withdrawal (last pull-out back toward the surface): distinct
      accent color, visually paired with "Initial" (same hue family) since
      together they bookend the cyclic sequence.

See :func:`tbr_calc.detect_cycles` for how segments are identified.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from tbr_calc import CycleSegment

# Distinct colors for "Initial" and "Final" (bookend segments).
INITIAL_COLOR = "#1f4e79"  # dark blue
FINAL_COLOR = "#b5651d"    # burnt orange

# Repeating palette for numbered cycles (colorblind-friendly, avoids the
# blue/orange used by Initial/Final so cycles are visually distinct).
CYCLE_PALETTE = [
    "#2ca02c",  # green
    "#9467bd",  # purple
    "#17becf",  # teal
    "#e377c2",  # pink
    "#bcbd22",  # olive
    "#7f7f7f",  # gray
    "#8c564b",  # brown
    "#d62728",  # red
]


def color_for_segment(seg: CycleSegment, cycle_index: Optional[int] = None) -> str:
    """Return the display color for one cycle segment. ``cycle_index`` (0
    based) selects a color from the repeating palette for "cycle" segments;
    if omitted it is parsed from the segment's own label ("Cycle N")."""
    if seg.kind == "initial":
        return INITIAL_COLOR
    if seg.kind == "final":
        return FINAL_COLOR
    if cycle_index is None:
        try:
            cycle_index = int(seg.label.split()[-1]) - 1
        except (ValueError, IndexError):
            cycle_index = 0
    return CYCLE_PALETTE[cycle_index % len(CYCLE_PALETTE)]


def short_label(seg: CycleSegment) -> str:
    """Short legend label, e.g. 'Initial', 'C1', 'C2', ..., 'Final'."""
    if seg.kind == "cycle":
        try:
            n = seg.label.split()[-1]
            return f"C{n}"
        except IndexError:
            return seg.label
    return seg.label


def plot_series_by_cycle(
    ax,
    x_values: Sequence[Optional[float]],
    y_values: Sequence[float],
    cycles: List[CycleSegment],
    linewidth: float = 1.4,
    show_legend: bool = True,
    legend_kwargs: Optional[dict] = None,
    single_color: Optional[str] = None,
) -> None:
    """Plot ``x_values``/``y_values`` on ``ax`` as a series of colored
    segments, one per entry in ``cycles`` (see :func:`tbr_calc.detect_cycles`),
    with a legend mapping each color to its segment label (Initial, C1, C2,
    ..., Final). Consecutive segments are drawn overlapping by one sample so
    the plotted line has no visual gaps at segment boundaries.

    ``x_values``/``y_values`` may contain ``None`` entries (e.g. missing
    timestamps); points with a ``None`` on either axis are simply skipped,
    which can leave small gaps but never crashes.

    ``single_color``, when given, overrides the whole cycle-colour scheme:
    the entire trace is drawn as one line in that color with no legend
    (a high cycle-count test can otherwise look "manic" with a different
    color per cycle).
    """
    if single_color is not None:
        pts = [
            (x, y) for x, y in zip(x_values, y_values)
            if x is not None and y is not None
        ]
        if pts:
            px = [p[0] for p in pts]
            py = [p[1] for p in pts]
            ax.plot(px, py, color=single_color, linewidth=linewidth, solid_capstyle="round")
        return

    if not cycles:
        ax.plot(x_values, y_values, color=INITIAL_COLOR, linewidth=linewidth)
        return

    cycle_counter = 0
    handles = []
    labels_seen = []
    for seg in cycles:
        if seg.kind == "cycle":
            color = color_for_segment(seg, cycle_counter)
            cycle_counter += 1
        else:
            color = color_for_segment(seg)

        start = seg.start_idx
        end = min(seg.end_idx + 1, len(x_values) - 1)  # +1 sample overlap
        xs = x_values[start:end + 1]
        ys = y_values[start:end + 1]
        # Filter out None points (keeps index alignment simple since we
        # only drop points, we don't need to remap positions for plotting).
        pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
        if not pts:
            continue
        px = [p[0] for p in pts]
        py = [p[1] for p in pts]
        line, = ax.plot(px, py, color=color, linewidth=linewidth, solid_capstyle="round")
        lbl = short_label(seg)
        if lbl not in labels_seen:
            handles.append(line)
            labels_seen.append(lbl)

    if show_legend and handles:
        kwargs = dict(
            loc="best", fontsize=7, framealpha=0.9, ncol=1,
            title=None, borderpad=0.6, handlelength=1.6,
        )
        if legend_kwargs:
            kwargs.update(legend_kwargs)
        ax.legend(handles, labels_seen, **kwargs)

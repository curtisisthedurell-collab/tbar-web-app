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

from matplotlib.lines import Line2D

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
    highlight_last_n_cycles: Optional[int] = None,
    highlight_color: str = "#ff0000",
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

    ``highlight_last_n_cycles``, when given (and greater than zero), takes
    precedence over both of the above: the whole trace is drawn in
    ``single_color`` (or a default) as a base layer, then the last N "cycle"
    segments (the final remolding pull/push round trips) are redrawn on top
    in ``highlight_color``, slightly thicker, so the peak (initial push) and
    the remolded/softened tail are both visible at a glance against a busy
    stack of overlapping traces.
    """
    if highlight_last_n_cycles is not None and highlight_last_n_cycles > 0:
        _plot_with_highlighted_tail(
            ax, x_values, y_values, cycles, linewidth,
            base_color=single_color or INITIAL_COLOR,
            highlight_color=highlight_color,
            n=highlight_last_n_cycles,
            show_legend=show_legend,
            legend_kwargs=legend_kwargs,
        )
        return

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


def _plot_with_highlighted_tail(
    ax,
    x_values: Sequence[Optional[float]],
    y_values: Sequence[float],
    cycles: List[CycleSegment],
    linewidth: float,
    base_color: str,
    highlight_color: str,
    n: int,
    show_legend: bool,
    legend_kwargs: Optional[dict],
) -> None:
    """Draw the whole trace in ``base_color``, then redraw the last ``n``
    "cycle" segments in ``highlight_color`` on top (thicker, higher
    z-order) so they stand out from a stack of overlapping traces."""
    base_pts = [
        (x, y) for x, y in zip(x_values, y_values)
        if x is not None and y is not None
    ]
    if base_pts:
        bx = [p[0] for p in base_pts]
        by = [p[1] for p in base_pts]
        ax.plot(bx, by, color=base_color, linewidth=linewidth,
                solid_capstyle="round", zorder=2)

    cycle_segs = [seg for seg in cycles if seg.kind == "cycle"]
    tail_segs = cycle_segs[-n:] if cycle_segs else []

    handles = []
    labels_seen = []
    if tail_segs:
        handles.append(Line2D([], [], color=base_color, linewidth=linewidth))
        labels_seen.append("Trace")

    for seg in tail_segs:
        start = seg.start_idx
        end = min(seg.end_idx + 1, len(x_values) - 1)  # +1 sample overlap
        xs = x_values[start:end + 1]
        ys = y_values[start:end + 1]
        pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
        if not pts:
            continue
        px = [p[0] for p in pts]
        py = [p[1] for p in pts]
        ax.plot(px, py, color=highlight_color, linewidth=linewidth * 1.4,
                 solid_capstyle="round", zorder=5)

    if tail_segs:
        handles.append(Line2D([], [], color=highlight_color, linewidth=linewidth * 1.4))
        labels_seen.append(f"Last {len(tail_segs)} cycle{'s' if len(tail_segs) != 1 else ''}")

    if show_legend and handles:
        kwargs = dict(
            loc="best", fontsize=7, framealpha=0.9, ncol=1,
            title=None, borderpad=0.6, handlelength=1.6,
        )
        if legend_kwargs:
            kwargs.update(legend_kwargs)
        ax.legend(handles, labels_seen, **kwargs)

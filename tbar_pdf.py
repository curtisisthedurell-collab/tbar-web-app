"""
tbar_pdf.py

Builds a landscape, two-page mini T-bar report PDF containing:
  - A title/metadata block of fixed (non-editable) text -- Project, Client,
    Location ID / Box Core Number, Test Date, Processed By, Cone ID, Unit
    Weight, and Nk -- all of which are edited beforehand in the GUI. The
    exported PDF is a fixed lab test record and intentionally does NOT
    contain editable form fields.
  - Two plots, rendered by matplotlib at the axis scales chosen in the GUI,
    placed side by side: (qn,T-bar or Su) vs Depth, and (qn,T-bar or Su) vs
    Time, depending on which series the user chose to display.

All chart axis scales are supplied by the caller (``PlotAxisScale``) so this
module never guesses limits itself -- that responsibility lives in the GUI,
where the user can edit them because T-bar tests vary in length/force.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence

import matplotlib

matplotlib.use("Agg")  # headless rendering backend for PDF export
import matplotlib.pyplot as plt

from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.units import mm
from reportlab.lib.colors import black, white, HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdfcanvas

from tbr_calc import CycleSegment
from tbar_plotting import plot_series_by_cycle


PAGE_SIZE = landscape(A4)  # 842 x 595 pt landscape

# Single accent color family used throughout the report (matches the
# "Initial" push color in the cycle plots so the report reads as one
# coherent, deliberately-designed document rather than default matplotlib
# output pasted onto a plain page).
ACCENT = HexColor("#66FF33")        # house green -- bands, section headers, rules
ACCENT_DARK = HexColor("#4db300")   # darker green -- sub-rule beneath header band
ACCENT_LIGHT = HexColor("#efffea")  # light green tint -- card backgrounds
ACCENT_TEXT = HexColor("#1a3300")   # dark green -- text on green band
RULE_GRAY = HexColor("#b8d9b0")
TEXT_GRAY = HexColor("#3d6b2e")


@dataclass
class PlotAxisScale:
    """Editable axis scale for one plot. ``auto=True`` lets matplotlib pick
    the limits from the data; otherwise ``min``/``max`` are used verbatim."""

    x_auto: bool = True
    x_min: Optional[float] = None
    x_max: Optional[float] = None
    y_auto: bool = True
    y_min: Optional[float] = None
    y_max: Optional[float] = None


@dataclass
class ReportMetadata:
    """Fixed title-block text shown on the PDF, edited beforehand in the
    GUI. The exported PDF renders these as plain (non-editable) text."""

    project: str = ""
    client: str = ""
    location_id: str = ""
    test_date: str = ""
    operator: str = ""
    cone_id: str = ""
    unit_weight_kn_m3: str = ""
    nk_factor: str = ""
    comments: str = ""
    source_filename: str = ""
    resistance_label: str = "qn,T-bar (MPa)"

    # Optional logos (raw PNG/JPG/GIF bytes). When supplied they are drawn
    # in the PDF footer: company logo on the left, client logo on the right.
    # Each logo is scaled to fit within a 120 x 28 pt box while preserving
    # its aspect ratio, so any reasonable image file will render cleanly.
    company_logo_bytes: Optional[bytes] = None
    client_logo_bytes: Optional[bytes] = None


def _style_axes(ax) -> None:
    """Shared modern-report styling applied to every chart axes."""
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color("#8a96a1")
    ax.tick_params(colors="#3a444d", labelsize=8)
    ax.title.set_color("#1f2d3a")
    ax.title.set_fontweight("bold")
    ax.xaxis.label.set_color("#3a444d")
    ax.yaxis.label.set_color("#3a444d")
    ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.45, color="#8a96a1")
    ax.set_facecolor("#fbfcfd")


def _render_resistance_depth_png(
    depth_m: Sequence[float],
    resistance_series: Sequence[float],
    resistance_label: str,
    scale: PlotAxisScale,
    width_in: float,
    height_in: float,
    cycles: Optional[List[CycleSegment]] = None,
    single_color: Optional[str] = None,
    highlight_last_n_cycles: Optional[int] = None,
    highlight_color: str = "#ff0000",
) -> bytes:
    fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=220)
    fig.patch.set_facecolor("white")
    plot_series_by_cycle(
        ax, resistance_series, depth_m, cycles or [],
        linewidth=1.3, show_legend=True,
        legend_kwargs=dict(fontsize=6.5, loc="best", framealpha=0.92),
        single_color=single_color,
        highlight_last_n_cycles=highlight_last_n_cycles,
        highlight_color=highlight_color,
    )
    ax.set_xlabel(resistance_label)
    ax.set_ylabel("Depth (m)")
    _title_label = re.sub(r'\s*\([^)]*\)', '', resistance_label).strip()
    ax.set_title(f"{_title_label} vs Depth", fontsize=10.5, pad=8)
    ax.invert_yaxis()  # depth increases downward, geotechnical convention
    _style_axes(ax)

    if not scale.x_auto and scale.x_min is not None and scale.x_max is not None:
        ax.set_xlim(scale.x_min, scale.x_max)
    if not scale.y_auto and scale.y_min is not None and scale.y_max is not None:
        # y-axis is inverted for depth; pass (max, min) so "min" stays at
        # the bottom visually only if user intends that -- we honour the
        # literal values requested, then re-apply inversion ordering.
        lo, hi = sorted((scale.y_min, scale.y_max))
        ax.set_ylim(hi, lo)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def _draw_label(c: pdfcanvas.Canvas, x: float, y: float, text: str) -> None:
    c.setFont("Helvetica-Bold", 7.5)
    c.setFillColor(TEXT_GRAY)
    c.drawString(x, y, text.upper())


def _draw_value(c: pdfcanvas.Canvas, x: float, y: float, text: str, width: float = 0) -> None:
    """Draw a fixed (non-editable) value with an underline rule beneath it,
    visually matching a filled-in report field without creating an
    AcroForm widget -- the exported PDF is a fixed lab record."""
    c.setFont("Helvetica", 9.5)
    c.setFillColor(black)
    c.drawString(x, y, text or "")
    if width:
        c.setStrokeColor(RULE_GRAY)
        c.setLineWidth(0.6)
        c.line(x, y - 4, x + width, y - 4)


def _draw_section_card(
    c: pdfcanvas.Canvas, x: float, y_top: float, width: float, height: float,
) -> None:
    """Light-tint rounded 'card' background used behind the metadata block,
    giving the report a modern, sectioned look rather than plain text
    floating on a white page."""
    c.saveState()
    c.setFillColor(ACCENT_LIGHT)
    c.setStrokeColor(RULE_GRAY)
    c.setLineWidth(0.6)
    c.roundRect(x, y_top - height, width, height, 6, fill=1, stroke=1)
    c.restoreState()


def _draw_section_heading(c: pdfcanvas.Canvas, x: float, y: float, text: str) -> None:
    """Small-caps accent-colored section heading (e.g. 'TEST METADATA')."""
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(ACCENT)
    c.drawString(x, y, text.upper())


def _draw_footer_logo(
    c: pdfcanvas.Canvas,
    logo_bytes: bytes,
    x_left: Optional[float],
    x_right: Optional[float],
    y_bottom: float,
    max_w: float = 120.0,
    max_h: float = 28.0,
) -> None:
    """Draw a logo image in the footer, scaled to fit within max_w x max_h pt
    while preserving aspect ratio.

    Specify either ``x_left`` (left-edge anchor) or ``x_right`` (right-edge
    anchor), leaving the other as None.  The image is clipped by a transparent
    mask so white backgrounds blend cleanly against the page.
    """
    try:
        ir = ImageReader(io.BytesIO(logo_bytes))
        img_w, img_h = ir.getSize()
        if img_w <= 0 or img_h <= 0:
            return
        scale = min(max_w / img_w, max_h / img_h)
        draw_w = img_w * scale
        draw_h = img_h * scale
        if x_left is not None:
            draw_x = x_left
        else:
            draw_x = x_right - draw_w
        # Vertically centre within the logo strip
        draw_y = y_bottom + (max_h - draw_h) / 2.0
        c.drawImage(
            ir, draw_x, draw_y, width=draw_w, height=draw_h,
            preserveAspectRatio=True, mask="auto",
        )
    except Exception:
        # Silently skip a corrupt/unsupported image rather than crashing the
        # entire PDF export -- the user will see the footer without the logo.
        pass


def _render_resistance_time_png(
    elapsed_s: Sequence[Optional[float]],
    resistance_series: Sequence[float],
    resistance_label: str,
    scale: PlotAxisScale,
    width_in: float,
    height_in: float,
    cycles: Optional[List[CycleSegment]] = None,
    single_color: Optional[str] = None,
    highlight_last_n_cycles: Optional[int] = None,
    highlight_color: str = "#ff0000",
) -> bytes:
    fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=220)
    fig.patch.set_facecolor("white")
    plot_series_by_cycle(
        ax, elapsed_s, resistance_series, cycles or [],
        linewidth=1.3, show_legend=True,
        legend_kwargs=dict(fontsize=6.5, loc="upper right", framealpha=0.92),
        single_color=single_color,
        highlight_last_n_cycles=highlight_last_n_cycles,
        highlight_color=highlight_color,
    )
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(resistance_label)
    _title_label = re.sub(r'\s*\([^)]*\)', '', resistance_label).strip()
    ax.set_title(f"{_title_label} vs Time", fontsize=10.5, pad=8)
    _style_axes(ax)

    if not scale.x_auto and scale.x_min is not None and scale.x_max is not None:
        ax.set_xlim(scale.x_min, scale.x_max)
    if not scale.y_auto and scale.y_min is not None and scale.y_max is not None:
        ax.set_ylim(scale.y_min, scale.y_max)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def _render_depth_time_png(
    elapsed_s: Sequence[Optional[float]],
    depth_m: Sequence[float],
    scale: PlotAxisScale,
    width_in: float,
    height_in: float,
    cycles: Optional[List[CycleSegment]] = None,
    single_color: Optional[str] = None,
    highlight_last_n_cycles: Optional[int] = None,
    highlight_color: str = "#ff0000",
) -> bytes:
    """Depth (inverted y-axis) vs elapsed time.
    Uses the same cycle colour scheme as the resistance plots."""
    fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=220)
    fig.patch.set_facecolor("white")
    plot_series_by_cycle(
        ax, elapsed_s, depth_m, cycles or [],
        linewidth=1.3, show_legend=True,
        legend_kwargs=dict(fontsize=6.5, loc="best", framealpha=0.92),
        single_color=single_color,
        highlight_last_n_cycles=highlight_last_n_cycles,
        highlight_color=highlight_color,
    )
    ax.set_xlabel("Elapsed Time (s)")
    ax.set_ylabel("Depth (m)")
    ax.set_title("Depth vs Elapsed Time", fontsize=10.5, pad=8)
    _style_axes(ax)

    if not scale.x_auto and scale.x_min is not None and scale.x_max is not None:
        ax.set_xlim(scale.x_min, scale.x_max)
    if not scale.y_auto and scale.y_min is not None and scale.y_max is not None:
        # y-axis is inverted for depth; honour the literal min/max requested,
        # then re-apply inversion ordering (same convention as the
        # resistance-vs-depth plot).
        lo, hi = sorted((scale.y_min, scale.y_max))
        ax.set_ylim(hi, lo)
    else:
        ax.invert_yaxis()

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def build_pdf(
    output_path: str,
    metadata: ReportMetadata,
    depth_m: Sequence[float],
    resistance_series: Sequence[float],
    elapsed_s: Sequence[Optional[float]],
    depth_scale: PlotAxisScale,
    time_scale: PlotAxisScale,
    cycles: Optional[List[CycleSegment]] = None,
    qa_scale: Optional[PlotAxisScale] = None,
    single_color: Optional[str] = None,
    highlight_last_n_cycles: Optional[int] = None,
    highlight_color: str = "#ff0000",
) -> None:
    """Build the landscape mini T-bar report PDF at ``output_path``.

    ``resistance_series`` is whichever series the GUI currently has
    selected for plotting (qn,T-bar or Su); ``metadata.resistance_label``
    supplies the matching axis label/title text. ``cycles`` (see
    :func:`tbr_calc.detect_cycles`) drives the color-coded Initial/Cycle
    N/Final segments and legend on both plots, unless ``single_color`` is
    given, in which case every plot (including the page 2 QA plot) is drawn
    as one uniform-color trace with no legend instead. If
    ``highlight_last_n_cycles`` is also given, the trace is instead drawn in
    ``single_color`` with only the last N remolding cycles redrawn on top in
    ``highlight_color`` for at-a-glance peak/remolded comparison. ``qa_scale``
    sets the axis limits for the page 2 Depth vs Elapsed Time QA plot
    (defaults to fully automatic if omitted). The PDF is a fixed lab record:
    all metadata is drawn as plain text, not editable form fields.
    """
    if qa_scale is None:
        qa_scale = PlotAxisScale()
    page_w, page_h = PAGE_SIZE
    margin = 15 * mm

    c = pdfcanvas.Canvas(output_path, pagesize=PAGE_SIZE)
    c.setTitle(f"Mini T-Bar Report - {metadata.location_id or 'Untitled'}")

    # ---- Header band --------------------------------------------------
    # A full-width accent-colored banner behind the title gives the report
    # an immediate "designed document" look instead of plain text on white.
    band_h = 34
    c.setFillColor(ACCENT)
    c.rect(0, page_h - band_h, page_w, band_h, fill=1, stroke=0)
    # Thin darker accent rule directly beneath the band for depth.
    c.setFillColor(ACCENT_DARK)
    c.rect(0, page_h - band_h - 3, page_w, 3, fill=1, stroke=0)

    c.setFont("Helvetica-Bold", 17)
    c.setFillColor(ACCENT_TEXT)
    c.drawString(margin, page_h - band_h + 11, "CYCLIC T-BAR TEST REPORT")

    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(HexColor("#1a3300"))
    subtitle = metadata.location_id or "Untitled Test"
    c.drawRightString(page_w - margin, page_h - band_h + 12, subtitle)

    # ---- Title block ------------------------------------------------
    title_top = page_h - band_h - 3

    row_gap = 26
    label_offset = 11

    card_top = title_top - 8
    card_height = 2 * row_gap + 16
    _draw_section_card(c, margin - 8, card_top, page_w - 2 * margin + 16, card_height)

    # Row 1: Project | Client | Location ID | Test Date | Processed By
    row1_y = card_top - 26
    _draw_label(c, margin, row1_y + label_offset, "Project")
    _draw_value(c, margin, row1_y, metadata.project, 120)

    _draw_label(c, margin + 152, row1_y + label_offset, "Client")
    _draw_value(c, margin + 152, row1_y, metadata.client, 120)

    _draw_label(c, margin + 304, row1_y + label_offset, "Location ID")
    _draw_value(c, margin + 304, row1_y, metadata.location_id, 120)

    _draw_label(c, margin + 456, row1_y + label_offset, "Test Date")
    _draw_value(c, margin + 456, row1_y, metadata.test_date, 120)

    _draw_label(c, margin + 608, row1_y + label_offset, "Processed By")
    _draw_value(c, margin + 608, row1_y, metadata.operator, 145)

    # Row 2: Cone ID | Unit Weight | Nk Factor -- grouped together on the
    # left rather than spread across the full card width.
    row2_y = row1_y - row_gap
    _draw_label(c, margin, row2_y + label_offset, "Cone ID")
    _draw_value(c, margin, row2_y, metadata.cone_id, 75)

    _draw_label(c, margin + 105, row2_y + label_offset, "Unit Weight (kN/m³)")
    _draw_value(c, margin + 105, row2_y, metadata.unit_weight_kn_m3, 75)

    _draw_label(c, margin + 255, row2_y + label_offset, "Nk Factor")
    _draw_value(c, margin + 255, row2_y, metadata.nk_factor, 60)

    # ---- Plots --------------------------------------------------------
    # footer_h: vertical space reserved below the plots for the footer.
    # Increased from 20 to 44 pt when logos are present (28 pt logo strip +
    # 2 pt gap + 14 pt text/rule strip); kept at 20 when no logos are used
    # so the plots fill the full available height as before.
    has_logos = bool(metadata.company_logo_bytes or metadata.client_logo_bytes)
    # footer_h = 60 when logos present: logo strip top at margin+48, roundRect
    # bottom at margin+56 (= plots_bottom-4 = margin+60-4), giving 8pt clearance.
    # Without logos, 20pt as before so the plots fill the full available height.
    footer_h = 60 if has_logos else 20
    footer_y_base = 8 * mm  # smaller bottom margin so less white space below footer
    plots_top = row2_y - 24  # 8pt gap below card, matching the 8pt gap above it

    plot_area_h = plots_top - footer_y_base - footer_h
    plot_w = (page_w - 2 * margin - 10) / 2.0
    plot_h = plot_area_h

    depth_png = _render_resistance_depth_png(
        depth_m, resistance_series, metadata.resistance_label, depth_scale,
        width_in=plot_w / 72.0, height_in=plot_h / 72.0, cycles=cycles,
        single_color=single_color,
        highlight_last_n_cycles=highlight_last_n_cycles,
        highlight_color=highlight_color,
    )
    time_png = _render_resistance_time_png(
        elapsed_s, resistance_series, metadata.resistance_label, time_scale,
        width_in=plot_w / 72.0, height_in=plot_h / 72.0, cycles=cycles,
        single_color=single_color,
        highlight_last_n_cycles=highlight_last_n_cycles,
        highlight_color=highlight_color,
    )

    img1 = ImageReader(io.BytesIO(depth_png))
    img2 = ImageReader(io.BytesIO(time_png))

    plots_bottom = footer_y_base + footer_h
    c.setStrokeColor(RULE_GRAY)
    c.setLineWidth(0.6)
    c.roundRect(margin - 4, plots_bottom - 4, plot_w + 8, plot_h + 8, 4, fill=0, stroke=1)
    c.roundRect(
        margin + plot_w + 10 - 4, plots_bottom - 4, plot_w + 8, plot_h + 8, 4,
        fill=0, stroke=1,
    )

    c.drawImage(
        img1, margin, plots_bottom, width=plot_w, height=plot_h,
        preserveAspectRatio=False, anchor="sw",
    )
    c.drawImage(
        img2, margin + plot_w + 10, plots_bottom, width=plot_w, height=plot_h,
        preserveAspectRatio=False, anchor="sw",
    )

    # ---- Footer ---------------------------------------------------------
    # All vertical positions are relative to footer_y_base (8 mm from page
    # bottom) rather than the full side margin, so there is less dead space
    # below the footer. Horizontal extents still use margin so the rule and
    # text align with the plot edges.

    # Logos (drawn first so the rule line renders on top if they overlap)
    if has_logos:
        logo_y_bottom = footer_y_base + 20
        if metadata.company_logo_bytes:
            _draw_footer_logo(
                c, metadata.company_logo_bytes,
                x_left=margin, x_right=None,
                y_bottom=logo_y_bottom,
            )
        if metadata.client_logo_bytes:
            _draw_footer_logo(
                c, metadata.client_logo_bytes,
                x_left=None, x_right=page_w - margin,
                y_bottom=logo_y_bottom,
            )

    # Rule line
    rule_y = footer_y_base + 14
    c.setStrokeColor(RULE_GRAY)
    c.setLineWidth(0.6)
    c.line(margin, rule_y, page_w - margin, rule_y)

    # Small-print text row
    text_y = footer_y_base + 2
    c.setFont("Helvetica", 7.5)
    c.setFillColor(TEXT_GRAY)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    c.drawString(margin, text_y, f"Generated {generated}  |  Mini T-Bar Processing Tool")
    c.drawRightString(page_w - margin, text_y, "Page 1 of 2")

    c.showPage()

    # ====================================================================
    # Page 2: Depth Encoder QA
    # Full-width depth-vs-time plot so the engineer can check the encoder
    # trace for slip, dropout, or non-physical reversals.
    # ====================================================================

    # Header band (same design language as page 1)
    c.setFillColor(ACCENT)
    c.rect(0, page_h - band_h, page_w, band_h, fill=1, stroke=0)
    c.setFillColor(ACCENT_DARK)
    c.rect(0, page_h - band_h - 3, page_w, 3, fill=1, stroke=0)

    c.setFont("Helvetica-Bold", 17)
    c.setFillColor(ACCENT_TEXT)
    c.drawString(margin, page_h - band_h + 11, "PENETRATION QA")

    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(HexColor("#1a3300"))
    c.drawRightString(page_w - margin, page_h - band_h + 12, subtitle)

    # Compact single-row metadata card (Project | Location | Date)
    qa_card_top = page_h - band_h - 3 - 8
    qa_card_h = row_gap + 16
    _draw_section_card(c, margin - 8, qa_card_top, page_w - 2 * margin + 16, qa_card_h)

    qa_row_y = qa_card_top - 24
    _draw_label(c, margin, qa_row_y + label_offset, "Project")
    _draw_value(c, margin, qa_row_y, metadata.project, 120)
    _draw_label(c, margin + 152, qa_row_y + label_offset, "Location ID")
    _draw_value(c, margin + 152, qa_row_y, metadata.location_id, 120)
    _draw_label(c, margin + 304, qa_row_y + label_offset, "Test Date")
    _draw_value(c, margin + 304, qa_row_y, metadata.test_date, 120)

    # Full-width QA plot -- same footer_h as page 1 so logos align identically
    qa_plot_top = qa_row_y - 30   # 30pt gap clears the card border cleanly
    qa_plot_area_h = qa_plot_top - footer_y_base - footer_h
    qa_plot_w = page_w - 2 * margin

    qa_png = _render_depth_time_png(
        elapsed_s, depth_m, qa_scale,
        width_in=qa_plot_w / 72.0,
        height_in=qa_plot_area_h / 72.0,
        cycles=cycles,
        single_color=single_color,
        highlight_last_n_cycles=highlight_last_n_cycles,
        highlight_color=highlight_color,
    )
    qa_img = ImageReader(io.BytesIO(qa_png))
    qa_plots_bottom = footer_y_base + footer_h
    c.setStrokeColor(RULE_GRAY)
    c.setLineWidth(0.6)
    c.roundRect(
        margin - 4, qa_plots_bottom - 4, qa_plot_w + 8, qa_plot_area_h + 8,
        4, fill=0, stroke=1,
    )
    c.drawImage(
        qa_img, margin, qa_plots_bottom, width=qa_plot_w, height=qa_plot_area_h,
        preserveAspectRatio=False, anchor="sw",
    )

    # Page 2 footer -- logos (same positions as page 1) + rule + text
    if has_logos:
        logo_y_bottom = footer_y_base + 20
        if metadata.company_logo_bytes:
            _draw_footer_logo(
                c, metadata.company_logo_bytes,
                x_left=margin, x_right=None,
                y_bottom=logo_y_bottom,
            )
        if metadata.client_logo_bytes:
            _draw_footer_logo(
                c, metadata.client_logo_bytes,
                x_left=None, x_right=page_w - margin,
                y_bottom=logo_y_bottom,
            )

    c.setStrokeColor(RULE_GRAY)
    c.setLineWidth(0.6)
    c.line(margin, rule_y, page_w - margin, rule_y)
    c.setFont("Helvetica", 7.5)
    c.setFillColor(TEXT_GRAY)
    c.drawString(margin, text_y, f"Generated {generated}  |  Mini T-Bar Processing Tool")
    c.drawRightString(page_w - margin, text_y, "Page 2 of 2")

    c.showPage()
    c.save()
"""
web_app.py

Streamlit web port of the Mini T-Bar Processing Tool.

Processing pipeline:

    1. Upload a .cdf file from the Seabed Screw T-bar / CPT acquisition
       software (version 2.51). The file is parsed entirely in memory
       (``rdf_parser.parse_cdf_bytes``) -- nothing touches the server's disk
       and nothing persists between sessions. The .cdf format contains
       pre-calibrated engineering-unit data (Pen in metres, Tip in MPa, etc.)
       so no raw sensor calibration is required.
    2. The report-metadata and geometry forms pre-fill from the file's header
       section and remain fully editable.
    3. Two live previews -- (qn,T-bar or Su) vs Depth (inverted depth axis)
       and vs Time -- colour-coded Initial / Cycle N / Final segments.
    4. Download buttons produce the landscape PDF lab report and an auditable
       live-formula Excel workbook.

Access control: a shared passcode is read from ``st.secrets["TBAR_PASSCODE"]``
(Streamlit Cloud / secrets.toml) or the ``TBAR_PASSCODE`` environment
variable (fallback). With no passcode configured anywhere, the app runs
open with a visible warning banner so local development stays frictionless.

Run locally:
    python -m streamlit run web_app.py
"""

from __future__ import annotations

import hashlib
import io
import os
import pathlib
import re
from typing import Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import streamlit as st

from rdf_parser import parse_cdf_bytes
from tbr_calc import CalibrationSettings, compute_series
from tbar_excel import build_excel
from tbar_pdf import PlotAxisScale, ReportMetadata, build_pdf
from tbar_plotting import plot_series_by_cycle

APP_TITLE = "Mini T-Bar Processing Tool"


# ---------------------------------------------------------------------------
# Default logos (bundled in assets/ alongside this file)
# ---------------------------------------------------------------------------

def _load_default_logo(filename: str) -> Optional[bytes]:
    try:
        return (pathlib.Path(__file__).parent / "assets" / filename).read_bytes()
    except Exception:
        return None

_DEFAULT_COMPANY_LOGO: Optional[bytes] = _load_default_logo("default_company_logo.png")
_DEFAULT_CLIENT_LOGO: Optional[bytes] = _load_default_logo("default_client_logo.png")
SERIES_QNT = "qn,T-bar (MPa)"
SERIES_SU = "Su (kPa)"


# ---------------------------------------------------------------------------
# Passcode gate
# ---------------------------------------------------------------------------

def _expected_passcode() -> str:
    try:
        secret = st.secrets.get("TBAR_PASSCODE", "")
        if secret:
            return str(secret)
    except Exception:
        pass
    return os.environ.get("TBAR_PASSCODE", "")


def access_gate() -> None:
    expected = _expected_passcode()
    if not expected:
        st.warning(
            "**Open access:** no `TBAR_PASSCODE` secret is configured, so "
            "anyone with this URL can use the tool. Set the secret before "
            "sharing the link publicly (locally: `.streamlit/secrets.toml`; "
            "on Streamlit Community Cloud: app *Settings > Secrets*)."
        )
        return
    if st.session_state.get("auth_ok"):
        return
    st.title(APP_TITLE)
    st.markdown("Enter the shared access passcode to use the tool.")
    password = st.text_input("Access passcode", type="password")
    if st.button("Unlock", type="primary"):
        if password == expected:
            st.session_state.auth_ok = True
            st.rerun()
        else:
            st.error("Incorrect passcode.")
    st.stop()


# ---------------------------------------------------------------------------
# Parsing and form helpers
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner="Parsing .cdf file…")
def parse_uploaded(data: bytes, name: str):
    """Parse uploaded bytes into (TbarDataset, baseline CalibrationSettings).

    Cached on the raw bytes so repeated Streamlit re-runs (one per widget
    interaction) do not re-parse the file."""
    dataset = parse_cdf_bytes(data, source_name=name)
    base_calibration = CalibrationSettings.from_dataset(dataset)
    return dataset, base_calibration


def _to_float(text):
    text = (str(text) if text is not None else "").strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def seed_form_defaults(dataset, base_cal: CalibrationSettings) -> None:
    """Pre-fill every form widget from a freshly parsed dataset."""
    # Extract test date from first data timestamp if available
    date_str = ""
    if dataset.timestamps and dataset.timestamps[0]:
        date_str = dataset.timestamps[0].strftime("%d/%m/%Y %H:%M")

    defaults = {
        "md_project": dataset.header_get("Project Name", "Project", default=""),
        "md_client": dataset.header_get("Client Name", "Client", default=""),
        "md_location": dataset.header_get("Push Name", "Location", "Location ID", default=""),
        "md_date": date_str,
        "md_operator": dataset.header_get("Operator", default=""),
        "md_comments": "",
        "cal_tip_area": f"{base_cal.tip_area_mm2:g}",
        "cal_cone_id": dataset.header_get(
            "Cone", "Test Number", "Push Name", "Fix Number", default=""
        ),
        "cal_rod_diameter": f"{base_cal.rod_diameter_mm:g}",
        "cal_unit_weight": f"{base_cal.unit_weight_kn_m3:g}",
        "cal_nk": f"{base_cal.nk_factor:g}",
    }
    for key, value in defaults.items():
        st.session_state[key] = value


def read_calibration(base: CalibrationSettings) -> CalibrationSettings:
    """Read calibration widgets, falling back to dataset-derived baseline."""
    def f(key: str, fallback: float) -> float:
        val = _to_float(st.session_state.get(key))
        return val if val is not None else fallback

    return CalibrationSettings(
        tip_area_mm2=f("cal_tip_area", base.tip_area_mm2),
        rod_diameter_mm=f("cal_rod_diameter", base.rod_diameter_mm),
        unit_weight_kn_m3=f("cal_unit_weight", base.unit_weight_kn_m3),
        nk_factor=f("cal_nk", base.nk_factor),
    )


def _axis_limits(prefix: str):
    auto = bool(st.session_state.get(f"{prefix}_auto", True))
    lo = _to_float(st.session_state.get(f"{prefix}_min"))
    hi = _to_float(st.session_state.get(f"{prefix}_max"))
    return auto, lo, hi


def read_plot_scales():
    dx_auto, dx_min, dx_max = _axis_limits("dx")
    dy_auto, dy_min, dy_max = _axis_limits("dy")
    tx_auto, tx_min, tx_max = _axis_limits("tx")
    ty_auto, ty_min, ty_max = _axis_limits("ty")
    qx_auto, qx_min, qx_max = _axis_limits("qx")
    qy_auto, qy_min, qy_max = _axis_limits("qy")
    depth_scale = PlotAxisScale(
        x_auto=dx_auto, x_min=dx_min, x_max=dx_max,
        y_auto=dy_auto, y_min=dy_min, y_max=dy_max,
    )
    time_scale = PlotAxisScale(
        x_auto=tx_auto, x_min=tx_min, x_max=tx_max,
        y_auto=ty_auto, y_min=ty_min, y_max=ty_max,
    )
    qa_scale = PlotAxisScale(
        x_auto=qx_auto, x_min=qx_min, x_max=qx_max,
        y_auto=qy_auto, y_min=qy_min, y_max=qy_max,
    )
    return depth_scale, time_scale, qa_scale


def read_report_metadata(
    resistance_label: str,
    company_logo_bytes: Optional[bytes] = None,
    client_logo_bytes: Optional[bytes] = None,
) -> ReportMetadata:
    def g(key: str) -> str:
        return str(st.session_state.get(key, "") or "")

    filename = str(st.session_state.get("source_filename", "") or "")
    return ReportMetadata(
        project=g("md_project"),
        client=g("md_client"),
        location_id=g("md_location"),
        test_date=g("md_date"),
        operator=g("md_operator"),
        cone_id=g("cal_cone_id"),
        unit_weight_kn_m3=g("cal_unit_weight"),
        nk_factor=g("cal_nk"),
        comments=g("md_comments"),
        source_filename=os.path.basename(filename),
        resistance_label=resistance_label,
        company_logo_bytes=company_logo_bytes,
        client_logo_bytes=client_logo_bytes,
    )


# ---------------------------------------------------------------------------
# Export builders (in-memory streams)
# ---------------------------------------------------------------------------

def build_pdf_bytes(metadata, series, values, depth_scale, time_scale, qa_scale,
                     single_color=None, highlight_last_n_cycles=None,
                     highlight_color="#ff0000") -> bytes:
    buf = io.BytesIO()
    build_pdf(
        output_path=buf,
        metadata=metadata,
        depth_m=series.depth_m,
        resistance_series=values,
        elapsed_s=series.elapsed_s,
        depth_scale=depth_scale,
        time_scale=time_scale,
        qa_scale=qa_scale,
        cycles=series.cycles,
        single_color=single_color,
        highlight_last_n_cycles=highlight_last_n_cycles,
        highlight_color=highlight_color,
    )
    return buf.getvalue()


def build_excel_bytes(dataset, metadata, calibration, series) -> bytes:
    buf = io.BytesIO()
    build_excel(
        output_path=buf,
        dataset=dataset,
        metadata=metadata,
        calibration=calibration,
        series=series,
    )
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Plot rendering
# ---------------------------------------------------------------------------

def _style_axes(ax) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color("#8a96a1")
    ax.tick_params(colors="#3a444d", labelsize=8)
    ax.title.set_color("#1f2d3a")
    ax.title.set_fontweight("bold")
    ax.xaxis.label.set_color("#3a444d")
    ax.yaxis.label.set_color("#3a444d")
    ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.6)


def render_plot(container, x_values, y_values, cycles, xlabel, ylabel, title,
                scale: PlotAxisScale, invert_y: bool, legend_kwargs=None,
                single_color: Optional[str] = None,
                highlight_last_n_cycles: Optional[int] = None,
                highlight_color: str = "#ff0000") -> None:
    fig, ax = plt.subplots(figsize=(5.2, 4.3), dpi=130)
    _lkw = legend_kwargs if legend_kwargs is not None else {}
    plot_series_by_cycle(ax, x_values, y_values, cycles, linewidth=1.2,
                         show_legend=True, legend_kwargs=_lkw,
                         single_color=single_color,
                         highlight_last_n_cycles=highlight_last_n_cycles,
                         highlight_color=highlight_color)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    _style_axes(ax)
    if not scale.x_auto and scale.x_min is not None and scale.x_max is not None:
        ax.set_xlim(scale.x_min, scale.x_max)
    if not scale.y_auto and scale.y_min is not None and scale.y_max is not None:
        lo, hi = sorted((scale.y_min, scale.y_max))
        if invert_y:
            ax.set_ylim(hi, lo)
        else:
            ax.set_ylim(lo, hi)
    elif invert_y:
        ax.invert_yaxis()
    fig.tight_layout()
    container.pyplot(fig, use_container_width=True)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Page setup and main flow
# ---------------------------------------------------------------------------

st.set_page_config(page_title=APP_TITLE, page_icon="🧪", layout="wide")
access_gate()

st.markdown("""
<style>
/* House colour: rgb(102,255,51) = #66FF33 */
section[data-testid="stSidebar"] { background-color: #f0fff0; }
section[data-testid="stSidebar"] h3 { color: #2d7a00; }
div[data-testid="stMetric"] label { color: #2d7a00 !important; }
div[data-testid="stMetric"] div[data-testid="stMetricValue"] { color: #1a3300 !important; }
.stDownloadButton > button { background-color: #66FF33 !important; color: #1a3300 !important; border: none !important; }
.stDownloadButton > button:hover { background-color: #4db300 !important; }
h1 { color: #1a3300 !important; }
</style>
""", unsafe_allow_html=True)

st.title("🧪 " + APP_TITLE)
st.caption("Mini T-Bar Processing — Web Edition")

# ---- Sidebar: upload, plotted series, axis scales -------------------------
with st.sidebar:
    st.subheader("Data file")
    uploaded = st.file_uploader("Upload a .cdf export", type=["cdf"])

dataset = None
base_cal = None

if uploaded is not None:
    data = uploaded.getvalue()
    try:
        dataset, base_cal = parse_uploaded(data, uploaded.name)
    except Exception as exc:
        st.error(f"Could not parse this file:\n\n{exc}")
        st.stop()

    file_key = hashlib.sha256(data).hexdigest()
    if st.session_state.get("_file_key") != file_key:
        st.session_state["_file_key"] = file_key
        st.session_state["source_filename"] = uploaded.name
        seed_form_defaults(dataset, base_cal)
        st.rerun()

    if not dataset.timestamps:
        st.error("No sample rows found — is this a valid .cdf export?")
        st.stop()

with st.sidebar:
    st.divider()
    st.subheader("Logos (PDF footer)")
    st.caption(
        "Optional. Logos appear side-by-side in the PDF footer — "
        "company on the left, client on the right. PNG or JPG, any size."
    )
    company_logo_file = st.file_uploader(
        "Your company logo", type=["png", "jpg", "jpeg"], key="logo_company"
    )
    client_logo_file = st.file_uploader(
        "Client logo", type=["png", "jpg", "jpeg"], key="logo_client"
    )

with st.sidebar:
    st.divider()
    st.subheader("Plotted quantity")
    series_choice = st.radio(
        "Resistance series",
        [SERIES_SU, SERIES_QNT],
        horizontal=True,
        label_visibility="collapsed",
    )
    st.divider()
    st.subheader("Trace colour")
    color_mode = st.radio(
        "Trace colour mode",
        ["Cycle-colour-coded", "Single colour", "Highlight last N cycles"],
        key="trace_color_mode",
        label_visibility="collapsed",
        help="Cycle-colour-coded gives every cycle its own colour (can "
             "look busy on a high cycle count). Single colour draws the "
             "whole trace in one colour. Highlight last N cycles draws the "
             "whole trace in a base colour with only the final remolding "
             "cycles redrawn on top in a bright colour, so the peak "
             "(initial push) and remolded tail both stand out at a glance.",
    )
    trace_color = None
    highlight_last_n_cycles = None
    highlight_color = "#ff0000"
    if color_mode == "Single colour":
        trace_color = st.color_picker(
            "Trace colour", value="#1f4e79", key="single_color_value",
        )
    elif color_mode == "Highlight last N cycles":
        c1, c2 = st.columns(2)
        trace_color = c1.color_picker(
            "Base colour", value="#1f4e79", key="highlight_base_color",
        )
        highlight_color = c2.color_picker(
            "Highlight colour", value="#ff0000", key="highlight_tail_color",
        )
        highlight_last_n_cycles = st.number_input(
            "Number of final cycles to highlight",
            min_value=1, max_value=50, value=5, step=1,
            key="highlight_last_n_cycles",
        )
    st.divider()
    st.subheader("Axis scales")
    with st.expander("Depth plot"):
        st.caption("X: resistance  |  Y: depth (inverted)")
        st.checkbox("Auto X", value=True, key="dx_auto")
        c1, c2 = st.columns(2)
        c1.text_input("Min", key="dx_min")
        c2.text_input("Max", key="dx_max")
        st.checkbox("Auto Y", value=True, key="dy_auto")
        c1, c2 = st.columns(2)
        c1.text_input("Min", key="dy_min")
        c2.text_input("Max", key="dy_max")
    with st.expander("Time plot"):
        st.caption("X: time  |  Y: resistance")
        st.checkbox("Auto X", value=True, key="tx_auto")
        c1, c2 = st.columns(2)
        c1.text_input("Min", key="tx_min")
        c2.text_input("Max", key="tx_max")
        st.checkbox("Auto Y", value=True, key="ty_auto")
        c1, c2 = st.columns(2)
        c1.text_input("Min", key="ty_min")
        c2.text_input("Max", key="ty_max")
    with st.expander("Depth vs Time (PDF page 2 QA plot)"):
        st.caption("X: time  |  Y: depth (inverted)")
        st.checkbox("Auto X", value=True, key="qx_auto")
        c1, c2 = st.columns(2)
        c1.text_input("Min", key="qx_min")
        c2.text_input("Max", key="qx_max")
        st.checkbox("Auto Y", value=True, key="qy_auto")
        c1, c2 = st.columns(2)
        c1.text_input("Min", key="qy_min")
        c2.text_input("Max", key="qy_max")

# ---- Landing state --------------------------------------------------------
if dataset is None:
    st.info(
        "**Get started:** upload a `.cdf` file using the sidebar. The tool "
        "pre-fills every editable field from the file's header, draws both "
        "cycle-colour-coded plots, and lets you download the PDF lab report "
        "and the auditable Excel workbook."
    )
    st.markdown(
        "1. **Upload** a T-Bar `.cdf` export (pre-calibrated engineering units)\n"
        "2. Review/edit the **metadata** and **geometry** forms\n"
        "3. Toggle **qn,T-bar / Su** and adjust **axis scales** as needed\n"
        "4. **Download** the PDF report and/or Excel workbook"
    )
    st.stop()

# ---- Forms (pre-filled from the file, fully editable) ----------------------
st.success(
    f"Loaded **{uploaded.name}** — {len(dataset.timestamps):,} samples."
)

form_left, form_right = st.columns(2, gap="medium")
with form_left:
    st.markdown("**Report metadata** *(pre-filled from file, editable)*")
    st.text_input("Project", key="md_project")
    st.text_input("Client", key="md_client",
                  help="Pre-filled from file if present.")
    st.text_input("Location ID", key="md_location")
    st.text_input("Test Date", key="md_date")
    st.text_input("Operator", key="md_operator")
with form_right:
    st.markdown("**Geometry & analysis** *(pre-filled from file header, editable)*")
    r1a, r1b = st.columns(2)
    r1a.text_input("Tip Area (mm²)", key="cal_tip_area",
                   help="From file header 'Tip Area (mm)'")
    r1b.text_input("Cone ID", key="cal_cone_id",
                   help="From file header Fix/Test/Push Number")
    r2a, r2b = st.columns(2)
    r2a.text_input("Rod Diameter (mm)", key="cal_rod_diameter")
    r2b.text_input("Unit Weight (kN/m³)", key="cal_unit_weight")
    st.text_input("Nk Factor", key="cal_nk",
                   help="From file header 'N Value'. Must be greater than 0.")

st.text_area("Comments", key="md_comments", height=68)

# ---- Compute ----------------------------------------------------------------
calibration = read_calibration(base_cal)
if calibration.nk_factor <= 0:
    st.error(
        f"Nk Factor must be greater than 0 (got {calibration.nk_factor:g}). "
        "Please enter a valid value in the Geometry & analysis form."
    )
    st.stop()
try:
    series = compute_series(dataset, calibration)
except Exception as exc:
    st.error(f"Calculation failed: {exc}")
    st.stop()

values, res_label = (
    ((series.su_kpa, "Undrained Shear Strength, Su (kPa)") if series_choice == SERIES_SU
     else (series.qnt_mpa, "qn,T-bar (MPa)"))
)
depth_scale, time_scale, qa_scale = read_plot_scales()

# ---- Summary metrics + live previews ---------------------------------------
max_depth_m = max(series.depth_m) if series.depth_m else 0.0
n_cycles = sum(1 for seg in series.cycles if seg.kind == "cycle")
max_tilt = None
tilt_vals = [v for v in dataset.extra_columns.get("combined_tilt_deg", [])
             if v is not None]
if tilt_vals:
    max_tilt = max(tilt_vals)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Samples", f"{len(dataset.timestamps):,}")
m2.metric("Max penetration", f"{max_depth_m:.3f} m")
m3.metric("Remolding cycles", n_cycles)
if max_tilt is not None:
    m4.metric("Max combined tilt", f"{max_tilt:.1f}°")
else:
    m4.metric("Nk Factor", f"{calibration.nk_factor:g}")

plot_col_1, plot_col_2 = st.columns(2)
_title_label = re.sub(r'\s*\([^)]*\)', '', res_label).strip()
render_plot(plot_col_1, values, series.depth_m, series.cycles,
            res_label, "Depth (m)", f"{_title_label} vs Depth",
            depth_scale, invert_y=True, single_color=trace_color,
            highlight_last_n_cycles=highlight_last_n_cycles,
            highlight_color=highlight_color)
render_plot(plot_col_2, series.elapsed_s, values, series.cycles,
            "Time (s)", res_label, f"{_title_label} vs Time",
            time_scale, invert_y=False,
            legend_kwargs=dict(loc="upper right"), single_color=trace_color)

# ---- Exports ----------------------------------------------------------------
_company_logo = company_logo_file.getvalue() if company_logo_file is not None else _DEFAULT_COMPANY_LOGO
_client_logo = client_logo_file.getvalue() if client_logo_file is not None else _DEFAULT_CLIENT_LOGO
metadata = read_report_metadata(res_label, _company_logo, _client_logo)

pdf_bytes = b""
xlsx_bytes = b""
try:
    pdf_bytes = build_pdf_bytes(
        metadata, series, values, depth_scale, time_scale, qa_scale,
        single_color=trace_color,
        highlight_last_n_cycles=highlight_last_n_cycles,
        highlight_color=highlight_color,
    )
except Exception as exc:
    st.error(f"PDF export failed: {exc}")
try:
    xlsx_bytes = build_excel_bytes(dataset, metadata, calibration, series)
except Exception as exc:
    st.error(f"Excel export failed: {exc}")

safe_name = "".join(
    ch for ch in metadata.location_id.strip() if ch not in '\\/:*?"<>|'
).strip() or "tbar_report"

dl1, dl2 = st.columns(2)
dl1.download_button(
    "⬇️ Export PDF report",
    data=pdf_bytes,
    file_name=f"{safe_name}.pdf",
    mime="application/pdf",
    disabled=not pdf_bytes,
    use_container_width=True,
)
dl2.download_button(
    "⬇️ Export Excel workbook (.xlsx)",
    data=xlsx_bytes,
    file_name=f"{safe_name}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    disabled=not xlsx_bytes,
    use_container_width=True,
)

# ---- Provenance -------------------------------------------------------------
with st.expander("Parsed file header (provenance)"):
    for key, val in dataset.header.items():
        if key == "_notes":
            continue
        st.markdown(f"- **{key}:** {val}" if val else f"- **{key}:** *(empty)*")

st.caption(
    "Web edition of the Mini T-Bar Processing Tool · "
    "uploads are processed in memory and never stored."
)

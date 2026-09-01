"""
rdf_parser.py

Parser for .cdf (calculated data) files produced by the T-bar/CPT acquisition
software (version 2.51).

The .cdf file is used as the primary input. It contains pre-calibrated
engineering-unit data — penetration in metres, tip resistance in MPa, sleeve
friction, pore pressure, and tilt in degrees — computed from the raw ADC
readings by the acquisition software. This avoids the need for raw sensor
calibration coefficients in this tool.

File structure
--------------
Quoted-CSV header rows at the top, followed by a data block:

  Header rows (pairs: label-line then value-line):
    "Software Version" / "2.51"
    "Project Name","Client Name","Location",...  / values
    "Fix Number","Push Name","Test Number",...   / values
    "Sample Start Depth (cm)","Cycle Top Position (cm)",... / values
    "Tip Area (mm)","N Value"  / values
    "Tip Area Factor", <value>

  Data header line:
    "Date&Time","Pen (m)","Tip (MPa Qc)","Cu (MPa)","Sleeve (MPa)",
    "Measured Pore (MPa)","TiltX (Degrees)","TiltY (Degrees)",
    "Combined Tilt (Degrees)"

  Data rows:
    #DD/MM/YYYY HH:MM:SS#,<pen_m>,<tip_mpa>,...

The .rdf (raw bits) file is not parsed here; the .cdf contains all the data
needed for downstream analysis.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from typing import Optional

from tbr_parser import TbarDataset

_DATE_RE = re.compile(r'^#(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})#$')


def _clean(s: str) -> str:
    return s.strip().strip('"').strip()


def _try_float(s: str) -> Optional[float]:
    try:
        return float(_clean(s))
    except (ValueError, TypeError):
        return None


def _parse_timestamp(s: str) -> Optional[datetime]:
    m = _DATE_RE.match(s.strip())
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d/%m/%Y %H:%M:%S")
    except ValueError:
        return None


def _csv_row(line: str) -> list:
    """Parse one CSV line leniently; falls back to comma-split on error
    (handles malformed/unclosed quotes that appear in some header rows)."""
    try:
        return list(next(csv.reader([line])))
    except Exception:
        return line.split(',')


def _parse_cdf_lines(lines: list, dataset: TbarDataset) -> None:
    """Parse a list of stripped text lines from a .cdf file into dataset."""
    dataset.is_precalculated = True

    # Locate the data header line — contains "Date&Time" and "Pen"
    data_header_idx = None
    for idx, line in enumerate(lines):
        s = line.strip()
        if 'Date&Time' in s and 'Pen' in s:
            data_header_idx = idx
            break

    # ---------- Parse metadata header ----------
    i = 0
    limit = data_header_idx if data_header_idx is not None else len(lines)

    while i < limit:
        raw = lines[i].strip()
        if not raw:
            i += 1
            continue

        row = [_clean(c) for c in _csv_row(raw)]
        first = row[0] if row else ""

        # "Software Version" → next non-blank line is the version value
        if first == "Software Version":
            i += 1
            while i < limit and not lines[i].strip():
                i += 1
            if i < limit:
                v = _csv_row(lines[i].strip())
                dataset.header["Software Version"] = _clean(v[0]) if v else ""
            i += 1
            continue

        # Multi-column key rows: keys on this line, values on next non-blank line
        MULTI_KEY_STARTS = {
            "Project Name",
            "Fix Number",
            "Sample Start Depth (cm)",
            "Tip Area (mm)",
        }
        if first in MULTI_KEY_STARTS:
            keys = row
            i += 1
            while i < limit and not lines[i].strip():
                i += 1
            if i < limit:
                vals = [_clean(c) for c in _csv_row(lines[i].strip())]
                for k, v in zip(keys, vals):
                    if k:
                        dataset.header[k] = v
            i += 1
            continue

        # "Tip Area Factor", <value>  — key and value on the same line
        if first == "Tip Area Factor":
            full = _csv_row(raw)
            dataset.header["Tip Area Factor"] = _clean(full[1]) if len(full) > 1 else ""
            i += 1
            continue

        i += 1

    # ---------- Parse data rows ----------
    if data_header_idx is None:
        return

    for line in lines[data_header_idx + 1:]:
        stripped = line.strip()
        if not stripped:
            continue
        # Skip trailing quoted rows (e.g. "Post Baseline" section in .rdf)
        if stripped.startswith('"'):
            continue

        parts = stripped.split(',')
        if len(parts) < 3:
            continue

        ts = _parse_timestamp(parts[0])
        if ts is None:
            continue

        pen_m = _try_float(parts[1])
        tip_mpa = _try_float(parts[2])
        if pen_m is None or tip_mpa is None:
            continue

        dataset.timestamps.append(ts)
        dataset.position_raw.append(pen_m)
        dataset.load_raw.append(tip_mpa)

        cols = dataset.extra_columns
        if len(parts) > 3:
            cols.setdefault("cu_mpa", []).append(_try_float(parts[3]))
        if len(parts) > 4:
            cols.setdefault("sleeve_mpa", []).append(_try_float(parts[4]))
        if len(parts) > 5:
            cols.setdefault("pore_mpa", []).append(_try_float(parts[5]))
        if len(parts) > 6:
            cols.setdefault("tiltx_deg", []).append(_try_float(parts[6]))
        if len(parts) > 7:
            cols.setdefault("tilty_deg", []).append(_try_float(parts[7]))
        if len(parts) > 8:
            cols.setdefault("combined_tilt_deg", []).append(_try_float(parts[8]))


def parse_cdf_file(path: str) -> TbarDataset:
    """Parse a .cdf file from a filesystem path."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = [line.rstrip("\r\n") for line in f.readlines()]
    dataset = TbarDataset(source_path=path)
    _parse_cdf_lines(lines, dataset)
    return dataset


def parse_cdf_bytes(data: bytes, source_name: str = "") -> TbarDataset:
    """Parse a .cdf file from raw bytes (used by the web app for uploads).

    Decoding mirrors :func:`parse_cdf_file` exactly so both entry points
    produce identical datasets for identical content."""
    text = data.decode("utf-8", errors="replace")
    lines = [line.rstrip("\r\n") for line in io.StringIO(text).readlines()]
    dataset = TbarDataset(source_path=source_name)
    _parse_cdf_lines(lines, dataset)
    return dataset

"""
tbr_parser.py

Robust parser for GEOTAC Sigma-1 / TBar-SI ".tbr" raw data files.

The file format (observed from real Sigma-1 TBar-SI exports) looks like:

    [HEADER]
    Created by ...
    Project:\t<value>\tLoad Frame Name:\t<value>
    Date:\t<value>\tTime:\t<value>
    Box Core Number:\t<value>
    Box Core Length (mm):\t<value>\tBox Core Width (mm):\t<value>\t...
    Ball Diameter (mm):\t<value>\tRod Diameter (mm):\t<value>
    TBar Diameter (mm):\t<value>\tTBar Length (mm):\t<value>
    Comments:\t<value>

    [SENSORS]
    Name\t<Sensor A name>\t<Sensor B name>
    ID\t<id>\t<id>
    Module\t<module>\t<module>
    Channel\t<n>\t<n>
    Unit\t<unit>\t<unit>
    Cal. Factor\t<value>\t<value>
    Excitation\t<value>\t<value>
    Zero\t<value>\t<value>
    Min. Reading\t<value>\t<value>
    Max. Reading\t<value>\t<value>

    [Profile 1]\t[Step 1]\tTBar
    Time          \t<Sensor A name>\t<Sensor B name>
    <timestamp>\t<raw A>\t<raw B>
    ...

    [Profile 1]\t[Step 2]\tTBar
    ...

    [PROFILE]

There can be an arbitrary number of "[Profile N]\t[Step M]" data blocks; they
are all part of the same continuous push and are concatenated in file order.

This parser is intentionally tolerant of:
    - Variable amounts of whitespace around tab-separated values.
    - Missing/extra header fields (any header key that isn't recognised is
      still captured in ``header`` so nothing is silently lost).
    - Sensor columns identified by their ``Unit`` row rather than a hardcoded
      name, so a load-cell channel named something other than
      "External Load Cell" (or a different column order) is still detected
      correctly as long as its unit is force-like ("N", "kN", "lbf", ...) and
      the other channel's unit is length-like ("mm", "m", "cm", ...).
    - "N/A" values for Excitation/Zero on channels where they are not
      applicable (defaults to Excitation=1, Zero=0 so the shared formula
      ``(raw - zero) * cal_factor / excitation`` degenerates cleanly to
      ``raw * cal_factor``).
    - Any number of Profile/Step data blocks.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


FORCE_UNITS = {"n", "kn", "lbf", "lb", "lbs"}
LENGTH_UNITS = {"mm", "m", "cm", "in", "inch", "inches"}

_DATETIME_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %I:%M:%S %p",
)


def _clean(value: str) -> str:
    """Strip whitespace and stray quote characters from a raw field."""
    return value.strip().strip('"').strip()


def _try_float(value: str) -> Optional[float]:
    value = _clean(value)
    if value == "" or value.upper() in ("N/A", "NA", "NONE"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_datetime(value: str) -> Optional[datetime]:
    value = _clean(value)
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


@dataclass
class SensorInfo:
    """Calibration/metadata for a single sensor channel."""

    name: str = ""
    sensor_id: str = ""
    module: str = ""
    channel: str = ""
    unit: str = ""
    cal_factor: Optional[float] = None
    excitation: Optional[float] = None
    zero: Optional[float] = None
    min_reading: Optional[float] = None
    max_reading: Optional[float] = None

    @property
    def effective_excitation(self) -> float:
        return self.excitation if self.excitation not in (None, 0) else 1.0

    @property
    def effective_zero(self) -> float:
        return self.zero if self.zero is not None else 0.0


@dataclass
class TbarDataset:
    """Fully parsed representation of a data file (.tbr or .cdf)."""

    source_path: str = ""
    header: dict = field(default_factory=dict)
    sensors: dict = field(default_factory=dict)  # name -> SensorInfo (.tbr only)
    load_sensor: Optional[SensorInfo] = None
    position_sensor: Optional[SensorInfo] = None

    timestamps: list = field(default_factory=list)   # datetime objects
    load_raw: list = field(default_factory=list)      # raw counts (.tbr) or Tip MPa (.cdf)
    position_raw: list = field(default_factory=list)  # raw counts (.tbr) or Pen m (.cdf)
    step_boundaries: list = field(default_factory=list)  # (label, start_idx) — .tbr only
    extra_columns: dict = field(default_factory=dict)    # additional .cdf columns
    is_precalculated: bool = False  # True for .cdf files (engineering units already applied)

    # --- convenience accessors for common header fields -----------------
    def header_get(self, *keys, default: str = "") -> str:
        for key in keys:
            for hk, hv in self.header.items():
                if hk.strip().lower() == key.strip().lower():
                    return hv
        return default

    def header_get_float(self, *keys, default: Optional[float] = None) -> Optional[float]:
        raw = self.header_get(*keys, default="")
        val = _try_float(raw)
        return val if val is not None else default


def _split_tabs(line: str) -> list:
    return line.rstrip("\n").rstrip("\r").split("\t")


def _parse_header_block(lines: list, dataset: TbarDataset) -> None:
    for line in lines:
        if not line.strip():
            continue
        parts = _split_tabs(line)
        # Header lines are made of "Key:\tValue" pairs, possibly repeated
        # several times on one line (e.g. "Project:\tFoo\tLoad Frame
        # Name:\tBar"). Some lines (e.g. the software banner) start with a
        # free-text cell that has no trailing colon and is not a key --
        # anything before the first colon-terminated cell is treated as a
        # note rather than mis-paired with the next cell as its "value".
        i = 0
        while i < len(parts) and not _clean(parts[i]).endswith(":"):
            note = _clean(parts[i])
            if note:
                dataset.header.setdefault("_notes", "")
                dataset.header["_notes"] += (note + "\n")
            i += 1
        while i < len(parts):
            key = _clean(parts[i])
            if key.endswith(":"):
                key = key[:-1].strip()
            value = _clean(parts[i + 1]) if i + 1 < len(parts) else ""
            if key:
                dataset.header[key] = value
            i += 2


def _parse_sensors_block(lines: list, dataset: TbarDataset) -> None:
    rows = {}
    for line in lines:
        if not line.strip():
            continue
        parts = [_clean(p) for p in _split_tabs(line)]
        if not parts or not parts[0]:
            continue
        row_key = parts[0]
        if row_key.endswith(":"):
            row_key = row_key[:-1]
        rows[row_key] = parts[1:]

    if "Name" not in rows:
        return
    names = rows["Name"]
    n_cols = len(names)

    def col(row_key, idx):
        vals = rows.get(row_key, [])
        if idx >= len(vals):
            return None
        return vals[idx]

    for idx in range(n_cols):
        name = names[idx]
        if not name:
            continue
        info = SensorInfo(
            name=name,
            sensor_id=col("ID", idx) or "",
            module=col("Module", idx) or "",
            channel=col("Channel", idx) or "",
            unit=(col("Unit", idx) or "").strip(),
            cal_factor=_try_float(col("Cal. Factor", idx) or ""),
            excitation=_try_float(col("Excitation", idx) or ""),
            zero=_try_float(col("Zero", idx) or ""),
            min_reading=_try_float(col("Min. Reading", idx) or ""),
            max_reading=_try_float(col("Max. Reading", idx) or ""),
        )
        dataset.sensors[name] = info

    # Identify load-cell (force units) vs. position/encoder (length units)
    # sensors by their unit, independent of the exact channel name used by
    # the acquisition software.
    for info in dataset.sensors.values():
        unit_lower = info.unit.lower()
        if unit_lower in FORCE_UNITS and dataset.load_sensor is None:
            dataset.load_sensor = info
        elif unit_lower in LENGTH_UNITS and dataset.position_sensor is None:
            dataset.position_sensor = info

    # Fallback: if unit-based detection failed (unexpected/missing units),
    # assume the first column is the load cell and the second is position,
    # matching the layout observed in every known Sigma-1 export.
    remaining = list(dataset.sensors.values())
    if dataset.load_sensor is None and remaining:
        dataset.load_sensor = remaining[0]
    if dataset.position_sensor is None and len(remaining) > 1:
        dataset.position_sensor = remaining[1]


def _parse_data_block(lines: list, dataset: TbarDataset, label: str) -> None:
    if not lines:
        return
    header_line = lines[0]
    header_cols = [_clean(c) for c in _split_tabs(header_line)]

    load_col_idx = None
    pos_col_idx = None
    for idx, col_name in enumerate(header_cols):
        if dataset.load_sensor and col_name == dataset.load_sensor.name:
            load_col_idx = idx
        elif dataset.position_sensor and col_name == dataset.position_sensor.name:
            pos_col_idx = idx

    # Fallback to fixed layout (Time, Load, Position) if names didn't match
    # exactly -- a rare safety net rather than the primary path.
    if load_col_idx is None and len(header_cols) > 1:
        load_col_idx = 1
    if pos_col_idx is None and len(header_cols) > 2:
        pos_col_idx = 2

    start_idx = len(dataset.timestamps)
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = _split_tabs(line)
        if len(parts) <= max(load_col_idx or 0, pos_col_idx or 0):
            continue
        ts = _parse_datetime(parts[0])
        load_val = _try_float(parts[load_col_idx]) if load_col_idx is not None else None
        pos_val = _try_float(parts[pos_col_idx]) if pos_col_idx is not None else None
        if load_val is None or pos_val is None:
            continue
        dataset.timestamps.append(ts)
        dataset.load_raw.append(load_val)
        dataset.position_raw.append(pos_val)

    if len(dataset.timestamps) > start_idx:
        dataset.step_boundaries.append((label, start_idx))


def _parse_tbr_sections(raw_lines, dataset: TbarDataset) -> None:
    """Section-split ``raw_lines`` and dispatch each named "[...]" block to
    its dedicated parser. Shared core behind both entry points below, so
    path-based and bytes-based parsing always behave identically."""
    # Split the input into named sections. A section starts at any line whose
    # first tab-delimited cell looks like "[...]" (e.g. "[HEADER]" or
    # "[Profile 1]\t[Step 1]\tTBar").
    sections = []  # list of (label, [lines...])
    current_label = None
    current_lines = []

    def flush():
        if current_label is not None:
            sections.append((current_label, current_lines[:]))

    for raw_line in raw_lines:
        stripped = raw_line.rstrip("\r\n")
        first_cell = stripped.split("\t", 1)[0].strip()
        if first_cell.startswith("[") and first_cell.endswith("]"):
            flush()
            current_label = stripped.strip()
            current_lines = []
        else:
            current_lines.append(stripped)
    flush()

    for label, lines in sections:
        upper = label.upper()
        if upper.startswith("[HEADER]"):
            _parse_header_block(lines, dataset)
        elif upper.startswith("[SENSORS]"):
            _parse_sensors_block(lines, dataset)
        elif upper.startswith("[PROFILE]"):
            # Trailing summary/footer section -- no per-sample data in any
            # observed export; ignored unless it happens to hold a data
            # table (handled by the generic fallback below).
            if lines and lines[0].lower().startswith("time"):
                _parse_data_block(lines, dataset, label)
        elif upper.startswith("[PROFILE"):
            # "[Profile N]\t[Step M]\t..." data block.
            _parse_data_block(lines, dataset, label)
        else:
            # Unknown section: if it looks like a data block (starts with a
            # "Time" style header row) parse it anyway so unexpected section
            # names don't silently drop data.
            if lines and lines[0].lower().startswith("time"):
                _parse_data_block(lines, dataset, label)


def parse_tbr_lines(raw_lines, source_name: str = "") -> TbarDataset:
    """Parse .tbr content supplied as an iterable of text lines."""
    dataset = TbarDataset(source_path=source_name)
    _parse_tbr_sections(raw_lines, dataset)
    return dataset


def parse_tbr_file(path: str) -> TbarDataset:
    """Parse a GEOTAC Sigma-1 / TBar-SI .tbr file from a filesystem path."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw_lines = f.readlines()
    return parse_tbr_lines(raw_lines, source_name=path)


def parse_tbr_bytes(data: bytes, source_name: str = "") -> TbarDataset:
    """Parse a GEOTAC Sigma-1 / TBar-SI .tbr file from raw bytes.

    Used by the web app (web_app.py), where files arrive as in-memory
    uploads rather than paths. ``source_name`` is recorded in the dataset's
    ``source_path`` (typically the upload's original filename). Decoding
    mirrors :func:`parse_tbr_file` exactly, so both entry points produce
    identical datasets for identical content."""
    text = data.decode("utf-8", errors="replace")
    raw_lines = io.StringIO(text).readlines()
    return parse_tbr_lines(raw_lines, source_name=source_name)

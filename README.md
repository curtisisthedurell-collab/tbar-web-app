# Mini T-Bar Processing Tool — Web Edition

A browser-based **Mini T-Bar Processing Tool** for T-bar/CPT penetrometer
data:

1. Upload a `.cdf` export from the T-bar/CPT acquisition software (v2.51).
   The `.cdf` contains pre-calibrated engineering-unit data (Pen in metres,
   Tip in MPa, etc.) computed by the acquisition software itself, so no raw
   sensor calibration is required here.
2. Report metadata and calibration/geometry forms pre-fill from the file's
   header section and stay fully editable.
3. Two live previews — (qnT-bar or Su) vs Depth and vs Time — colour-coded
   Initial / Cycle N / Final, with per-axis auto/min/max scale controls.
4. Download the landscape **PDF lab report** (plus a Penetration QA page)
   and the auditable live-formula **Excel workbook**.

**Privacy model:** uploads are parsed entirely in memory; nothing is written
to the server disk and nothing persists between sessions. Access is gated by
a shared passcode (see below).

## Files

| File | Purpose |
| --- | --- |
| `web_app.py` | The Streamlit application (passcode gate, upload, forms, plots, downloads). |
| `rdf_parser.py` | Parses `.cdf` files — the input format this app uses — from a path (`parse_cdf_file`) or in-memory bytes (`parse_cdf_bytes`, used by the web upload). |
| `tbr_parser.py` | Shared `TbarDataset`/`SensorInfo` types used by both parsers, plus a legacy raw `.tbr` GEOTAC Sigma-1 / TBar-SI parser (`parse_tbr_file` / `parse_tbr_bytes`) kept for older raw-sensor exports — **not** used by the current `.cdf` upload flow in `web_app.py`. |
| `tbr_calc.py` | Engineering-unit processing: depth zeroing, overburden correction, qn,T-bar, Su, and cycle detection. |
| `tbar_plotting.py`, `tbar_pdf.py`, `tbar_excel.py` | Shared cycle-colour plotting helpers and the PDF/Excel report builders. |
| `requirements.txt` | Web-only dependencies (Streamlit, matplotlib, numpy, reportlab, openpyxl). |
| `test_web_smoke.py` | Web-specific smoke tests (byte-parse parity + stream-based exports) against real `.cdf` sample files. |
| `test_smoke.py` | Full engine regression suite (parser → calc → PDF/Excel export) against a real `.cdf` sample file. |
| `samples/` | Real `.cdf` sample files used by both test suites. |
| `.streamlit/config.toml` | Theme (accent matches the report's dark blue) + upload cap. |

## Run locally

```powershell
python -m venv C:\tbrwebvenv
C:\tbrwebvenv\Scripts\python.exe -m pip install -r requirements.txt
C:\tbrwebvenv\Scripts\python.exe -m streamlit run web_app.py
```

Run the last command from this project's folder. The app opens at
`http://localhost:8501`.

## Passcode gate

The first screen asks for a passcode when one is configured. It is read from:

1. `st.secrets["TBAR_PASSCODE"]` — production (see below), or locally via
   `.streamlit/secrets.toml`:
   ```toml
   TBAR_PASSCODE = "choose-a-shared-secret"
   ```
2. Fallback: the `TBAR_PASSCODE` environment variable.

**If neither is set the app runs OPEN with a visible warning banner** — that
keeps local development frictionless, but always set the secret before
sharing a deployed URL.

This is a shared-passcode gate (everyone uses the same secret), not
per-user accounts — appropriate for sharing lab data with a known group,
not for regulated/confidential material.

## Deploying to Streamlit Community Cloud (free)

Streamlit Community Cloud gives you a permanent public URL
(`https://<you>-tbar-web-app.streamlit.app`) straight from a GitHub repo.

### 1. Put the folder on GitHub (no git installation needed)

1. Go to <https://github.com/new> and create a new **public** repository,
   e.g. `tbar-web-app` (do **not** initialise with a README).
2. On the empty repo page click **"uploading an existing file"**.
3. Drag in the **contents** of this folder (`web_app.py`, all `tbr_*.py`
   and `rdf_parser.py`, both test files, `requirements.txt`, `.gitignore`,
   `.streamlit/`, `samples/`) — most browsers accept whole-folder drag &
   drop. Do not add `.streamlit/secrets.toml` even if you created one
   locally; it is gitignored and must stay private.
4. Click **Commit changes**.

### 2. Create the app

1. Go to <https://share.streamlit.io> and **Sign up / Sign in with GitHub**.
2. **Create app** → *Deploy a public app from GitHub* → pick your
   `tbar-web-app` repo, branch `main`, **Main file path: `web_app.py`**.
3. (Optional) Advanced settings → choose Python 3.12+ → Save.
4. Click **Deploy!** First boot takes a few minutes while dependencies
   install.

### 3. Set the passcode

In your app on share.streamlit.io: **⋮ menu → Settings → Secrets**, add:

```toml
TBAR_PASSCODE = "choose-a-shared-secret"
```

Save — the app restarts and now shows the passcode screen. Share the URL
and the passcode only with people who should have access. Changing the
secret revokes everyone immediately.

## Updating later

Edit any file here, then re-upload it to GitHub (the web UI lets you edit a
file's contents directly, or delete + re-drag). Streamlit Cloud redeploys
automatically on every commit.

## Tests

```powershell
C:\tbrwebvenv\Scripts\python.exe test_web_smoke.py   # web plumbing
C:\tbrwebvenv\Scripts\python.exe test_smoke.py       # full engine suite
```

Both print `... PASSED` on success. The engine suite hand-verifies depth /
overburden / qnT-bar / Su against known values (including that an Nk Factor
of 0 is rejected rather than silently substituted), and validates the PDF
and Excel outputs structurally (including the live-formula corruption
regression check).

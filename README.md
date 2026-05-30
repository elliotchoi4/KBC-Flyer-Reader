# KBC Flyer Reader

Drop a real-estate flyer in. Get a populated KBC survey template out.

The app reads industrial / commercial real-estate flyers (PDFs or images),
runs a local LLM to extract the structured fields, and writes one row per
building (or land parcel) into a fresh copy of the locked KBC template —
preserving all dropdowns, conditional formatting, and merged headers.

Everything runs locally. No flyer data ever leaves the machine.

---

## Features

- **Drag-and-drop GUI** with a Building/Land toggle.
- **Auto-detects** images vs. digital PDFs vs. scanned PDFs; routes each
  through the appropriate pipeline (OCR vs. direct text extraction).
- **Two extraction providers** — choose in Settings (Claude is the default):
  - **Claude API** (default) — sends the flyer text to Anthropic's Claude.
    Far more accurate and reliable; needs an API key and an internet
    connection. Paste your key into the **Claude API key** field in
    Settings (a show/hide box; click the **?** beside it for how to get a
    key). On Windows the key is stored in the **Windows Credential Manager**
    (encrypted, per-user) and is never written to the app's config file.
    On other platforms — or if the Credential Manager is unavailable — it
    falls back to the config file. As a last resort the app still reads a
    plain-text `claude-key` file from the folder under your Documents.
  - **Local (Ollama)** — runs a model on your machine. No API key, no
    data leaves the computer; free, but slower and less accurate on a
    low-end laptop.
- **Pydantic-validated output** — closed-set fields (e.g. *Sale/Lease*,
  *Building Status*) can only be one of the template's allowed values.
- **Multi-building aware** — a park-overview flyer with three buildings
  produces three rows, never a fourth "park" row.
- **Blank when unknown** — fields the flyer doesn't mention are left blank.
  No hallucinated "TBD"s.
- **Confidence highlighting** — cells the model wasn't sure about are
  highlighted yellow so a reviewer can scan them quickly.
- **Batch processing** — drop in multiple flyers, they're processed in
  parallel and aggregated into a single output workbook.

---

## Quick start

### Windows

1. Install [Python 3.10+](https://www.python.org/downloads/) — be sure to
   tick **"Add Python to PATH"** during install.
2. Double-click **`install.bat`**. It will:
    - create a Python virtual environment,
    - install all dependencies,
    - point you to Tesseract OCR if it's not installed,
    - point you to Ollama if it's not installed,
    - pull the default model (`qwen2.5:3b`),
    - remove any leftover launcher from an older version,
    - create a **Flyer Reader Output** folder (in the app folder) for your
      exported spreadsheets,
    - create the **KBC Flyer Reader** shortcut (book icon, no console).
3. Launch from the **KBC Flyer Reader** shortcut (the installer puts one in this folder and in the Start Menu). It has the book icon and opens with no console window.

> **About the icon:** only a Windows shortcut (`.lnk`) or a built `.exe` can show a custom icon — a `.bat` or `.vbs` file always shows a generic system icon. That's why the book icon lives on the **KBC Flyer Reader** shortcut, not on a script file.

> If you installed an earlier copy and still have a stale `KBC Flyer Reader App.vbs` in the folder, delete it (it can open in Notepad instead of running on some machines). Re-running `install.bat` removes it for you. If you don't have the shortcut yet, double-click **`Create Shortcut.bat`** once to generate it. **`KBC Flyer Reader (backup).bat`** also launches the app as a fallback, but shows a generic icon and briefly flashes a console window.

### macOS / Linux

```bash
./install.sh
./run.sh
```

### Optional: standalone executable

After installing, run **`build_exe.bat`** (Windows) to produce a
`dist/"KBC Flyer Reader"/` folder you can zip and send to coworkers. The
launcher inside is **`KBC Flyer Reader.exe`** (book icon, no console). They
still need Ollama and Tesseract installed separately on their machines, but
they won't need Python.

---

## Requirements

- **Python 3.10+** (Windows / macOS / Linux).
- **Ollama** with a model pulled. Default is `qwen2.5:3b`. Heavier models
  (`llama3.1:70b`, `mixtral`) yield better extraction on messy flyers if
  your hardware can run them.
- **Tesseract OCR** — only needed for image flyers and scanned PDFs.
- About **2 GB of disk** for the default model (`qwen2.5:3b`).

---

## How it works

```
flyer (PDF or image)
       │
       ▼
┌──────────────────────────────┐
│ Stage 1 — detect             │  python-magic / suffix sniff
└──────────────────────────────┘
       │           │
       │           └─ scanned PDF / image → OCR (pytesseract)
       ▼
┌──────────────────────────────┐
│ Stage 2 — extract text       │  pdfplumber (digital PDFs)
└──────────────────────────────┘
       │
       ▼
┌──────────────────────────────┐
│ Stage 3 — local LLM extract  │  Ollama + instructor
│   instructed to:             │  - Pydantic schema enforcement
│   - return one record per    │  - closed-set fields only accept
│     building (never the park │    allowed values
│     itself)                  │  - null for missing fields
│   - mark low-confidence      │
│     fields                   │
└──────────────────────────────┘
       │
       ▼
┌──────────────────────────────┐
│ Stage 4 — normalize          │  dateutil, phonenumbers, state map
└──────────────────────────────┘
       │
       ▼
┌──────────────────────────────┐
│ Stage 5 — write Excel        │  openpyxl into a copy of the locked
│   - rows 1–5 untouched       │  KBC template; all dropdowns and
│   - data starts at row 6     │  merged headers preserved
│   - blank when unknown       │
│   - yellow-fill if low conf  │
└──────────────────────────────┘
       │
       ▼
KBC_Building_Survey_YYYYMMDD_HHMMSS.xlsx
```

---

## Output behavior

- The locked templates in `templates/` are **never modified**. Each run
  copies the appropriate template to a fresh timestamped file.
- Data rows start at row 6, leaving the column headers (row 3), example
  (row 4), and input-options hint (row 5) intact.
- The `#` column is auto-numbered per output workbook.
- For Land Survey, the *Estimated Sale Price per Acre* column is written
  as a live formula (`=Q/K`) instead of a static value, so it stays in
  sync if you edit acreage or price later.
- For Building Survey, the `office_space_sf` field accepts either a number
  or the literal string **`BTS`** (built-to-suit), matching the template hint.
- The **View Exported Files** button (top of the window) opens your output
  folder in the file explorer so you can grab finished workbooks quickly.
  It opens the *Default output folder* set in Settings.
- A **Flyer Reader Output** folder ships with the app and is the default
  output location on first run. To use a different folder, open Settings →
  *Default output folder* → **Browse**.
- The template's **"Example" row and the input-options hint row are removed**
  from every new deliverable, so the output starts clean with your data at
  the first data row. (Cleared, not deleted, so the locked dropdowns stay
  intact.)

## Getting Started guide

On first launch the app shows a **Getting Started** window: a short "how it
works" walkthrough plus a side-by-side of the two engines (Claude vs.
Ollama) covering speed, accuracy, cost, where each runs, and which to use
for confidential data. Tick **Do not show this again** and click **Good to
go!** to stop it appearing on future startups. (You can always revisit the
same material from the **?** button.)

## Field Hints (broker shorthand presets)

Brokers write things in their own shorthand — e.g. a site-plan callout like
`DH 9'x10' (60)` may mean *60 dock-high doors, 9'×10'*. **Field Hints** let
you teach the app these mappings once and reuse them:

1. Click **Manage…** next to *Field hints* (top of the window).
2. Add a preset (typically one per broker/brokerage) and give it a name.
3. Add `shorthand → meaning` rows (e.g. `DH` → `dock-high doors, 9'×10'`).
   Each row also has a **Column** menu listing the survey template's columns,
   so you can tie a shorthand directly to where it should land (e.g. map
   `DH` to the "# of Existing Dock Doors" column). You can use the meaning
   text, the column, or both.
4. Save. Pick that preset from the *Field hints* dropdown before extracting.

The selected preset is compiled into a shorthand key and prepended to the
Additional Instructions on every run, so the model interprets that broker's
notation consistently. Presets are stored in `field_hints.json` in the app's
config folder; value compounds as you build more of them.

---

## CLI mode

```bash
# GUI
python -m src.main

# Batch from the command line
python -m src.main --building path/to/flyer1.pdf path/to/flyer2.png
python -m src.main --land path/to/land_flyer.pdf -o /custom/output/dir
```

---

## Configuration

Settings live in `%APPDATA%\FlyerReader\config.json` (Windows) or
`~/.config/FlyerReader/config.json` (Linux) /
`~/Library/Application Support/FlyerReader/config.json` (macOS). Edit via
the **Settings** button in the GUI:

| Setting | Default | Notes |
|---|---|---|
| Ollama base URL | `http://localhost:11434/v1` | Point elsewhere for a shared Ollama server. |
| Model | `qwen2.5:3b` | Larger = better extraction but slower. |
| OCR engine | `pytesseract` | Switch to `easyocr` if you can't install Tesseract. |
| Parallel extractions | `2` | Raise for multi-GPU servers; lower for laptops. |
| Low-confidence threshold | `0.8` | Cells with confidence below this get highlighted yellow. |
| Default output folder | `Flyer Reader Output/` (in the app folder) | Created by the installer; change in Settings. |

---

## Project layout

```
flyer_reader/
├── README.md              you are here
├── requirements.txt
├── pyproject.toml
├── install.bat / install.sh      one-shot installers
├── Create Shortcut.bat           makes the book-icon shortcut (run once)
├── KBC Flyer Reader.lnk          the launcher (book icon, no console) — created by setup
├── KBC Flyer Reader (backup).bat plain fallback launcher (no icon, console flash)
├── run.sh                        macOS / Linux launcher
├── build_exe.bat                 PyInstaller build (Windows)
├── flyer_reader.spec             PyInstaller config
├── templates/                 master KBC templates (READ-ONLY at runtime)
└── src/
    ├── main.py                CLI / GUI entry
    ├── gui.py                 drag-and-drop interface
    ├── pipeline.py            stage orchestrator + batch runner
    ├── detector.py            file-type sniffer (Stage 1)
    ├── extractor.py           OCR + PDF text (Stage 2)
    ├── llm_client.py          Ollama via instructor (Stage 3)
    ├── normalizers.py         dates / states / numbers (Stage 4)
    ├── excel_writer.py        template-preserving writer (Stage 5)
    ├── schemas.py             Pydantic models — the data contract
    ├── prompts.py             LLM prompt templates
    └── config.py              app config + paths
```

---

## Troubleshooting

**"Could not reach Ollama"**
&nbsp;&nbsp; Make sure the Ollama desktop app is running. On Windows it
runs as a tray icon. Confirm by visiting `http://localhost:11434` in a
browser — you should see "Ollama is running".

**Installer says Tesseract is not installed even though I installed it**
&nbsp;&nbsp; The UB Mannheim installer does not add Tesseract to PATH by
default. Re-run `install.bat` — it will look in `C:\Program Files\Tesseract-OCR\`
as a fallback and write the full path into the app config so PATH is
not required. If you installed to a custom location, open Settings in
the app and set the **Tesseract executable** path directly.

**"Model 'qwen2.5:3b' is not installed"**
&nbsp;&nbsp; Open a terminal and run: `ollama pull qwen2.5:3b`

**OCR result is gibberish on a scanned PDF**
&nbsp;&nbsp; Try changing the OCR engine in Settings from `pytesseract`
to `easyocr` (more robust on noisy images but slower and larger). You
can also try `--psm 4` or `--psm 11` in `extractor._ocr_pytesseract`.

**Excel formulas show `#REF!` after extraction**
&nbsp;&nbsp; This shouldn't happen; the writer never touches the example
row or the formula. If you see it, file an issue with the output file.

**Empty rows after extracting from a multi-building park flyer**
&nbsp;&nbsp; The LLM may have failed to parse the layout. Try a larger
model (e.g. `llama3.1:70b` or `qwen2.5:32b`) — the prompt explicitly tells
the model to enumerate every building separately, but model capability
matters here.

---

## License

For internal KBC use.

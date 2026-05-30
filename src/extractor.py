"""
Stage 2: Text extraction.

Routes each detected file to the right extractor:

  - Digital PDFs   -> pdfplumber (layout-aware extraction, preserves table-ish
                      column structure that flyers love to use).
  - Scanned PDFs   -> render each page to PNG with PyMuPDF, then OCR.
  - Images         -> OCR directly.

OCR is done with pytesseract by default (lighter, faster, system Tesseract
binary required). easyocr is a heavier alternative that ships its own models
and runs entirely in-process; toggle via config.ocr_engine.

The extractor returns one big string with `--- Page N ---` separators so the
downstream LLM can still tell which content came from which page (helpful
for multi-building flyers where Page 1 is the park overview).
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from .config import Config
from .detector import DetectionResult, FileKind


log = logging.getLogger("flyer_reader")


# ---------------------------------------------------------------------------
# Tesseract auto-discovery
#
# The app should not depend on the installer having written tesseract_cmd
# into the config. If the user moves the project folder, runs a fresh copy,
# or the install-time config write didn't take, we locate the Tesseract
# executable ourselves — PATH first, then the standard install locations.
# Mirrors the Ollama auto-discovery in llm_client.py.
# ---------------------------------------------------------------------------

# Default install locations per platform, in priority order.
_TESSERACT_CANDIDATES: dict[str, list[str]] = {
    "win32": [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe",
        r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe",
    ],
    "darwin": [
        "/opt/homebrew/bin/tesseract",
        "/usr/local/bin/tesseract",
    ],
    "linux": [
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
    ],
}

# Cached across OCR calls so we scan the disk at most once per run.
_TESSERACT_PATH: Optional[str] = None
_TESSERACT_SCANNED = False


def _find_tesseract_exe() -> Optional[str]:
    """
    Return the path to the tesseract executable, or None if not found.

    Checks (in order):
      1. 'tesseract' on the system PATH.
      2. Known default install locations per platform.
    """
    found = shutil.which("tesseract")
    if found:
        return found

    for raw in _TESSERACT_CANDIDATES.get(sys.platform, []):
        expanded = os.path.expandvars(raw)   # resolves %LOCALAPPDATA% etc.
        if Path(expanded).is_file():
            return expanded

    return None


def _resolve_tesseract(cfg: Config) -> Optional[str]:
    """
    Decide which tesseract path to use for this run.

    Priority:
      1. cfg.tesseract_cmd, but ONLY if it still points at a real file
         (a stale path from a previous machine/location is ignored).
      2. Auto-discovery via _find_tesseract_exe().

    When auto-discovery succeeds and the config was empty or stale, the
    discovered path is written back to the config so the next run is
    instant and the Settings dialog shows the right value.
    """
    global _TESSERACT_PATH, _TESSERACT_SCANNED

    # 1. Trust a configured path only if the file actually exists.
    if cfg.tesseract_cmd and Path(cfg.tesseract_cmd).is_file():
        return cfg.tesseract_cmd

    # 2. Auto-discover (cached after the first scan).
    if not _TESSERACT_SCANNED:
        _TESSERACT_PATH = _find_tesseract_exe()
        _TESSERACT_SCANNED = True
        if _TESSERACT_PATH:
            log.info("Tesseract auto-discovered at: %s", _TESSERACT_PATH)
            # Persist so future runs skip the scan and Settings reflects it.
            try:
                cfg.tesseract_cmd = _TESSERACT_PATH
                cfg.save()
            except Exception:
                pass  # non-fatal: a read-only config just means we re-scan next time

    return _TESSERACT_PATH


# ---------------------------------------------------------------------------
# Boilerplate filtering
#
# Marketing flyers sometimes bundle in pages that are pure noise for our
# purposes — most commonly an architect's electrical/MEP callout sheet
# ("PROVIDE 20A-120V DUPLEX RECEPTACLE...") that can be 50-60% of the
# extracted text. Feeding that to the LLM buries the ~400 useful characters
# of real spec data in thousands of characters of irrelevant construction
# boilerplate, and small models lose the signal entirely.
#
# We detect such pages by keyword density and drop them before the LLM
# stage. The detection is deliberately conservative: a page must be both
# long AND saturated with construction-callout vocabulary to be dropped,
# so a normal spec page (which might mention "sprinkler" or "power" once)
# is never removed.
# ---------------------------------------------------------------------------

# Vocabulary typical of architect/MEP callout sheets. Real estate marketing
# copy almost never uses these words, and never repeatedly.
_BOILERPLATE_TERMS = (
    "receptacle", "duplex", "j-box", "junction box", "power pack",
    "title 24", "mud ring", "conduit", "stubbed", "gfci", "gfi",
    "transformer", "raceway", "cover plate", "back splash",
    "1/2\"c", "3/4\"c", "#12", "aff", "sensor switch",
)


def _is_boilerplate_page(page_text: str) -> bool:
    """
    True if a page looks like architect/MEP callout boilerplate rather than
    marketing/spec content worth sending to the LLM.

    Criteria (must meet ALL):
      - the page is reasonably long (short pages are cheap to keep)
      - boilerplate terms appear many times (saturation, not a passing mention)
      - boilerplate terms make up a meaningful share of the page's words
    """
    low = page_text.lower()
    n_chars = len(low)
    if n_chars < 600:
        return False  # short pages are harmless — keep them

    hits = sum(low.count(term) for term in _BOILERPLATE_TERMS)
    if hits < 8:
        return False  # not saturated enough — keep it

    # Density check: roughly how many boilerplate hits per 1000 chars.
    density = hits / (n_chars / 1000.0)
    return density >= 3.0


def _rescue_useful_lines(page_text: str) -> list[str]:
    """
    Scan a boilerplate page for individual lines worth keeping even though
    the page as a whole is noise — primarily street addresses, which often
    appear in an office-plan footer buried mid-page.

    A line is rescued if it looks like a US address: contains a 5-digit ZIP
    and a 2-letter state or a spelled-out state name.
    """
    import re

    rescued: list[str] = []
    # ZIP: 5 digits, optionally preceded by a 2-letter state.
    zip_re = re.compile(r"\b\d{5}\b")
    state_re = re.compile(
        r"\b(?:A[LKZR]|C[AOT]|DE|FL|GA|HI|I[DLNA]|K[SY]|LA|M[EDAINSOT]|"
        r"N[EVHJMYCD]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[TA]|W[AVIY]|"
        r"California|Texas|Nevada|Arizona|Oregon|Washington)\b"
    )
    for raw_line in page_text.split("\n"):
        line = raw_line.strip()
        if 8 <= len(line) <= 200 and zip_re.search(line) and state_re.search(line):
            rescued.append(line)
    return rescued


def _filter_boilerplate(full_text: str) -> tuple[str, list[int]]:
    """
    Given the full extracted text (pages joined with '--- Page N ---'
    markers), trim pages that look like construction-callout boilerplate.

    A boilerplate page is not deleted outright:
      - its first ~600 characters are kept (page headers)
      - any address-like line anywhere on the page is rescued
    Only the saturated callout body is dropped. This keeps the noise out
    while preserving the occasional useful line such as a street address.

    Returns (filtered_text, trimmed_page_numbers). If trimming would empty
    every page, nothing is trimmed.
    """
    import re

    parts = re.split(r"--- Page (\d+) ---\n", full_text)
    if len(parts) < 3:
        return full_text, []

    pages: list[tuple[int, str]] = []
    for i in range(1, len(parts), 2):
        page_no = int(parts[i])
        page_text = parts[i + 1] if i + 1 < len(parts) else ""
        pages.append((page_no, page_text))

    kept: list[str] = []
    trimmed: list[int] = []
    HEAD_KEEP = 600  # chars of a boilerplate page to retain (header band)

    for page_no, page_text in pages:
        if _is_boilerplate_page(page_text):
            trimmed.append(page_no)
            head = page_text[:HEAD_KEEP].rstrip()
            # Rescue address lines from the dropped body.
            rescued = _rescue_useful_lines(page_text[HEAD_KEEP:])
            block = f"--- Page {page_no} (boilerplate trimmed) ---\n{head}"
            if rescued:
                block += "\n" + "\n".join(rescued)
            kept.append(block)
        else:
            kept.append(f"--- Page {page_no} ---\n{page_text.rstrip()}")

    if not kept:
        return full_text, []

    return "\n\n".join(kept).strip(), trimmed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_text(detection: DetectionResult, cfg: Config) -> str:
    """
    Return the full extracted text content of the file. Raises on hard failure.

    Previously this function also ran a "boilerplate filter" that tried to
    trim architect/MEP callout pages. That filter has been removed: it was
    over-aggressive and routinely discarded the page headers and footers
    that carry the building's street address, office square footage, and
    other real spec data. The LLM is better at ignoring noise than we are
    at predicting which page is "boilerplate" — so we hand it everything.
    """
    if detection.kind == FileKind.PDF_DIGITAL:
        raw = _extract_pdf_digital(detection.path, cfg)
    elif detection.kind == FileKind.PDF_SCANNED:
        raw = _extract_pdf_scanned(detection.path, cfg)
    elif detection.kind == FileKind.IMAGE:
        raw = _ocr_image_file(detection.path, cfg)
    else:
        raise ValueError(
            f"Cannot extract text from {detection.kind} ({detection.path})."
        )
    return raw


def extract_text_with_pages(
    detection: DetectionResult, cfg: Config,
) -> tuple[str, dict[int, str]]:
    """
    Like extract_text, but also returns a {page_number: text} mapping so
    callers (e.g. the review dialog) can attribute extracted fields back
    to specific pages of the original document.

    For non-PDF inputs (single images), the mapping contains a single
    entry {1: <whole text>}. For PDFs, page numbers are 1-indexed.

    Returns:
        (joined_text, page_map). The joined_text matches what extract_text
        would return for the same input, so callers that have only the
        joined string don't see any behavior change.
    """
    if detection.kind == FileKind.PDF_DIGITAL:
        per_page = _extract_pdf_digital_per_page(detection.path, cfg)
        joined = _join_per_page(per_page)
        page_map = {pno: text for pno, text, _src in per_page}
        return joined, page_map
    elif detection.kind == FileKind.PDF_SCANNED:
        # Scanned PDFs go through OCR for the whole document. We don't
        # currently track per-page OCR splits there; treat as page 1.
        raw = _extract_pdf_scanned(detection.path, cfg)
        return raw, {1: raw}
    elif detection.kind == FileKind.IMAGE:
        raw = _ocr_image_file(detection.path, cfg)
        return raw, {1: raw}
    else:
        raise ValueError(
            f"Cannot extract text from {detection.kind} ({detection.path})."
        )


# ---------------------------------------------------------------------------
# Digital PDFs: pdfplumber preserves layout reasonably well.
# We fall back to PyMuPDF if pdfplumber chokes on a weirdly-encoded PDF.
# ---------------------------------------------------------------------------

def _extract_pdf_digital(path: Path, cfg: Optional[Config] = None) -> str:
    """
    Extract text from a PDF, returning a single joined string. Thin wrapper
    around _extract_pdf_digital_per_page that flattens the per-page list.
    Kept for backwards compatibility with callers that only want the text.
    """
    per_page = _extract_pdf_digital_per_page(path, cfg)
    return _join_per_page(per_page)


def _join_per_page(per_page_text: list[tuple[int, str, str]]) -> str:
    """Join a per-page list into the legacy single-string format."""
    out: list[str] = []
    rescued: list[int] = []
    for page_no, text, source in per_page_text:
        clean = text.strip()
        if not clean:
            continue
        if source.startswith("ocr"):
            rescued.append(page_no)
        out.append(f"--- Page {page_no} ---\n{clean}")
    if rescued:
        log.info("Per-page OCR rescued empty pages: %s",
                 ", ".join(map(str, rescued)))
    return "\n\n".join(out).strip()


def _extract_pdf_digital_per_page(
    path: Path, cfg: Optional[Config] = None,
) -> list[tuple[int, str, str]]:
    """
    Extract text from a PDF, page by page, using a "grab everything" strategy.

    For each page we collect:
      1. The page's tables as pipe-delimited rows (extract_tables()).
         Spec sheets in flyers are very often laid out as tables, and pdfplumber's
         plain extract_text() misses or scrambles them. Pulling tables FIRST,
         then plain text, gives the LLM the structured spec data even when the
         visual layout is multi-column.
      2. The page's plain text via extract_text() with generous tolerances.
         We deliberately do NOT use layout=True — counterintuitively, layout
         mode pads cells with whitespace and that padding survives downstream,
         making columnar data harder to read for a small LLM, not easier.

    For pages with no embedded text at all (rendered images, scanned inserts)
    we per-page OCR-rescue them when a cfg is supplied.

    This is the same approach the previous version of this extractor used
    successfully — it produces roughly 1.5-2x more text per spec page than
    the layout-mode approach, which directly translates into more fields the
    LLM can fill in.

    Returns a list of (page_no, page_text, source) tuples. The source string
    is "digital", "digital(pymupdf)", or "ocr-rescue:<engine>".
    """
    per_page_text: list[tuple[int, str, str]] = []

    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                parts: list[str] = []
                # 1. Tables first — these are the spec blocks in most flyers.
                try:
                    tables = page.extract_tables()
                    for table in tables:
                        for row in table:
                            row_text = " | ".join(
                                str(cell).strip() if cell else ""
                                for cell in row
                            ).strip(" |")
                            if row_text:
                                parts.append(row_text)
                except Exception:
                    pass  # Some pages have no tables — that's fine.

                # 2. Plain text with generous tolerances. We use x_tolerance=3
                #    and y_tolerance=3 (not layout=True) because that produces
                #    the cleanest output for the LLM: words stay together,
                #    columns interleave across lines, no whitespace padding.
                text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                if text.strip():
                    parts.append(text)

                page_text = "\n".join(parts)
                per_page_text.append((i, page_text, "digital"))
    except Exception as e:
        log.warning("pdfplumber failed (%s); falling back to PyMuPDF.", e)
        per_page_text = _pymupdf_per_page(path)

    # If pdfplumber gave us nothing at all, use PyMuPDF as a second try.
    if not any(t.strip() for _, t, _ in per_page_text):
        per_page_text = _pymupdf_per_page(path)

    # Per-page OCR rescue: any page that came back empty gets OCR'd, IF we
    # have a config (and therefore an OCR engine) to do it with.
    if cfg is not None:
        per_page_text = _ocr_empty_pages(path, per_page_text, cfg)

    return per_page_text


def _pymupdf_per_page(path: Path) -> list[tuple[int, str, str]]:
    """Per-page text extraction via PyMuPDF (used as a pdfplumber fallback)."""
    import fitz
    out: list[tuple[int, str, str]] = []
    doc = fitz.open(str(path))
    try:
        for i, page in enumerate(doc, start=1):
            out.append((i, page.get_text("text"), "digital(pymupdf)"))
    finally:
        doc.close()
    return out


def _ocr_empty_pages(
    path: Path,
    pages: list[tuple[int, str, str]],
    cfg: Config,
) -> list[tuple[int, str, str]]:
    """
    For each page whose extracted text is empty, rasterize that single
    page at 300 DPI and run OCR on it. Pages with text are returned
    untouched. Failures are non-fatal: a page that can't be OCR'd just
    stays empty and we move on.
    """
    empty_idx = [i for i, (_, t, _) in enumerate(pages) if not t.strip()]
    if not empty_idx:
        return pages

    import fitz
    from PIL import Image
    import io

    try:
        doc = fitz.open(str(path))
    except Exception as e:
        log.warning("Could not open PDF for per-page OCR: %s", e)
        return pages

    try:
        for idx in empty_idx:
            page_no = pages[idx][0]
            try:
                page = doc[page_no - 1]
                pix = page.get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72))
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                text = _ocr_pil_image(img, cfg)
                if text.strip():
                    pages[idx] = (page_no, text, "ocr(rescue)")
            except Exception as e:
                log.warning("OCR rescue failed for page %d: %s", page_no, e)
    finally:
        doc.close()

    return pages


def _collapse_padding(text: str) -> str:
    """
    pdfplumber's layout mode pads with many spaces to reproduce the visual
    grid. Collapse runs of 3+ spaces down to a tab-like separator (' | ')
    so column boundaries stay visible but lines are not hundreds of chars
    wide. Runs of 1-2 spaces (normal word spacing) are left alone.
    """
    import re
    lines = []
    for line in text.split("\n"):
        # 3+ spaces => a column gap; mark it so the LLM sees the boundary.
        collapsed = re.sub(r" {3,}", "   ", line.rstrip())
        lines.append(collapsed)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scanned PDFs: render each page to a high-DPI PNG, then OCR.
# ---------------------------------------------------------------------------

def _extract_pdf_scanned(path: Path, cfg: Config) -> str:
    import fitz
    from PIL import Image
    import io

    doc = fitz.open(str(path))
    pages_text: list[str] = []
    try:
        for i, page in enumerate(doc, start=1):
            # 300 DPI matrix: 300/72 = ~4.17. Good balance of OCR quality vs speed.
            pix = page.get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72))
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            pages_text.append(f"--- Page {i} ---\n{_ocr_pil_image(img, cfg).strip()}")
    finally:
        doc.close()
    return "\n\n".join(pages_text).strip()


# ---------------------------------------------------------------------------
# Image files
# ---------------------------------------------------------------------------

def _ocr_image_file(path: Path, cfg: Config) -> str:
    from PIL import Image
    img = Image.open(str(path))
    return _ocr_pil_image(img, cfg).strip()


# ---------------------------------------------------------------------------
# OCR backends
# ---------------------------------------------------------------------------

_EASYOCR_READER = None  # cached across calls; the reader is expensive to construct


def _ocr_pil_image(img, cfg: Config) -> str:
    if cfg.ocr_engine == "easyocr":
        return _ocr_easyocr(img)
    return _ocr_pytesseract(img, cfg)


def _ocr_pytesseract(img, cfg: Config) -> str:
    import pytesseract

    # Resolve the Tesseract binary: configured path if valid, else
    # auto-discover on PATH / default install locations.
    tess_path = _resolve_tesseract(cfg)
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path
    else:
        raise RuntimeError(
            "Tesseract OCR could not be found. It is required for image "
            "flyers and scanned PDFs.\n"
            "Install it from https://github.com/UB-Mannheim/tesseract/wiki "
            "(default location is detected automatically), or set the "
            "Tesseract executable path in the app's Settings dialog."
        )

    # PSM 6 = "Assume a single uniform block of text." Flyers vary, but PSM 6
    # works better than the default for the mixed-layout marketing flyers we
    # care about. Tune to 4 (single column) if you see degraded results.
    return pytesseract.image_to_string(img, config="--oem 1 --psm 6")


def _ocr_easyocr(img) -> str:
    global _EASYOCR_READER
    import easyocr
    import numpy as np

    if _EASYOCR_READER is None:
        # First call downloads ~100MB of model weights.
        _EASYOCR_READER = easyocr.Reader(["en"], gpu=False)

    arr = np.array(img.convert("RGB"))
    results = _EASYOCR_READER.readtext(arr, detail=0, paragraph=True)
    return "\n".join(results)

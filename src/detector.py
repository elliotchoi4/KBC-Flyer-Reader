"""
Stage 1: Format detection & routing.

Inspects an input file and decides which extraction pipeline to use:
  - Image files (.png, .jpg, .webp, .tiff, .bmp) -> OCR
  - PDFs       -> first try direct text extraction. If the result is too
                  sparse (likely a scanned PDF), fall back to OCR on the
                  rasterized pages.

We use `python-magic` if available (more reliable for misnamed files) and
fall back to suffix-based detection.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class FileKind(str, Enum):
    PDF_DIGITAL = "pdf_digital"
    PDF_SCANNED = "pdf_scanned"
    IMAGE = "image"
    UNSUPPORTED = "unsupported"


@dataclass
class DetectionResult:
    kind: FileKind
    mime: str
    path: Path
    detail: str = ""


# Bytes of text below which we assume the PDF is scanned and needs OCR.
# Real scanned PDFs typically return 0–10 chars of garbage from get_text().
# Real digital flyers, even sparse single-pagers, easily clear 30 chars.
# Setting the bar at 30 keeps us safely on the "digital" side for genuine
# digital PDFs while still catching scans.
SCANNED_PDF_TEXT_THRESHOLD = 30


def _sniff_mime(path: Path) -> str:
    """Best-effort MIME sniffing. Tries libmagic, falls back to suffix."""
    try:
        import magic  # type: ignore
        return magic.from_file(str(path), mime=True)
    except Exception:
        suffix = path.suffix.lower()
        return {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
            ".bmp": "image/bmp",
        }.get(suffix, "application/octet-stream")


def _is_scanned_pdf(path: Path) -> bool:
    """Open the PDF with PyMuPDF, peek at extractable text from every page."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        # If PyMuPDF isn't installed, we err toward treating the PDF as
        # digital so the user gets text rather than nothing.
        return False
    try:
        doc = fitz.open(str(path))
    except Exception:
        return False
    total_text = 0
    try:
        for page in doc:
            total_text += len(page.get_text("text").strip())
            if total_text >= SCANNED_PDF_TEXT_THRESHOLD:
                return False
    finally:
        doc.close()
    return total_text < SCANNED_PDF_TEXT_THRESHOLD


def detect(path: str | Path) -> DetectionResult:
    """Classify a file. Returns DetectionResult.kind == UNSUPPORTED if unknown."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return DetectionResult(FileKind.UNSUPPORTED, "", p, "File does not exist.")

    mime = _sniff_mime(p)

    if mime.startswith("image/"):
        return DetectionResult(FileKind.IMAGE, mime, p)

    if mime == "application/pdf":
        if _is_scanned_pdf(p):
            return DetectionResult(FileKind.PDF_SCANNED, mime, p, "No embedded text; will OCR.")
        return DetectionResult(FileKind.PDF_DIGITAL, mime, p)

    return DetectionResult(FileKind.UNSUPPORTED, mime, p, f"Unsupported MIME type: {mime}")

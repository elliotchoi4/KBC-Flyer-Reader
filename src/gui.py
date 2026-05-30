"""
Drag-and-drop GUI — "Warm Paper" style (CustomTkinter).

Built on CustomTkinter for the modern rounded look, plus tkinterdnd2 for
cross-platform drag-and-drop. If tkinterdnd2 is missing the app still
works — it just falls back to the "click to browse" button.

Layout:

  +-----------------------------------------------------------+
  |  KBC Flyer Reader                            [ Settings ] |
  |  [ Survey type: Building | Land ]   [ Engine: ... ]       |
  |  +---------------------------------------------------+    |
  |  |        Drop your flyers here (PDF, PNG, JPG)      |    |
  |  +---------------------------------------------------+    |
  |  Files queued: ...                                        |
  |  Additional instructions: ...                             |
  |  Output: (•) new  ( ) append    Name: [______]           |
  |  [ Clear ]  [x] Review            [ Extract -> ] [ Stop ] |
  |  +--- stage banner: big readable "what's happening" ---+  |
  |  |  (o)  Reading flyer 1 of 2                    0:07  |  |
  |  |       Extracting fields with AI                    |  |
  |  |  [============ progress ============]        42%   |  |
  |  +---------------------------------------------------+    |
  |  [ Show detailed log v ]                                  |
  |  +--- (collapsible) log ----------------------------+     |
  +-----------------------------------------------------------+
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk

# Optional DnD support
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND_AVAILABLE = True
except Exception:
    _DND_AVAILABLE = False

from .config import Config
from .llm_client import ping_ollama
from .pipeline import process_flyers, ProgressEvent
from .field_hints import FieldHints
from .theme import WP, apply_ctk_appearance, style_ttk


SUPPORTED_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}


def _resource_path(*parts: str) -> Path:
    """
    Resolve a bundled resource path that works both when running from
    source and when frozen by PyInstaller (which unpacks data files into
    a temp dir exposed as sys._MEIPASS).
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base, *parts)
    # From source: this file is src/gui.py, so the project root is parent
    # of the package directory.
    return Path(__file__).resolve().parent.parent / Path(*parts)


_ICON_PATH = _resource_path("assets", "icon.ico")


# Max length of user-entered "additional instructions" text. Matches the
# server-side cap in llm_client._EXTRA_INSTRUCTIONS_MAX_CHARS so the user
# is never silently truncated past what they see.
_EXTRA_INSTRUCTIONS_GUI_LIMIT = 2000


# ---------------------------------------------------------------------------
# DnD-enabled CustomTkinter root
# ---------------------------------------------------------------------------
# tkinterdnd2 needs the root to advertise the tkdnd Tcl package. The standard
# recipe is to mix CTk with TkinterDnD.DnDWrapper and call _require() once.

if _DND_AVAILABLE:
    class _RootWindow(ctk.CTk, TkinterDnD.DnDWrapper):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            try:
                self.TkdndVersion = TkinterDnD._require(self)
            except Exception:
                # If the tkdnd package can't load, DnD just won't work;
                # the browse button still does.
                self.TkdndVersion = None
else:
    _RootWindow = ctk.CTk


# Small helpers to keep widget creation terse and consistent. -----------------

def _primary_btn(parent, text, command, **kw):
    kw.setdefault("height", 38)
    return ctk.CTkButton(
        parent, text=text, command=command,
        fg_color=WP.ACCENT, hover_color=WP.ACCENT_HOVER,
        text_color=WP.ON_ACCENT, corner_radius=WP.RADIUS_PILL,
        font=(WP.FONT_FAMILY, WP.SIZE_BODY, "bold"), **kw)


def _ghost_btn(parent, text, command, **kw):
    kw.setdefault("height", 34)
    return ctk.CTkButton(
        parent, text=text, command=command,
        fg_color="transparent", hover_color=WP.SECONDARY_BG,
        text_color=WP.ACCENT, border_color=WP.BORDER, border_width=1,
        corner_radius=WP.RADIUS_PILL, font=(WP.FONT_FAMILY, WP.SIZE_SMALL),
        **kw)


def _danger_btn(parent, text, command, **kw):
    kw.setdefault("height", 38)
    return ctk.CTkButton(
        parent, text=text, command=command,
        fg_color=WP.DANGER_RED, hover_color="#a83c24",
        text_color=WP.ON_ACCENT, corner_radius=WP.RADIUS_PILL,
        font=(WP.FONT_FAMILY, WP.SIZE_SMALL, "bold"), **kw)


def _card(parent, **kw):
    return ctk.CTkFrame(
        parent, fg_color=WP.PANEL_BG, corner_radius=WP.RADIUS,
        border_color=WP.BORDER, border_width=1, **kw)


def _caption(parent, text, **kw):
    return ctk.CTkLabel(parent, text=text, text_color=WP.TEXT_MUTED,
                        font=(WP.FONT_FAMILY, WP.SIZE_TINY), **kw)


class _Tooltip:
    """
    A lightweight hover tooltip for any Tk/CTk widget. Shows a small
    bordered popup near the cursor on <Enter>, hides on <Leave>. Supports
    multi-line text; the popup wraps to a fixed width.
    """

    def __init__(self, widget, text: str, *, wraplength: int = 360,
                 delay_ms: int = 250):
        self.widget = widget
        self.text = text
        self.wraplength = wraplength
        self.delay_ms = delay_ms
        self._tip = None
        self._after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self):
        if self._tip is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 24
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        except Exception:
            return
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.configure(bg=WP.BORDER_STRONG)
        # 1px border via an outer frame, then the padded label inside.
        inner = tk.Frame(tw, bg=WP.SECONDARY_BG)
        inner.pack(padx=1, pady=1)
        tk.Label(
            inner, text=self.text, justify="left", anchor="w",
            bg=WP.SECONDARY_BG, fg=WP.TEXT, wraplength=self.wraplength,
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL), padx=12, pady=10,
        ).pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


# Step-by-step guide shown by the "?" next to the Claude API key field.
_CLAUDE_KEY_HELP = (
    "Ask a team member for an existing key, or follow these steps:\n\n"
    "1. Go to console.anthropic.com and sign in (or create an account).\n"
    "2. Open the account menu and choose \"API keys\".\n"
    "3. Click \"Create Key\", give it a name (e.g. \"KBC Flyer Reader\").\n"
    "4. Copy the key now — it's shown only once. It starts with \"sk-ant-\".\n"
    "5. Paste it into the Claude API key box here and click Save.\n\n"
    "Note: using the API may incur usage charges on that account."
)


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent, cfg: Config):
        super().__init__(parent)
        self.title("Settings")
        self.configure(fg_color=WP.PAGE_BG)
        self.resizable(False, False)
        self.cfg = cfg

        frm = ctk.CTkFrame(self, fg_color="transparent")
        frm.pack(fill="both", expand=True, padx=16, pady=14)
        frm.columnconfigure(1, weight=1)

        def lbl(text, r):
            return ctk.CTkLabel(frm, text=text, text_color=WP.TEXT,
                                font=(WP.FONT_FAMILY, WP.SIZE_SMALL),
                                anchor="w").grid(
                row=r, column=0, sticky="w", pady=4, padx=(0, 10))

        def entry(var, r, **kw):
            e = ctk.CTkEntry(frm, textvariable=var, fg_color=WP.PANEL_BG,
                             border_color=WP.BORDER, text_color=WP.TEXT,
                             corner_radius=WP.RADIUS_SM, height=32, **kw)
            e.grid(row=r, column=1, sticky="we", pady=4)
            return e

        row = 0
        # --- Provider selector ---------------------------------------------
        self._provider_lbl = ctk.CTkLabel(
            frm, text="Extraction provider", text_color=WP.TEXT,
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL), anchor="w")
        self._provider_lbl.grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
        self.provider_var = tk.StringVar(
            value="Claude API" if cfg.provider == "claude" else "Local (Ollama)")
        ctk.CTkOptionMenu(
            frm, variable=self.provider_var,
            values=["Local (Ollama)", "Claude API"],
            command=lambda _v: self._refresh_provider_fields(),
            fg_color=WP.PANEL_BG, button_color=WP.ACCENT,
            button_hover_color=WP.ACCENT_HOVER, text_color=WP.TEXT,
            corner_radius=WP.RADIUS_SM,
        ).grid(row=row, column=1, sticky="we", pady=4)

        row += 1
        # --- Claude API key (password field + show/hide + help) ------------
        self.claude_key_label = ctk.CTkLabel(
            frm, text="Claude API key", text_color=WP.TEXT,
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL), anchor="w")
        self.claude_key_label.grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
        key_row = ctk.CTkFrame(frm, fg_color="transparent")
        key_row.grid(row=row, column=1, sticky="we", pady=4)
        key_row.columnconfigure(0, weight=1)
        self.claude_key_var = tk.StringVar(value=cfg.claude_key_for_display())
        self.claude_key_entry = ctk.CTkEntry(
            key_row, textvariable=self.claude_key_var, fg_color=WP.PANEL_BG,
            border_color=WP.BORDER, text_color=WP.TEXT,
            corner_radius=WP.RADIUS_SM, height=32, show="•",
            placeholder_text="sk-ant-...")
        self.claude_key_entry.grid(row=0, column=0, sticky="we")
        # Show/hide toggle.
        self._key_shown = False
        self.claude_key_toggle = _ghost_btn(
            key_row, "Show", self._toggle_key_visibility, width=58)
        self.claude_key_toggle.grid(row=0, column=1, padx=(6, 0))
        # "?" help button with a hover tooltip describing how to get a key.
        self.claude_key_help = _ghost_btn(key_row, "?", lambda: None, width=34)
        self.claude_key_help.configure(font=(WP.FONT_FAMILY, WP.SIZE_BODY, "bold"))
        self.claude_key_help.grid(row=0, column=2, padx=(6, 0))
        _Tooltip(self.claude_key_help, _CLAUDE_KEY_HELP, wraplength=380)

        row += 1
        self.claude_hint = ctk.CTkLabel(
            frm,
            text="Hover the “?” for how to create a key. Stored securely on this PC.",
            text_color=WP.TEXT_FAINT, font=(WP.FONT_FAMILY, WP.SIZE_TINY),
            anchor="w")
        self.claude_hint.grid(row=row, column=1, sticky="w")

        row += 1
        self.claude_model_label = ctk.CTkLabel(
            frm, text="Claude model", text_color=WP.TEXT,
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL), anchor="w")
        self.claude_model_label.grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
        self.claude_model_var = tk.StringVar(value=cfg.claude_model)
        self.claude_model_entry = entry(self.claude_model_var, row)

        row += 1
        self.url_label = ctk.CTkLabel(
            frm, text="Ollama base URL", text_color=WP.TEXT,
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL), anchor="w")
        self.url_label.grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
        self.url_var = tk.StringVar(value=cfg.ollama_base_url)
        self.url_entry = entry(self.url_var, row)

        row += 1
        self.model_label = ctk.CTkLabel(
            frm, text="Ollama model", text_color=WP.TEXT,
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL), anchor="w")
        self.model_label.grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
        self.model_var = tk.StringVar(value=cfg.ollama_model)
        self.model_entry = entry(self.model_var, row)

        row += 1
        lbl("OCR engine", row)
        self.ocr_var = tk.StringVar(value=cfg.ocr_engine)
        ctk.CTkOptionMenu(
            frm, variable=self.ocr_var, values=["pytesseract", "easyocr"],
            fg_color=WP.PANEL_BG, button_color=WP.ACCENT,
            button_hover_color=WP.ACCENT_HOVER, text_color=WP.TEXT,
            corner_radius=WP.RADIUS_SM,
        ).grid(row=row, column=1, sticky="we", pady=4)

        row += 1
        lbl("Tesseract path (blank = system PATH)", row)
        self.tess_var = tk.StringVar(value=cfg.tesseract_cmd)
        entry(self.tess_var, row)

        row += 1
        lbl("Parallel extractions", row)
        self.parallel_var = tk.StringVar(value=str(cfg.max_parallel_extractions))
        ctk.CTkOptionMenu(
            frm, variable=self.parallel_var,
            values=[str(i) for i in range(1, 9)],
            fg_color=WP.PANEL_BG, button_color=WP.ACCENT,
            button_hover_color=WP.ACCENT_HOVER, text_color=WP.TEXT,
            corner_radius=WP.RADIUS_SM, width=80,
        ).grid(row=row, column=1, sticky="w", pady=4)

        row += 1
        lbl("Default output folder", row)
        self.out_var = tk.StringVar(value=cfg.default_output_dir)
        out_row = ctk.CTkFrame(frm, fg_color="transparent")
        out_row.grid(row=row, column=1, sticky="we", pady=4)
        out_row.columnconfigure(0, weight=1)
        self.out_entry = ctk.CTkEntry(
            out_row, textvariable=self.out_var, fg_color=WP.PANEL_BG,
            border_color=WP.BORDER, text_color=WP.TEXT,
            corner_radius=WP.RADIUS_SM, height=32)
        self.out_entry.grid(row=0, column=0, sticky="we")
        _ghost_btn(out_row, "📂  Browse", self._browse_output_folder,
                   width=100).grid(row=0, column=1, padx=(6, 0))

        row += 1
        lbl("Low-confidence threshold (0-1)", row)
        self.thresh_var = tk.StringVar(value=str(cfg.low_confidence_threshold))
        entry(self.thresh_var, row)

        row += 1
        btns = ctk.CTkFrame(frm, fg_color="transparent")
        btns.grid(row=row, column=0, columnspan=2, pady=(16, 0), sticky="e")
        _ghost_btn(btns, "Test connection", self._test).pack(side="left", padx=4)
        _ghost_btn(btns, "Cancel", self.destroy).pack(side="left", padx=4)
        _primary_btn(btns, "Save", self._save, height=34).pack(side="left", padx=4)

        self._refresh_provider_fields()
        self.transient(parent)
        # grab_set after the window is visible, else it can fail on some WMs.
        self.after(120, self.grab_set)

    def _toggle_key_visibility(self):
        """Show or mask the Claude API key text."""
        self._key_shown = not self._key_shown
        self.claude_key_entry.configure(show="" if self._key_shown else "•")
        self.claude_key_toggle.configure(text="Hide" if self._key_shown else "Show")

    def _browse_output_folder(self):
        """Pick the default output folder via the OS folder picker."""
        current = self.out_var.get().strip() or self.cfg.default_output_dir
        chosen = filedialog.askdirectory(
            title="Choose the default output folder",
            initialdir=str(current) if current else None,
            mustexist=False, parent=self)
        if chosen:
            self.out_var.set(chosen)

    def _refresh_provider_fields(self):
        """Enable/grey the provider-specific fields based on the dropdown."""
        is_claude = self.provider_var.get().startswith("Claude")
        claude_state = "normal" if is_claude else "disabled"
        ollama_state = "disabled" if is_claude else "normal"
        for w in (self.claude_key_entry, self.claude_key_toggle,
                  self.claude_key_help, self.claude_model_entry):
            w.configure(state=claude_state)
        for w in (self.url_entry, self.model_entry):
            w.configure(state=ollama_state)
        fg_claude = WP.TEXT if is_claude else WP.TEXT_FAINT
        fg_ollama = WP.TEXT if not is_claude else WP.TEXT_FAINT
        for lab in (self.claude_key_label, self.claude_model_label):
            lab.configure(text_color=fg_claude)
        for lab in (self.url_label, self.model_label):
            lab.configure(text_color=fg_ollama)
        self.claude_hint.configure(
            text_color=WP.TEXT_FAINT if is_claude else WP.BORDER_STRONG)

    def _collect(self) -> Config:
        """Build a Config from the current dialog fields."""
        try:
            thresh = float(self.thresh_var.get())
        except ValueError:
            thresh = self.cfg.low_confidence_threshold
        try:
            parallel = max(1, int(self.parallel_var.get()))
        except ValueError:
            parallel = self.cfg.max_parallel_extractions
        # Start from the existing config and override only the fields this
        # dialog edits, so unrelated fields (e.g. claude_privacy_ack,
        # debug_dump, ollama_timeout_seconds) are preserved rather than
        # reset to defaults.
        from dataclasses import replace
        return replace(
            self.cfg,
            provider="claude" if self.provider_var.get().startswith("Claude") else "ollama",
            ollama_base_url=self.url_var.get().strip(),
            ollama_model=self.model_var.get().strip(),
            claude_api_key=self.claude_key_var.get().strip(),
            claude_model=self.claude_model_var.get().strip(),
            ocr_engine=self.ocr_var.get().strip() or "pytesseract",
            tesseract_cmd=self.tess_var.get().strip(),
            max_parallel_extractions=parallel,
            low_confidence_threshold=thresh,
            default_output_dir=self.out_var.get().strip(),
        )

    def _test(self):
        temp = self._collect()
        ok, msg = ping_ollama(temp)  # provider-aware despite the legacy name
        (messagebox.showinfo if ok else messagebox.showwarning)(
            "Connection test", msg, parent=self)

    def _save(self):
        new = self._collect()
        for fld in new.__dataclass_fields__:
            setattr(self.cfg, fld, getattr(new, fld))
        # Route the key to the most secure backend available. On Windows this
        # stores it in the Credential Manager and clears claude_api_key so it
        # is never written to config.json; otherwise it stays in the field.
        self.cfg.persist_claude_key(self.claude_key_var.get().strip())
        self.cfg.save()
        self.destroy()


# ---------------------------------------------------------------------------
# Help dialog — "How it works" flowchart
# ---------------------------------------------------------------------------

class HelpDialog(ctk.CTkToplevel):
    """Tabbed help: the pipeline flowchart, plus a model-comparison guide."""

    # (badge, title, blurb, kind) — kind picks the badge colour.
    _STAGES = [
        ("1", "Add your flyers",
         "Drag in PDFs or images (PNG, JPG, TIFF), or click to browse. "
         "Queue as many as you like for one batch.", "normal"),
        ("2", "Read the flyer",
         "The app detects whether each file is a digital PDF or a scan, "
         "then pulls out the text — running OCR automatically on scans "
         "and photos.", "normal"),
        ("3", "Extract with AI",
         "Your chosen engine — local Ollama or Claude — reads that text "
         "and fills in the survey fields for every property it finds.",
         "normal"),
        ("4", "Review & edit",
         "Optional. With “Review before saving” on, you check each flyer’s "
         "data beside the source page and fix anything — click a field to "
         "jump to where it appears on the page.", "optional"),
        ("5", "Write to Excel",
         "Approved records drop into the locked KBC survey template — "
         "either a brand-new file you name, or appended to an existing "
         "survey.", "normal"),
        ("✓", "Done",
         "You get a finished .xlsx, ready to share. The stage banner shows "
         "the file name and how many records were written.", "done"),
    ]

    # Model comparison. Each entry: (name, tagline, cost, speed, accuracy,
    # is_default). Speed/cost are framed for "KBC Laptops" (Surface Pro).
    _CLOUD_MODELS = [
        ("Claude Sonnet 4.6", "Default — best all-round choice",
         "$$ — roughly a few cents per flyer",
         "~1–3 min total per flyer on a KBC Laptop — OCR runs locally and is "
         "the slow part; the Sonnet AI call itself takes only seconds in the cloud",
         "High — handles messy scans and multi-building flyers reliably",
         True),
        ("Claude Haiku 4.5", "Fastest and cheapest Claude option",
         "$ — the cheapest Claude option, a fraction of Sonnet's cost",
         "~1–3 min total per flyer — same OCR time as Sonnet; Haiku's cloud "
         "call is slightly faster but the difference is minor",
         "Good — great on clean, simple flyers; can miss detail on messy ones",
         False),
        ("Claude Opus 4.7", "Most accurate for difficult flyers",
         "$$$ — the most expensive option per flyer",
         "~1–4 min total per flyer — Opus's cloud call is a little slower than "
         "Sonnet, but OCR still dominates for most flyers",
         "Highest — best on dense, low-quality, or unusual layouts",
         False),
    ]
    _LOCAL_MODELS = [
        ("qwen2.5:3b", "Default local model — best free option",
         "Free — no API charges; runs on the KBC Laptop",
         "~5–12 min total per flyer on a KBC Laptop — multi-building flyers "
         "take longer as the model runs once per building found",
         "Modest — fine for straightforward flyers, less reliable on complex ones",
         True),
        ("qwen2.5:7b / llama3.1:8b", "Heavier, more accurate local models",
         "Free",
         "~20–40 min total per flyer on a KBC Laptop — not practical for "
         "regular use; better suited to overnight batches",
         "Better than 3B, but still below the Claude models",
         False),
        ("qwen2.5:1.5b", "Lightest local model",
         "Free",
         "~3–7 min total per flyer on a KBC Laptop — faster than 3B but "
         "still slow; accuracy drops noticeably",
         "Lowest — only dependable on very simple flyers",
         False),
    ]

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Flyer Reader — Help")
        self.configure(fg_color=WP.PAGE_BG)
        try:
            if _ICON_PATH.exists():
                self.iconbitmap(str(_ICON_PATH))
        except Exception:
            pass

        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = min(600, int(sw * 0.92))
        h = min(720, int(sh * 0.92))
        self.geometry(f"{w}x{h}")
        self.minsize(440, 460)

        # Tabbed body: "How it works" + "Choosing a model".
        self.tabs = ctk.CTkTabview(
            self, fg_color=WP.SECONDARY_BG,
            segmented_button_fg_color=WP.SECONDARY_BG,
            segmented_button_selected_color=WP.ACCENT,
            segmented_button_selected_hover_color=WP.ACCENT_HOVER,
            segmented_button_unselected_color=WP.SECONDARY_BG,
            segmented_button_unselected_hover_color=WP.BORDER,
            text_color=WP.ON_ACCENT, text_color_disabled=WP.TEXT_MUTED)
        self.tabs.pack(fill="both", expand=True, padx=14, pady=(14, 6))
        tab_flow = self.tabs.add("How it works")
        tab_models = self.tabs.add("Choosing a model")
        self._build_howitworks_tab(tab_flow)
        self._build_models_tab(tab_models)

        # Footer close button.
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=20, pady=(0, 16))
        _primary_btn(foot, "Got it", self.destroy, width=120, height=34).pack(
            side="right")

        self.transient(parent)
        self.after(120, self.grab_set)

    # ----- Tab 1: how it works ---------------------------------------------

    def _build_howitworks_tab(self, parent):
        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.pack(fill="x", pady=(2, 4))
        ctk.CTkLabel(
            head, text="How the Flyer Reader works",
            font=(WP.FONT_FAMILY, WP.SIZE_HEADING, "bold"),
            text_color=WP.TEXT, anchor="center", justify="center").pack(fill="x")
        ctk.CTkLabel(
            head, text="From a flyer to a filled-in survey in a few steps.",
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL),
            text_color=WP.TEXT_MUTED, anchor="center", justify="center").pack(
            fill="x")

        body = ctk.CTkScrollableFrame(
            parent, fg_color="transparent",
            scrollbar_button_color=WP.BORDER_STRONG,
            scrollbar_button_hover_color=WP.ACCENT)
        body.pack(fill="both", expand=True, pady=(8, 4))

        for i, (badge, title, blurb, kind) in enumerate(self._STAGES):
            self._add_node(body, badge, title, blurb, kind)
            if i < len(self._STAGES) - 1:
                ctk.CTkLabel(
                    body, text="↓", text_color=WP.ACCENT,
                    font=(WP.FONT_FAMILY, 20, "bold")).pack(pady=1)

    def _add_node(self, parent, badge, title, blurb, kind):
        badge_color = {
            "normal": WP.ACCENT,
            "optional": WP.WARN_AMBER,
            "done": WP.OK_GREEN,
        }.get(kind, WP.ACCENT)

        card = ctk.CTkFrame(
            parent, fg_color=WP.PANEL_BG, corner_radius=WP.RADIUS,
            border_color=WP.BORDER, border_width=1)
        card.pack(fill="x", pady=2)

        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(padx=14, pady=12)

        b = ctk.CTkFrame(inner, width=40, height=40, corner_radius=20,
                         fg_color=badge_color)
        b.pack(pady=(0, 6))
        b.pack_propagate(False)
        ctk.CTkLabel(b, text=badge, text_color=WP.ON_ACCENT,
                     font=(WP.FONT_FAMILY, WP.SIZE_BODY, "bold")).pack(
            expand=True)

        title_row = ctk.CTkFrame(inner, fg_color="transparent")
        title_row.pack()
        ctk.CTkLabel(
            title_row, text=title, text_color=WP.TEXT, anchor="center",
            justify="center",
            font=(WP.FONT_FAMILY, WP.SIZE_HEADING, "bold")).pack(side="left")
        if kind == "optional":
            pill = ctk.CTkLabel(
                title_row, text="optional", text_color=WP.WARN_AMBER,
                fg_color=WP.ACCENT_SOFT, corner_radius=WP.RADIUS_PILL,
                font=(WP.FONT_FAMILY, WP.SIZE_TINY), padx=10, pady=1)
            pill.pack(side="left", padx=(8, 0))

        ctk.CTkLabel(
            inner, text=blurb, text_color=WP.TEXT_MUTED, anchor="center",
            justify="center", wraplength=380,
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL)).pack(pady=(4, 0))

    # ----- Tab 2: choosing a model -----------------------------------------

    def _build_models_tab(self, parent):
        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.pack(fill="x", pady=(2, 4))
        ctk.CTkLabel(
            head, text="Choosing a model",
            font=(WP.FONT_FAMILY, WP.SIZE_HEADING, "bold"),
            text_color=WP.TEXT, anchor="center", justify="center").pack(fill="x")
        ctk.CTkLabel(
            head,
            text=("KBC Laptops are Surface Pro devices. Claude models run in "
                  "the cloud — their speed depends on your internet, not the "
                  "laptop. Local (Ollama) models run on the KBC Laptop's own "
                  "processor: free, but limited by the laptop's CPU."),
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL),
            text_color=WP.TEXT_MUTED, anchor="center", justify="center",
            wraplength=520).pack(fill="x", pady=(2, 0))

        body = ctk.CTkScrollableFrame(
            parent, fg_color="transparent",
            scrollbar_button_color=WP.BORDER_STRONG,
            scrollbar_button_hover_color=WP.ACCENT)
        body.pack(fill="both", expand=True, pady=(8, 4))

        self._section_label(body, "Claude API  ·  cloud, paid, most accurate")
        for entry in self._CLOUD_MODELS:
            self._add_model_card(body, *entry)
        self._section_label(body, "Local (Ollama)  ·  on the KBC Laptop, free")
        for entry in self._LOCAL_MODELS:
            self._add_model_card(body, *entry)

        ctk.CTkLabel(
            body,
            text=("Cost and timing are approximate and vary with each flyer's "
                  "length and quality and with current Anthropic pricing. The "
                  "$ symbols are relative, not exact prices."),
            text_color=WP.TEXT_FAINT, font=(WP.FONT_FAMILY, WP.SIZE_TINY),
            anchor="w", justify="left", wraplength=520).pack(
            fill="x", pady=(8, 2), padx=2)

    def _section_label(self, parent, text):
        ctk.CTkLabel(
            parent, text=text, text_color=WP.TEXT,
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL, "bold"),
            anchor="w").pack(fill="x", pady=(10, 4), padx=2)

    def _add_model_card(self, parent, name, tagline, cost, speed, accuracy,
                        is_default):
        card = ctk.CTkFrame(
            parent, fg_color=WP.PANEL_BG, corner_radius=WP.RADIUS,
            border_color=WP.ACCENT if is_default else WP.BORDER,
            border_width=2 if is_default else 1)
        card.pack(fill="x", pady=3)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=12)

        title_row = ctk.CTkFrame(inner, fg_color="transparent")
        title_row.pack(fill="x")
        ctk.CTkLabel(
            title_row, text=name, text_color=WP.TEXT, anchor="w",
            font=(WP.FONT_FAMILY, WP.SIZE_BODY, "bold")).pack(side="left")
        if is_default:
            ctk.CTkLabel(
                title_row, text="default", text_color=WP.ON_ACCENT,
                fg_color=WP.ACCENT, corner_radius=WP.RADIUS_PILL,
                font=(WP.FONT_FAMILY, WP.SIZE_TINY), padx=10, pady=1).pack(
                side="left", padx=(8, 0))
        ctk.CTkLabel(
            inner, text=tagline, text_color=WP.TEXT_MUTED, anchor="w",
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL)).pack(fill="x", pady=(0, 6))

        for icon, label, value in (
            ("$", "Cost", cost),
            ("⏱", "Speed", speed),
            ("◎", "Accuracy", accuracy),
        ):
            r = ctk.CTkFrame(inner, fg_color="transparent")
            r.pack(fill="x", pady=1)
            ctk.CTkLabel(
                r, text=f"{label}", text_color=WP.ACCENT, anchor="w",
                width=72, font=(WP.FONT_FAMILY, WP.SIZE_SMALL, "bold")).pack(
                side="left")
            ctk.CTkLabel(
                r, text=value, text_color=WP.TEXT, anchor="w",
                justify="left", wraplength=420,
                font=(WP.FONT_FAMILY, WP.SIZE_SMALL)).pack(
                side="left", fill="x", expand=True)


# ---------------------------------------------------------------------------
# Getting Started dialog (startup)
# ---------------------------------------------------------------------------

class GettingStartedDialog(ctk.CTkToplevel):
    """
    A friendly startup guide: how to use the app in a few steps, plus a
    clear Claude-vs-Ollama comparison so the user picks the right engine.
    Has a "Do not show this again" checkbox and a "Good to go!" button.
    """

    _STEPS = [
        ("1", "Add your flyers",
         "Drag PDFs or images onto the drop zone, or click to browse. "
         "Queue as many as you want."),
        ("2", "Pick survey type & engine",
         "Choose Building or Land, then the extraction engine (see below). "
         "Optionally pick a Field Hints preset for a broker's shorthand."),
        ("3", "Extract & review",
         "Click Extract. With “Review before saving” on, you can check and "
         "fix each flyer's data next to the source page."),
        ("4", "Get your Excel file",
         "Approved data is written into the KBC survey template. Use "
         "“View Exported Files” to open your output folder."),
    ]

    def __init__(self, parent):
        super().__init__(parent)
        self.dont_show_again = False
        self.title("Getting Started — KBC Flyer Reader")
        self.configure(fg_color=WP.PAGE_BG)
        try:
            if _ICON_PATH.exists():
                self.iconbitmap(str(_ICON_PATH))
        except Exception:
            pass
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(640, int(sw*0.94))}x{min(700, int(sh*0.94))}")
        self.minsize(480, 480)

        # Header.
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=22, pady=(18, 2))
        ctk.CTkLabel(
            head, text="Welcome to KBC Flyer Reader",
            font=(WP.FONT_FAMILY, WP.SIZE_TITLE, "bold"),
            text_color=WP.TEXT, anchor="center").pack(fill="x")
        ctk.CTkLabel(
            head,
            text="Turn property flyers into filled-in survey spreadsheets.",
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL), text_color=WP.TEXT_MUTED,
            anchor="center").pack(fill="x")

        # Scrollable body.
        body = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color=WP.BORDER_STRONG,
            scrollbar_button_hover_color=WP.ACCENT)
        body.pack(fill="both", expand=True, padx=16, pady=(8, 6))

        # Steps.
        ctk.CTkLabel(
            body, text="How it works", text_color=WP.TEXT, anchor="w",
            font=(WP.FONT_FAMILY, WP.SIZE_HEADING, "bold")).pack(
            fill="x", pady=(2, 4))
        for badge, title, blurb in self._STEPS:
            self._step(body, badge, title, blurb)

        # Engine comparison.
        ctk.CTkLabel(
            body, text="Which engine should I use?", text_color=WP.TEXT,
            anchor="w", font=(WP.FONT_FAMILY, WP.SIZE_HEADING, "bold")).pack(
            fill="x", pady=(14, 4))

        self._engine_card(
            body, "Claude  (cloud)", WP.ACCENT,
            [
                ("Speed", "Fast — usually under a minute per flyer."),
                ("Accuracy", "High — best with messy scans and multi-building flyers."),
                ("Cost", "Paid per run — roughly ~30¢ per flyer on average "
                         "(varies with flyer length and current pricing)."),
                ("Where it runs", "On Claude's servers — adds no load to your "
                                  "laptop, so your computer's speed doesn't matter."),
                ("Confidential data", "Avoid for confidential information — flyer "
                                      "text is processed on Claude's servers. Only "
                                      "use it for confidential data if you're on a "
                                      "Claude Enterprise API key."),
                ("Setup", "Requires an API key — paste it into Settings "
                          "(gear button) before your first run."),
            ])

        self._engine_card(
            body, "Ollama  (local)", WP.OK_GREEN,
            [
                ("Where it runs", "Entirely on your own computer — it loads the "
                                  "AI model onto your laptop's hardware, so its "
                                  "speed and capability are limited by that machine."),
                ("Speed", "Slower — about 5–15 minutes per flyer on a KBC Laptop."),
                ("Accuracy", "Lower — fine for simple flyers, less reliable on "
                             "complex ones."),
                ("Cost", "Free — no per-run charges."),
                ("Confidential data", "Best choice for confidential information — "
                                      "nothing leaves your computer."),
            ])

        ctk.CTkLabel(
            body,
            text=("In short: use Claude for everyday public flyers when you want "
                  "speed and accuracy; use Ollama when the material is "
                  "confidential and must stay on your machine."),
            text_color=WP.TEXT_MUTED, font=(WP.FONT_FAMILY, WP.SIZE_SMALL),
            anchor="w", justify="left", wraplength=560).pack(
            fill="x", pady=(10, 2), padx=2)

        # Footer: checkbox (left) + Good to go (right).
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=22, pady=(2, 16))
        self._dont_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            foot, text="Do not show this again", variable=self._dont_var,
            fg_color=WP.ACCENT, hover_color=WP.ACCENT_HOVER,
            text_color=WP.TEXT, font=(WP.FONT_FAMILY, WP.SIZE_SMALL),
            checkbox_width=20, checkbox_height=20, corner_radius=6,
        ).pack(side="left")
        _primary_btn(foot, "Good to go!", self._close, width=140).pack(
            side="right")

        self.transient(parent)
        self.after(120, self.grab_set)
        # Treat the window-close [X] the same as the button.
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _step(self, parent, badge, title, blurb):
        card = ctk.CTkFrame(
            parent, fg_color=WP.PANEL_BG, corner_radius=WP.RADIUS,
            border_color=WP.BORDER, border_width=1)
        card.pack(fill="x", pady=3)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=10)
        b = ctk.CTkFrame(row, width=32, height=32, corner_radius=16,
                         fg_color=WP.ACCENT)
        b.pack(side="left", padx=(0, 12))
        b.pack_propagate(False)
        ctk.CTkLabel(b, text=badge, text_color=WP.ON_ACCENT,
                     font=(WP.FONT_FAMILY, WP.SIZE_SMALL, "bold")).pack(
            expand=True)
        col = ctk.CTkFrame(row, fg_color="transparent")
        col.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(col, text=title, text_color=WP.TEXT, anchor="w",
                     font=(WP.FONT_FAMILY, WP.SIZE_BODY, "bold")).pack(
            fill="x")
        ctk.CTkLabel(col, text=blurb, text_color=WP.TEXT_MUTED, anchor="w",
                     justify="left", wraplength=500,
                     font=(WP.FONT_FAMILY, WP.SIZE_SMALL)).pack(fill="x")

    def _engine_card(self, parent, title, accent, rows):
        card = ctk.CTkFrame(
            parent, fg_color=WP.PANEL_BG, corner_radius=WP.RADIUS,
            border_color=accent, border_width=2)
        card.pack(fill="x", pady=3)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=12)
        ctk.CTkLabel(inner, text=title, text_color=accent, anchor="w",
                     font=(WP.FONT_FAMILY, WP.SIZE_BODY, "bold")).pack(
            fill="x", pady=(0, 4))
        for label, value in rows:
            r = ctk.CTkFrame(inner, fg_color="transparent")
            r.pack(fill="x", pady=1)
            ctk.CTkLabel(
                r, text=label, text_color=WP.TEXT, anchor="nw", width=120,
                font=(WP.FONT_FAMILY, WP.SIZE_SMALL, "bold")).pack(side="left")
            ctk.CTkLabel(
                r, text=value, text_color=WP.TEXT_MUTED, anchor="w",
                justify="left", wraplength=400,
                font=(WP.FONT_FAMILY, WP.SIZE_SMALL)).pack(
                side="left", fill="x", expand=True)

    def _close(self):
        self.dont_show_again = bool(self._dont_var.get())
        self.destroy()


# ---------------------------------------------------------------------------
# Field Hints manager dialog
# ---------------------------------------------------------------------------

class FieldHintsDialog(ctk.CTkToplevel):
    """
    Manage broker-shorthand presets. Left: list of presets (+ add/delete).
    Right: the selected preset's name and its shorthand→meaning rows, which
    the user can add to, edit, and remove. Saves on "Save & Close".
    """

    def __init__(self, parent, hints: FieldHints):
        super().__init__(parent)
        self.hints = hints
        self.title("Field Hints — broker shorthand")
        self.configure(fg_color=WP.PAGE_BG)
        try:
            if _ICON_PATH.exists():
                self.iconbitmap(str(_ICON_PATH))
        except Exception:
            pass
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(900, int(sw*0.94))}x{min(580, int(sh*0.92))}")
        self.minsize(720, 440)

        self._current: int | None = None  # index into hints.presets
        # (frame, sh_var, mean_var, col_var)
        self._row_widgets: list[tuple] = []

        # Column catalog for the "Column" menu: human label <-> field name.
        from .excel_writer import all_template_columns
        self._COL_NONE = "(none)"
        try:
            cols = all_template_columns()
        except Exception:
            cols = []
        self._col_labels = [self._COL_NONE] + [label for label, _f in cols]
        self._label_to_field = {label: f for label, f in cols}
        self._field_to_label = {f: label for label, f in cols}

        # Header.
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(16, 4))
        ctk.CTkLabel(
            head, text="Field Hints",
            font=(WP.FONT_FAMILY, WP.SIZE_TITLE, "bold"),
            text_color=WP.TEXT).pack(anchor="w")
        ctk.CTkLabel(
            head,
            text=("Teach the app a broker's shorthand once, then pick the "
                  "preset when extracting. Example — shorthand “DH 9'x10' "
                  "(60)” → meaning “60 dock-high doors, 9'×10' (dock_doors)”."),
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL), text_color=WP.TEXT_MUTED,
            justify="left", wraplength=700, anchor="w").pack(anchor="w")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(8, 8))

        # Left: preset list.
        left = _card(body)
        left.pack(side="left", fill="y", padx=(0, 12))
        _caption(left, "Presets", anchor="w").pack(anchor="w", padx=12, pady=(10, 4))
        self.preset_list = tk.Listbox(
            left, width=22, height=12, bg=WP.PANEL_BG, fg=WP.TEXT,
            selectbackground=WP.ACCENT_SOFT, selectforeground=WP.TEXT,
            highlightthickness=0, borderwidth=0, activestyle="none",
            exportselection=False, font=(WP.FONT_FAMILY, WP.SIZE_SMALL))
        self.preset_list.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self.preset_list.bind("<<ListboxSelect>>", self._on_select_preset)
        btns = ctk.CTkFrame(left, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(0, 12))
        _ghost_btn(btns, "+ Add", self._add_preset, width=80).pack(side="left")
        _danger_btn(btns, "Delete", self._delete_preset, width=80).pack(
            side="right")

        # Right: selected preset editor.
        right = _card(body)
        right.pack(side="left", fill="both", expand=True)
        name_row = ctk.CTkFrame(right, fg_color="transparent")
        name_row.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(name_row, text="Name", text_color=WP.TEXT, width=48,
                     anchor="w", font=(WP.FONT_FAMILY, WP.SIZE_SMALL)).pack(
            side="left")
        self.name_var = tk.StringVar()
        self.name_entry = ctk.CTkEntry(
            name_row, textvariable=self.name_var, fg_color=WP.PANEL_BG,
            border_color=WP.BORDER, text_color=WP.TEXT,
            corner_radius=WP.RADIUS_SM, height=30,
            placeholder_text="e.g. CBRE – Phoenix")
        self.name_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.name_var.trace_add("write", lambda *_: self._on_name_edit())

        hdr = ctk.CTkFrame(right, fg_color="transparent")
        hdr.pack(fill="x", padx=12, pady=(6, 0))
        ctk.CTkLabel(hdr, text="Shorthand", text_color=WP.TEXT_MUTED,
                     width=110, anchor="w",
                     font=(WP.FONT_FAMILY, WP.SIZE_TINY)).pack(side="left")
        ctk.CTkLabel(hdr, text="Means…", text_color=WP.TEXT_MUTED, anchor="w",
                     font=(WP.FONT_FAMILY, WP.SIZE_TINY)).pack(
            side="left", padx=(6, 0))
        ctk.CTkLabel(hdr, text="Column", text_color=WP.TEXT_MUTED, width=170,
                     anchor="w", font=(WP.FONT_FAMILY, WP.SIZE_TINY)).pack(
            side="right", padx=(6, 40))

        self.rows_frame = ctk.CTkScrollableFrame(
            right, fg_color="transparent",
            scrollbar_button_color=WP.BORDER_STRONG,
            scrollbar_button_hover_color=WP.ACCENT)
        self.rows_frame.pack(fill="both", expand=True, padx=8, pady=(2, 6))
        _ghost_btn(right, "+ Add mapping", self._add_row, width=130).pack(
            anchor="w", padx=12, pady=(0, 12))

        # Footer.
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=18, pady=(0, 16))
        _ghost_btn(foot, "Cancel", self.destroy).pack(side="right", padx=(8, 0))
        _primary_btn(foot, "Save & Close", self._save_close, width=140).pack(
            side="right")

        self._reload_preset_list()
        if self.hints.presets:
            self.preset_list.selection_set(0)
            self._load_preset(0)
        else:
            self._set_editor_enabled(False)

        self.transient(parent)
        self.after(120, self.grab_set)

    # ----- preset list ------------------------------------------------------

    def _reload_preset_list(self):
        self.preset_list.delete(0, "end")
        for p in self.hints.presets:
            self.preset_list.insert("end", p.name or "(unnamed)")

    def _on_select_preset(self, _e=None):
        sel = self.preset_list.curselection()
        if not sel:
            return
        self._commit_current()
        self._load_preset(sel[0])

    def _add_preset(self):
        from .field_hints import HintPreset
        self._commit_current()
        self.hints.presets.append(HintPreset(name="New preset", mappings=[]))
        self._reload_preset_list()
        idx = len(self.hints.presets) - 1
        self.preset_list.selection_clear(0, "end")
        self.preset_list.selection_set(idx)
        self._load_preset(idx)
        self.name_entry.focus_set()

    def _delete_preset(self):
        if self._current is None:
            return
        name = self.hints.presets[self._current].name or "(unnamed)"
        if not messagebox.askyesno(
                "Delete preset", f"Delete the preset “{name}”?", parent=self):
            return
        del self.hints.presets[self._current]
        self._current = None
        self._reload_preset_list()
        if self.hints.presets:
            self.preset_list.selection_set(0)
            self._load_preset(0)
        else:
            self.name_var.set("")
            self._clear_rows()
            self._set_editor_enabled(False)

    # ----- editor -----------------------------------------------------------

    def _load_preset(self, idx: int):
        self._current = idx
        p = self.hints.presets[idx]
        self._set_editor_enabled(True)
        self._loading = True
        self.name_var.set(p.name)
        self._loading = False
        self._clear_rows()
        for m in p.mappings:
            self._add_row(m.shorthand, m.meaning, getattr(m, "column", ""))
        if not p.mappings:
            self._add_row()

    def _on_name_edit(self):
        if getattr(self, "_loading", False) or self._current is None:
            return
        self.hints.presets[self._current].name = self.name_var.get()
        # Live-update the list label.
        self.preset_list.delete(self._current)
        self.preset_list.insert(self._current, self.name_var.get() or "(unnamed)")
        self.preset_list.selection_set(self._current)

    def _clear_rows(self):
        for frame, _sh, _mn, _col in self._row_widgets:
            frame.destroy()
        self._row_widgets = []

    def _add_row(self, shorthand: str = "", meaning: str = "", column: str = ""):
        row = ctk.CTkFrame(self.rows_frame, fg_color="transparent")
        row.pack(fill="x", pady=2)
        sh_var = tk.StringVar(value=shorthand)
        mn_var = tk.StringVar(value=meaning)
        # Column var holds the human label; map to/from field name.
        col_label = self._field_to_label.get(column, self._COL_NONE) if column \
            else self._COL_NONE
        col_var = tk.StringVar(value=col_label)
        ctk.CTkEntry(
            row, textvariable=sh_var, width=100, fg_color=WP.PANEL_BG,
            border_color=WP.BORDER, text_color=WP.TEXT,
            corner_radius=WP.RADIUS_SM, height=30,
            placeholder_text="DH").pack(side="left")
        ctk.CTkEntry(
            row, textvariable=mn_var, fg_color=WP.PANEL_BG,
            border_color=WP.BORDER, text_color=WP.TEXT,
            corner_radius=WP.RADIUS_SM, height=30,
            placeholder_text="dock-high doors, 9'x10'").pack(
            side="left", fill="x", expand=True, padx=(6, 6))
        col_menu = ctk.CTkOptionMenu(
            row, variable=col_var, values=self._col_labels,
            fg_color=WP.PANEL_BG, button_color=WP.ACCENT,
            button_hover_color=WP.ACCENT_HOVER, text_color=WP.TEXT,
            corner_radius=WP.RADIUS_SM, width=170, dynamic_resizing=False)
        col_menu.pack(side="left", padx=(0, 6))
        entry = (row, sh_var, mn_var, col_var)

        def _remove():
            row.destroy()
            if entry in self._row_widgets:
                self._row_widgets.remove(entry)

        _danger_btn(row, "✕", _remove, width=34).pack(side="right")
        self._row_widgets.append(entry)

    def _set_editor_enabled(self, on: bool):
        state = "normal" if on else "disabled"
        try:
            self.name_entry.configure(state=state)
        except Exception:
            pass

    # ----- commit / save ----------------------------------------------------

    def _commit_current(self):
        """Write the editor's rows back into the in-memory preset."""
        if self._current is None:
            return
        from .field_hints import HintMapping
        p = self.hints.presets[self._current]
        p.name = self.name_var.get()
        mappings = []
        for _f, sh, mn, col in self._row_widgets:
            label = col.get()
            field = self._label_to_field.get(label, "") \
                if label != self._COL_NONE else ""
            mappings.append(HintMapping(sh.get(), mn.get(), field))
        p.mappings = mappings

    def _save_close(self):
        self._commit_current()
        # Drop fully-empty presets and blank rows, then persist.
        cleaned = []
        for p in self.hints.presets:
            cp = p.cleaned()
            if cp.name or cp.mappings:
                cleaned.append(cp)
        self.hints.presets = cleaned
        self.hints.save()
        self.destroy()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class FlyerReaderApp:
    _OLLAMA_MODEL_CHOICES: list[tuple[str, str]] = [
        ("qwen2.5:3b (fast, default)",        "qwen2.5:3b"),
        ("qwen2.5:7b (more accurate, slow)",  "qwen2.5:7b"),
        ("llama3.2:3b (alt 3B)",              "llama3.2:3b"),
        ("llama3.1:8b (most accurate, slow)", "llama3.1:8b"),
        ("qwen2.5:1.5b (lightest)",           "qwen2.5:1.5b"),
    ]
    _CLAUDE_MODEL_CHOICES: list[tuple[str, str]] = [
        ("Claude Sonnet 4.6 (balanced, default)", "claude-sonnet-4-6"),
        ("Claude Haiku 4.5 (fastest, cheapest)",  "claude-haiku-4-5-20251001"),
        ("Claude Opus 4.7 (most accurate)",       "claude-opus-4-7"),
    ]

    # Baseline window size. The real minimum is computed from content in
    # _recompute_minsize() so nothing is ever clipped, and the window is
    # capped to the screen in __init__ so it always fits the display.
    _WIN_W = 860
    _WIN_H_COLLAPSED = 720
    _WIN_H_EXPANDED = 980
    # Minimum visible heights (px) for the flexible sections so they
    # shrink gracefully but never vanish.
    _DROP_MIN_H = 72
    # The instructions box is a FIXED height — it does not grow or shrink
    # with the window. _INSTR_BOX_H is the textbox itself (about 3 lines);
    # _INSTR_ROW_H is the whole row including the caption and padding.
    _INSTR_BOX_H = 66
    _INSTR_ROW_H = 118
    _FILES_MIN_LINES = 2
    _LOG_MIN_H = 130
    # Enforced window minimum (log hidden). Chosen so every fixed row plus
    # the flexible floors fit with no clipping; verified by screenshot.
    _MIN_W = 720
    # Field-hints picker sentinel for "no preset selected".
    _PRESET_NONE = "(none)"
    _MIN_H_BASE = 700

    def __init__(self):
        self.cfg = Config.load()
        self.field_hints = FieldHints.load()
        self.files: list[Path] = []
        self.log_queue: "queue.Queue[str]" = queue.Queue()

        # On Windows, the taskbar groups windows by an "Application User
        # Model ID". A bare pythonw process inherits Python's identity, so
        # the taskbar shows the Python icon. Setting an explicit AppID
        # before any window is created makes Windows treat this as its own
        # app and use the window icon we set below in the taskbar too.
        self._set_windows_app_id()

        apply_ctk_appearance(ctk)
        self.root = _RootWindow()
        self.root.title("KBC Flyer Reader")
        self.root.configure(fg_color=WP.PAGE_BG)
        # ttk theming for any residual ttk widgets (the review dialog uses
        # ttk.Treeview etc.); harmless to install here.
        style_ttk(self.root)

        # Window icon (titlebar + taskbar). Guarded — iconbitmap only takes
        # .ico on Windows; on other platforms it can raise, which is fine.
        self._apply_window_icon(self.root)

        # Size the window to fit the user's screen: never open larger than
        # ~92% of the available screen, so it's fully visible on small
        # laptops. The enforced minimum is set later from real content.
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(self._WIN_W, int(sw * 0.92))
        h = min(self._WIN_H_COLLAPSED, int(sh * 0.92))
        self.root.geometry(f"{w}x{h}")

        # Animation / run state.
        self._spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._spinner_idx = 0
        self._running = False
        self._start_time: Optional[float] = None
        self._stop_event: Optional[threading.Event] = None
        # Per-batch stage tracking (drives the big stage banner).
        self._batch_total = 0
        self._batch_current = 0
        self._run_saw_success = False
        self._run_saw_fatal = False
        self._run_fatal_detail = ""
        self._run_success_count: Optional[int] = None
        self._run_success_file = ""
        self._log_visible = False

        self._build_ui()
        self._poll_log()
        # Compute a real minimum window size from the laid-out content so
        # every component stays visible no matter how small the user drags
        # the window.
        self.root.after(60, self._recompute_minsize)

        # On startup, show the Getting Started guide (until dismissed
        # permanently). It covers the Claude/privacy guidance too, so when it
        # is shown we don't also fire the separate privacy popup.
        if self.cfg.show_getting_started:
            self.root.after(150, self._show_getting_started)
        elif self.cfg.provider == "claude" and not self.cfg.claude_privacy_ack:
            self.root.after(150, self._show_claude_privacy_warning_once)

        # Check GitHub for a newer release in the background; prompt only if
        # one is found. Fully non-blocking and silent on any failure.
        self.root.after(800, self._start_update_check)

    def _start_update_check(self):
        """Run the update check on a worker thread, then prompt on the UI."""
        def worker():
            try:
                from .update_checker import check_for_update
                info = check_for_update()
            except Exception:
                info = None
            if info is not None:
                # Hand back to the Tk main thread to show the dialog.
                self.root.after(0, lambda: self._prompt_update(info))
        threading.Thread(target=worker, daemon=True).start()

    def _prompt_update(self, info):
        """Tell the user an update is available and offer to open it."""
        import webbrowser
        msg = (
            f"A new version of KBC Flyer Reader is available.\n\n"
            f"   Installed:  {info.current}\n"
            f"   Latest:     {info.latest}\n\n"
            f"Would you like to open the download page to update now?"
        )
        try:
            if messagebox.askyesno("Update available", msg, parent=self.root):
                webbrowser.open(info.url)
        except Exception:
            pass

    def _set_windows_app_id(self) -> None:
        """
        Tell Windows this is its own application (not Python) so the
        taskbar uses our window icon. No-op / harmless on other OSes.
        """
        try:
            if sys.platform.startswith("win"):
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "KBC.FlyerReader.App")
        except Exception:
            pass

    def _apply_window_icon(self, window) -> None:
        """
        Set the book icon on a window. iconbitmap(.ico) drives the Windows
        title bar and taskbar; we also set iconphoto from the same .ico via
        Pillow as a cross-platform fallback (and to be doubly sure the
        taskbar picks it up). A reference to the PhotoImage is kept on the
        instance so Tk doesn't garbage-collect it.
        """
        if not _ICON_PATH.exists():
            return
        try:
            window.iconbitmap(str(_ICON_PATH))
        except Exception:
            pass
        try:
            from PIL import Image, ImageTk
            if not hasattr(self, "_icon_photo"):
                img = Image.open(str(_ICON_PATH))
                self._icon_photo = ImageTk.PhotoImage(img)
            window.iconphoto(True, self._icon_photo)
        except Exception:
            pass

    def _recompute_minsize(self):
        """
        Enforce a minimum window size so every component stays visible no
        matter how small the user drags the window: at the minimum, the
        flexible areas (drop zone, file list, instructions) sit at their
        floors and the window simply can't go smaller.

        We use fixed minimums rather than measuring, because CustomTkinter
        frames report a large default requested height that doesn't reflect
        their actual content and would massively over-estimate the floor.
        The minimum is capped to the screen so it always fits the display.
        """
        base = self._MIN_H_BASE
        if self._log_visible:
            base += self._LOG_MIN_H + 12
        sh = self.root.winfo_screenheight()
        h = min(base, max(420, int(sh * 0.95)))
        try:
            self.root.minsize(self._MIN_W, h)
        except Exception:
            pass

    # ----- Provider / model selectors --------------------------------------

    _CLAUDE_PRIVACY_NOTICE = (
        "Note: When using a Claude model, please do not upload confidential "
        "information. The attachments will be shared with Claude. The Claude "
        "model is mainly for analyzing PUBLIC information, including public "
        "flyers, site plans, etc."
    )

    def _show_claude_privacy_warning(self):
        messagebox.showwarning("Claude API — privacy notice",
                               self._CLAUDE_PRIVACY_NOTICE,
                               parent=self.root)

    def _show_claude_privacy_warning_once(self):
        """Launch-time notice: show, then remember it was acknowledged so it
        doesn't reappear on every startup."""
        self._show_claude_privacy_warning()
        if not self.cfg.claude_privacy_ack:
            self.cfg.claude_privacy_ack = True
            self.cfg.save()

    def _current_model_choices(self) -> list[tuple[str, str]]:
        is_claude = self.provider_var.get().startswith("Claude")
        return self._CLAUDE_MODEL_CHOICES if is_claude else self._OLLAMA_MODEL_CHOICES

    def _refresh_model_choices(self):
        choices = self._current_model_choices()
        labels = [label for label, _ in choices]
        self.model_cb.configure(values=labels)
        is_claude = self.provider_var.get().startswith("Claude")
        active = self.cfg.claude_model if is_claude else self.cfg.ollama_model
        match = next((lbl for lbl, m in choices if m == active), None)
        if match is None:
            match = labels[0]
            if is_claude:
                self.cfg.claude_model = choices[0][1]
            else:
                self.cfg.ollama_model = choices[0][1]
            self.cfg.save()
        self.model_var.set(match)

    def _on_provider_change(self, *, initial: bool = False):
        is_claude = self.provider_var.get().startswith("Claude")
        new_provider = "claude" if is_claude else "ollama"
        crossing_into_claude = (new_provider == "claude"
                                and self.cfg.provider != "claude")
        if new_provider != self.cfg.provider:
            self.cfg.provider = new_provider
            self.cfg.save()
            if not initial:
                self._log(f"Switched extraction provider to "
                          f"{'Claude API' if is_claude else 'local Ollama'}.",
                          "stage")
        self._refresh_model_choices()
        if crossing_into_claude and not initial:
            self._show_claude_privacy_warning()

    def _on_model_change(self):
        choices = self._current_model_choices()
        label = self.model_var.get()
        model = next((m for lbl, m in choices if lbl == label), None)
        if model is None:
            return
        is_claude = self.provider_var.get().startswith("Claude")
        if is_claude:
            self.cfg.claude_model = model
        else:
            self.cfg.ollama_model = model
        self.cfg.save()
        self._log(f"Model set to {model}.", "info")

    def _on_survey_change(self, value: str):
        """Segmented control switched Building/Land."""
        self.survey_var.set(value.strip().lower())

    # ----- UI construction --------------------------------------------------

    # Grid row indices for the main content (see _build_ui).
    _ROW_HEADER = 0
    _ROW_SELECT = 1
    _ROW_DROP = 2
    _ROW_FILES = 3
    _ROW_INSTR = 4
    _ROW_OUTPUT = 5
    _ROW_ACTION = 6
    _ROW_STAGE = 7
    _ROW_LOGBAR = 8
    _ROW_LOG = 9

    def _build_ui(self):
        outer = ctk.CTkFrame(self.root, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=16, pady=14)
        self._outer = outer
        outer.columnconfigure(0, weight=1)
        # Flexible rows grow with the window and bottom out at a minimum
        # so they stay visible when the window is small. Fixed rows keep
        # their natural height. The drop zone and file list share the
        # stretch; the log (row 9) takes the most when shown. The
        # instructions row is deliberately FIXED (weight 0) so its box
        # keeps a constant height and never shrinks vertically.
        outer.rowconfigure(self._ROW_DROP, weight=3, minsize=self._DROP_MIN_H)
        outer.rowconfigure(self._ROW_FILES, weight=4, minsize=92)
        outer.rowconfigure(self._ROW_INSTR, weight=0, minsize=self._INSTR_ROW_H)
        outer.rowconfigure(self._ROW_LOG, weight=5)

        # --- Header --------------------------------------------------------
        header = ctk.CTkFrame(outer, fg_color="transparent")
        header.grid(row=self._ROW_HEADER, column=0, sticky="ew")
        ctk.CTkLabel(
            header, text="KBC Flyer Reader",
            font=(WP.FONT_FAMILY, WP.SIZE_TITLE, "bold"),
            text_color=WP.TEXT,
        ).pack(side="left")
        # Right side, packed right-to-left: Settings, then "?", then
        # "View Exported Files" (so the order on screen is
        # View Exported Files · ? · Settings).
        _ghost_btn(header, "⚙  Settings", self._open_settings).pack(side="right")
        help_btn = _ghost_btn(header, "?", self._open_help, width=36)
        help_btn.configure(font=(WP.FONT_FAMILY, WP.SIZE_BODY, "bold"))
        help_btn.pack(side="right", padx=(0, 8))
        _ghost_btn(
            header, "📂  View Exported Files", self._open_output_folder
        ).pack(side="right", padx=(0, 8))

        # --- Selector row: Survey type + Engine ---------------------------
        sel = ctk.CTkFrame(outer, fg_color="transparent")
        sel.grid(row=self._ROW_SELECT, column=0, sticky="ew", pady=(12, 6))

        survey_card = _card(sel)
        survey_card.pack(side="left", fill="y", padx=(0, 10))
        _caption(survey_card, "Survey type", anchor="w").pack(
            anchor="w", padx=14, pady=(10, 2))
        self.survey_var = tk.StringVar(value="building")
        self.survey_seg = ctk.CTkSegmentedButton(
            survey_card, values=["Building", "Land"],
            command=self._on_survey_change,
            fg_color=WP.SECONDARY_BG, selected_color=WP.ACCENT,
            selected_hover_color=WP.ACCENT_HOVER,
            unselected_color=WP.SECONDARY_BG,
            unselected_hover_color=WP.BORDER,
            text_color=WP.TEXT, corner_radius=WP.RADIUS_PILL,
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL),
        )
        self.survey_seg.set("Building")
        self.survey_seg.pack(padx=14, pady=(0, 12))

        engine_card = _card(sel)
        engine_card.pack(side="left", fill="both", expand=True)
        _caption(engine_card, "Extraction engine", anchor="w").pack(
            anchor="w", padx=14, pady=(10, 2))
        engine_row = ctk.CTkFrame(engine_card, fg_color="transparent")
        engine_row.pack(fill="x", padx=14, pady=(0, 12))

        self.provider_var = tk.StringVar(
            value="Claude API" if self.cfg.provider == "claude" else "Local (Ollama)")
        ctk.CTkOptionMenu(
            engine_row, variable=self.provider_var,
            values=["Local (Ollama)", "Claude API"],
            command=lambda _v: self._on_provider_change(),
            fg_color=WP.PANEL_BG, button_color=WP.ACCENT,
            button_hover_color=WP.ACCENT_HOVER, text_color=WP.TEXT,
            corner_radius=WP.RADIUS_SM, width=150,
        ).pack(side="left", padx=(0, 10))

        self.model_var = tk.StringVar()
        self.model_cb = ctk.CTkOptionMenu(
            engine_row, variable=self.model_var, values=[],
            command=lambda _v: self._on_model_change(),
            fg_color=WP.PANEL_BG, button_color=WP.ACCENT,
            button_hover_color=WP.ACCENT_HOVER, text_color=WP.TEXT,
            corner_radius=WP.RADIUS_SM, width=240,
        )
        self.model_cb.pack(side="left", fill="x", expand=True)

        self._refresh_model_choices()
        self._on_provider_change(initial=True)

        # --- Field hints (broker shorthand preset) ------------------------
        hints_card = _card(sel)
        hints_card.pack(side="left", fill="y", padx=(10, 0))
        _caption(hints_card, "Field hints", anchor="w").pack(
            anchor="w", padx=14, pady=(10, 2))
        hints_row = ctk.CTkFrame(hints_card, fg_color="transparent")
        hints_row.pack(fill="x", padx=14, pady=(0, 12))
        self.preset_var = tk.StringVar(value=self._PRESET_NONE)
        self.preset_menu = ctk.CTkOptionMenu(
            hints_row, variable=self.preset_var,
            values=[self._PRESET_NONE],
            command=lambda _v: None,
            fg_color=WP.PANEL_BG, button_color=WP.ACCENT,
            button_hover_color=WP.ACCENT_HOVER, text_color=WP.TEXT,
            corner_radius=WP.RADIUS_SM, width=170,
        )
        self.preset_menu.pack(side="left", padx=(0, 8))
        _ghost_btn(hints_row, "Manage…", self._open_field_hints,
                   width=86).pack(side="left")
        self._refresh_preset_choices()

        # --- Drop zone (flexible) -----------------------------------------
        self.drop_zone = ctk.CTkFrame(
            outer, fg_color=WP.PANEL_BG, corner_radius=WP.RADIUS,
            border_color="#e3c9a8", border_width=2)
        self.drop_zone.grid(row=self._ROW_DROP, column=0, sticky="nsew",
                            pady=(0, 8))
        self._dz_inner = ctk.CTkFrame(self.drop_zone, fg_color="transparent")
        self._dz_inner.place(relx=0.5, rely=0.5, anchor="center")
        self._dz_arrow = ctk.CTkLabel(
            self._dz_inner, text="⬆", text_color=WP.ACCENT,
            font=(WP.FONT_FAMILY, 22))
        self._dz_main = ctk.CTkLabel(
            self._dz_inner,
            text=("Drop your flyers here" if _DND_AVAILABLE
                  else "Click to browse for flyers"),
            text_color=WP.TEXT, font=(WP.FONT_FAMILY, WP.SIZE_BODY))
        self._dz_hint = ctk.CTkLabel(
            self._dz_inner,
            text=("PDF, PNG, JPG, TIFF  —  or click to browse" if _DND_AVAILABLE
                  else "install 'tkinterdnd2' to enable drag-and-drop"),
            text_color=WP.TEXT_FAINT, font=(WP.FONT_FAMILY, WP.SIZE_TINY))
        # Initial layout shows everything; _on_dropzone_resize trims it to
        # fit when the box is short so nothing overflows the border.
        self._dz_level = None
        self._dz_resize_after = None
        self._layout_dropzone("full")
        self.drop_zone.bind("<Configure>", self._on_dropzone_resize)

        for w in (self.drop_zone, self._dz_inner,
                  self._dz_arrow, self._dz_main, self._dz_hint):
            w.bind("<Button-1>", lambda e: self._browse())
        if _DND_AVAILABLE and hasattr(self.drop_zone, "drop_target_register"):
            try:
                self.drop_zone.drop_target_register(DND_FILES)
                self.drop_zone.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

        # --- Files queued (flexible) --------------------------------------
        queue_card = _card(outer)
        queue_card.grid(row=self._ROW_FILES, column=0, sticky="nsew",
                        pady=(0, 8))
        _caption(queue_card, "Files queued", anchor="w").pack(
            anchor="w", padx=14, pady=(10, 4))
        list_wrap = ctk.CTkFrame(queue_card, fg_color="transparent")
        list_wrap.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.files_list = tk.Listbox(
            list_wrap, height=self._FILES_MIN_LINES, bg=WP.PANEL_BG, fg=WP.TEXT,
            selectbackground=WP.ACCENT_SOFT, selectforeground=WP.TEXT,
            highlightthickness=0, borderwidth=0, activestyle="none",
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL))
        self.files_list.pack(side="left", fill="both", expand=True)
        files_sb = ctk.CTkScrollbar(list_wrap, command=self.files_list.yview)
        files_sb.pack(side="right", fill="y")
        self.files_list.config(yscrollcommand=files_sb.set)

        # --- Additional instructions (fixed height) -----------------------
        # This row is fixed (weight 0); the card fills the width and keeps a
        # constant height, and the textbox itself is a fixed height so it
        # never shrinks vertically as the window resizes.
        instr_card = _card(outer)
        instr_card.grid(row=self._ROW_INSTR, column=0, sticky="ew",
                        pady=(0, 8))
        _caption(
            instr_card,
            f"Additional instructions  (optional — max "
            f"{_EXTRA_INSTRUCTIONS_GUI_LIMIT} chars)", anchor="w").pack(
            anchor="w", padx=14, pady=(10, 4))
        self.instr_text = ctk.CTkTextbox(
            instr_card, height=self._INSTR_BOX_H, fg_color=WP.PANEL_BG,
            text_color=WP.TEXT, border_color=WP.BORDER, border_width=1,
            corner_radius=WP.RADIUS_SM,
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL), wrap="word")
        self.instr_text.pack(fill="x", expand=False, padx=14, pady=(0, 12))
        self._instr_placeholder = (
            'e.g. "Only extract building 2", or "Convert measurements to '
            'square meters", or leave blank for default extraction.'
        )
        self._instr_has_placeholder = True
        self.instr_text.insert("1.0", self._instr_placeholder)
        self.instr_text.configure(text_color=WP.TEXT_FAINT)
        self.instr_text.bind("<FocusIn>", self._on_instr_focus_in)
        self.instr_text.bind("<FocusOut>", self._on_instr_focus_out)

        # --- Output destination (fixed) -----------------------------------
        self._append_target_path: Optional[Path] = None
        out_card = _card(outer)
        out_card.grid(row=self._ROW_OUTPUT, column=0, sticky="ew", pady=(0, 8))
        out_top = ctk.CTkFrame(out_card, fg_color="transparent")
        out_top.pack(fill="x", padx=14, pady=(10, 2))
        ctk.CTkLabel(out_top, text="Output", text_color=WP.TEXT_MUTED,
                     font=(WP.FONT_FAMILY, WP.SIZE_TINY)).pack(side="left", padx=(0, 8))
        self._output_mode_var = tk.StringVar(value="new")
        ctk.CTkRadioButton(
            out_top, text="Create new file", variable=self._output_mode_var,
            value="new", command=self._on_output_mode_change,
            fg_color=WP.ACCENT, hover_color=WP.ACCENT_HOVER,
            text_color=WP.TEXT, font=(WP.FONT_FAMILY, WP.SIZE_SMALL),
            radiobutton_width=18, radiobutton_height=18,
        ).pack(side="left", padx=(0, 14))
        ctk.CTkRadioButton(
            out_top, text="Append to existing:", variable=self._output_mode_var,
            value="append", command=self._on_output_mode_change,
            fg_color=WP.ACCENT, hover_color=WP.ACCENT_HOVER,
            text_color=WP.TEXT, font=(WP.FONT_FAMILY, WP.SIZE_SMALL),
            radiobutton_width=18, radiobutton_height=18,
        ).pack(side="left", padx=(0, 8))
        self._append_target_var = tk.StringVar(value="(none selected)")
        self._append_target_label = ctk.CTkLabel(
            out_top, textvariable=self._append_target_var,
            text_color=WP.TEXT_MUTED, font=(WP.FONT_FAMILY, WP.SIZE_TINY),
            anchor="w")
        self._append_target_label.pack(side="left", fill="x", expand=True)
        self._browse_target_btn = _ghost_btn(
            out_top, "Browse...", self._browse_append_target, width=90)
        self._browse_target_btn.configure(state="disabled")
        self._browse_target_btn.pack(side="right")

        out_name = ctk.CTkFrame(out_card, fg_color="transparent")
        out_name.pack(fill="x", padx=14, pady=(2, 12))
        ctk.CTkLabel(out_name, text="Name", text_color=WP.TEXT,
                     font=(WP.FONT_FAMILY, WP.SIZE_SMALL), width=44,
                     anchor="w").pack(side="left")
        self._output_name_var = tk.StringVar(value="")
        self._output_name_entry = ctk.CTkEntry(
            out_name, textvariable=self._output_name_var, fg_color=WP.PANEL_BG,
            border_color=WP.BORDER, text_color=WP.TEXT,
            corner_radius=WP.RADIUS_SM, height=32,
            placeholder_text="leave blank for an automatic timestamped name")
        self._output_name_entry.pack(side="left", fill="x", expand=True,
                                     padx=(6, 8))
        ctk.CTkLabel(
            out_name, text=".xlsx added if missing",
            text_color=WP.TEXT_FAINT,
            font=(WP.FONT_FAMILY, WP.SIZE_TINY)).pack(side="left")

        # --- Action row (fixed) -------------------------------------------
        action = ctk.CTkFrame(outer, fg_color="transparent")
        action.grid(row=self._ROW_ACTION, column=0, sticky="ew", pady=(0, 8))
        _ghost_btn(action, "Clear queue", self._clear).pack(side="left")
        self.review_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            action, text="Review before saving", variable=self.review_var,
            fg_color=WP.ACCENT, hover_color=WP.ACCENT_HOVER,
            text_color=WP.TEXT, font=(WP.FONT_FAMILY, WP.SIZE_SMALL),
            checkbox_width=20, checkbox_height=20, corner_radius=6,
        ).pack(side="left", padx=(14, 0))
        self.extract_btn = _primary_btn(action, "Extract  →", self._extract,
                                        width=140)
        self.extract_btn.pack(side="right")
        self.stop_btn = _danger_btn(action, "■ Stop", self._request_stop,
                                    width=110)
        # (packed/unpacked dynamically in _extract / _on_finished)

        # --- Stage banner (fixed, always visible) -------------------------
        self.stage_frame = ctk.CTkFrame(
            outer, fg_color=WP.SECONDARY_BG, corner_radius=WP.RADIUS)
        self.stage_frame.grid(row=self._ROW_STAGE, column=0, sticky="ew",
                              pady=(0, 6))
        banner = ctk.CTkFrame(self.stage_frame, fg_color="transparent")
        banner.pack(fill="x", padx=16, pady=(12, 4))
        self.stage_icon = ctk.CTkLabel(
            banner, text="○", text_color=WP.TEXT_MUTED,
            font=(WP.FONT_FAMILY, 22), width=30)
        self.stage_icon.pack(side="left", padx=(0, 10))
        text_col = ctk.CTkFrame(banner, fg_color="transparent")
        text_col.pack(side="left", fill="x", expand=True)
        self.stage_title = ctk.CTkLabel(
            text_col, text="Ready", text_color=WP.TEXT, anchor="w",
            font=(WP.FONT_FAMILY, WP.SIZE_STAGE, "bold"))
        self.stage_title.pack(anchor="w", fill="x")
        self.stage_detail = ctk.CTkLabel(
            text_col, text="Add flyers and press Extract.",
            text_color=WP.TEXT_MUTED, anchor="w",
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL))
        self.stage_detail.pack(anchor="w", fill="x")
        self.elapsed_lbl = ctk.CTkLabel(
            banner, text="", text_color=WP.TEXT_MUTED,
            font=(WP.FONT_FAMILY, WP.SIZE_BODY), width=56)
        self.elapsed_lbl.pack(side="right")

        bar_row = ctk.CTkFrame(self.stage_frame, fg_color="transparent")
        bar_row.pack(fill="x", padx=16, pady=(0, 12))
        self.progress = ctk.CTkProgressBar(
            bar_row, progress_color=WP.ACCENT, fg_color=WP.PANEL_BG,
            corner_radius=WP.RADIUS_SM, height=12)
        self.progress.set(0)
        self.progress.pack(side="left", fill="x", expand=True)
        self.percent_lbl = ctk.CTkLabel(
            bar_row, text="", text_color=WP.TEXT_MUTED,
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL), width=48)
        self.percent_lbl.pack(side="right", padx=(10, 0))

        # --- Log toggle (fixed) + collapsible log (flexible) --------------
        log_bar = ctk.CTkFrame(outer, fg_color="transparent")
        log_bar.grid(row=self._ROW_LOGBAR, column=0, sticky="ew")
        self.log_toggle_btn = _ghost_btn(
            log_bar, "Show detailed log  ▾", self._toggle_log, width=170)
        self.log_toggle_btn.pack(side="left")

        self.log_container = _card(outer)
        # Not gridded initially — log is hidden by default.
        log_inner = ctk.CTkFrame(self.log_container, fg_color="transparent")
        log_inner.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_text = tk.Text(
            log_inner, height=4, wrap="word", state="disabled",
            bg=WP.SUNKEN_BG, fg=WP.LOG_INFO, relief="flat",
            highlightthickness=0, borderwidth=0, padx=8, pady=6,
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL))
        self.log_text.pack(side="left", fill="both", expand=True)
        log_sb = ctk.CTkScrollbar(log_inner, command=self.log_text.yview)
        log_sb.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=log_sb.set)
        self.log_text.tag_configure("info", foreground=WP.LOG_INFO)
        self.log_text.tag_configure(
            "stage", foreground=WP.LOG_STAGE,
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL, "bold"))
        self.log_text.tag_configure("success", foreground=WP.LOG_SUCCESS)
        self.log_text.tag_configure("error", foreground=WP.LOG_ERROR)
        self.log_text.tag_configure(
            "done", foreground=WP.LOG_DONE,
            font=(WP.FONT_FAMILY, WP.SIZE_SMALL, "bold"))

    # ----- Drop-zone adaptive layout ----------------------------------------

    def _layout_dropzone(self, level: str):
        """
        Show as much of the drop-zone content as fits:
          full   — arrow + main label + hint
          medium — main label + hint (arrow hidden)
          min    — main label only
        Trimming from the top/bottom keeps the stack shorter than the box,
        so nothing spills past the orange border when the window is small.
        """
        if level == self._dz_level:
            return
        self._dz_level = level
        for w in (self._dz_arrow, self._dz_main, self._dz_hint):
            w.pack_forget()
        if level == "full":
            self._dz_arrow.pack()
            self._dz_main.pack()
            self._dz_hint.pack(pady=(2, 0))
        elif level == "medium":
            self._dz_main.pack()
            self._dz_hint.pack(pady=(2, 0))
        else:  # min
            self._dz_main.pack()

    def _on_dropzone_resize(self, event=None):
        """
        Debounced: a window drag fires a burst of <Configure> events (one
        per pixel). Doing pack/forget work on each one — and the extra
        events that work itself triggers — makes resizing stutter. Instead
        we coalesce the burst into a single relayout shortly after the size
        settles, so dragging stays smooth.
        """
        if getattr(self, "_dz_resize_after", None) is not None:
            try:
                self.root.after_cancel(self._dz_resize_after)
            except Exception:
                pass
        self._dz_resize_after = self.root.after(40, self._apply_dropzone_layout)

    def _apply_dropzone_layout(self):
        """Pick the densest drop-zone layout that fits the current height."""
        self._dz_resize_after = None
        try:
            h = self.drop_zone.winfo_height()
        except Exception:
            return
        if h >= 96:
            self._layout_dropzone("full")
        elif h >= 58:
            self._layout_dropzone("medium")
        else:
            self._layout_dropzone("min")

    # ----- Log show/hide ----------------------------------------------------

    def _toggle_log(self):
        if self._log_visible:
            self.log_container.grid_remove()
            self._outer.rowconfigure(self._ROW_LOG, weight=5, minsize=0)
            self.log_toggle_btn.configure(text="Show detailed log  ▾")
            self._log_visible = False
            self._recompute_minsize()
            # Shrink back to the collapsed height (capped to the screen).
            cur_w = self.root.winfo_width() or self._WIN_W
            sh = self.root.winfo_screenheight()
            self.root.geometry(f"{cur_w}x{min(self._WIN_H_COLLAPSED, int(sh * 0.92))}")
        else:
            self.log_container.grid(row=self._ROW_LOG, column=0,
                                    sticky="nsew", pady=(6, 0))
            self._outer.rowconfigure(self._ROW_LOG, weight=5,
                                     minsize=self._LOG_MIN_H)
            self.log_toggle_btn.configure(text="Hide detailed log  ▴")
            self._log_visible = True
            self._recompute_minsize()
            # Grow to show the log (without exceeding the screen).
            cur_w = self.root.winfo_width() or self._WIN_W
            cur_h = self.root.winfo_height() or self._WIN_H_COLLAPSED
            sh = self.root.winfo_screenheight()
            target_h = min(max(cur_h, self._WIN_H_EXPANDED), int(sh * 0.95))
            self.root.geometry(f"{cur_w}x{target_h}")

    # ----- File queue management -------------------------------------------

    def _add_files(self, raw_paths: list[str]):
        added = 0
        for raw in raw_paths:
            p = Path(raw).expanduser()
            if not p.exists():
                continue
            if p.suffix.lower() not in SUPPORTED_EXTS:
                self._log(f"Skipped (unsupported type): {p.name}")
                continue
            if p in self.files:
                continue
            self.files.append(p)
            self.files_list.insert("end", p.name)
            added += 1
        if added:
            self._log(f"Added {added} file(s). Queue size: {len(self.files)}.")
            if not self._running:
                self._set_stage("idle", "Ready",
                                f"{len(self.files)} file(s) queued — press Extract.")

    def _on_drop(self, event):
        try:
            raw = self.root.tk.splitlist(event.data)
        except Exception:
            raw = [event.data]
        self._add_files(list(raw))

    def _browse(self):
        paths = filedialog.askopenfilenames(
            title="Choose flyers",
            filetypes=[("Flyers", "*.pdf *.png *.jpg *.jpeg *.webp *.tif *.tiff *.bmp"),
                       ("All files", "*.*")],
        )
        if paths:
            self._add_files(list(paths))

    def _clear(self):
        self.files.clear()
        self.files_list.delete(0, "end")
        self._log("Queue cleared.")
        if not self._running:
            self._set_stage("idle", "Ready", "Add flyers and press Extract.")

    # ----- Additional-instructions textbox helpers -------------------------

    def _on_instr_focus_in(self, _event=None):
        if self._instr_has_placeholder:
            self.instr_text.delete("1.0", "end")
            self.instr_text.configure(text_color=WP.TEXT)
            self._instr_has_placeholder = False

    def _on_instr_focus_out(self, _event=None):
        if not self.instr_text.get("1.0", "end-1c").strip():
            self.instr_text.delete("1.0", "end")
            self.instr_text.insert("1.0", self._instr_placeholder)
            self.instr_text.configure(text_color=WP.TEXT_FAINT)
            self._instr_has_placeholder = True

    def _get_extra_instructions(self) -> str:
        """User's free-form notes, with the selected broker-shorthand preset
        prepended so the model interprets that broker's notation."""
        if self._instr_has_placeholder:
            user = ""
        else:
            user = self.instr_text.get("1.0", "end-1c").strip()

        preset_block = ""
        name = self.preset_var.get()
        if name and name != self._PRESET_NONE:
            preset_block = self.field_hints.instruction_block_for(name)

        if preset_block and user:
            s = preset_block + "\n\n" + user
        else:
            s = preset_block or user

        if len(s) > _EXTRA_INSTRUCTIONS_GUI_LIMIT:
            s = s[:_EXTRA_INSTRUCTIONS_GUI_LIMIT]
        return s

    def _refresh_preset_choices(self):
        """Repopulate the preset dropdown from the saved field hints."""
        names = [self._PRESET_NONE] + self.field_hints.names()
        self.preset_menu.configure(values=names)
        if self.preset_var.get() not in names:
            self.preset_var.set(self._PRESET_NONE)

    def _open_field_hints(self):
        """Open the Field Hints manager, then refresh the picker."""
        dlg = FieldHintsDialog(self.root, self.field_hints)
        self._apply_window_icon(dlg)
        self.root.wait_window(dlg)
        # The dialog mutates+saves self.field_hints in place; reflect any
        # renames/additions/removals in the picker.
        self._refresh_preset_choices()

    def _clear_extra_instructions(self):
        self.instr_text.delete("1.0", "end")
        self.instr_text.insert("1.0", self._instr_placeholder)
        self.instr_text.configure(text_color=WP.TEXT_FAINT)
        self._instr_has_placeholder = True

    # ----- Stage banner -----------------------------------------------------

    def _set_stage(self, mode: str, title: str, detail: str = ""):
        """
        Update the prominent stage banner.

        mode controls the icon + bar colour:
          idle  — neutral dot, accent bar
          work  — spinner (animated in _tick), accent bar
          done  — green check, full green bar
          warn  — amber pause, amber bar
          error — red cross, red bar
        """
        self.stage_title.configure(text=title)
        self.stage_detail.configure(text=detail)
        if mode == "idle":
            self.stage_icon.configure(text="○", text_color=WP.TEXT_MUTED)
            self.progress.configure(progress_color=WP.ACCENT)
        elif mode == "done":
            self.stage_icon.configure(text="✓", text_color=WP.OK_GREEN)
            self.progress.configure(progress_color=WP.OK_GREEN)
            self.progress.set(1.0)
            self.percent_lbl.configure(text="100%")
        elif mode == "warn":
            self.stage_icon.configure(text="‖", text_color=WP.WARN_AMBER)
            self.progress.configure(progress_color=WP.WARN_AMBER)
        elif mode == "error":
            self.stage_icon.configure(text="✕", text_color=WP.DANGER_RED)
            self.progress.configure(progress_color=WP.DANGER_RED)
        # mode == "work": icon animated by _tick; bar stays accent.

    def _derive_stage_title(self, message: str) -> Optional[str]:
        """
        Map a raw pipeline log message to a friendly high-level stage
        title for the banner. Returns None to leave the title unchanged.
        """
        m = message.lower()
        label = None
        if "detecting file type" in m or "detected:" in m:
            label = "Reading flyer"
        elif "extracting text" in m or ("extracted" in m and "character" in m):
            label = "Reading text from flyer"
        elif ("sending text to" in m or "counting" in m or "phase" in m
              or "extracting fields" in m or "this can take a while" in m):
            label = "Extracting fields with AI"
        elif "awaiting user review" in m:
            label = "Waiting for your review"
        elif "user chose to skip" in m:
            label = "Skipped flyer"
        elif "writing" in m and "excel" in m:
            return "Writing to Excel"
        elif "saved output file" in m:
            return "Saving file"
        if label is None:
            return None
        if self._batch_total > 1:
            return f"Flyer {self._batch_current} of {self._batch_total} · {label}"
        return label

    # ----- Extraction -------------------------------------------------------

    def _extract(self):
        if not self.files:
            messagebox.showinfo("No files", "Add one or more flyers to the queue first.")
            return

        # Claude selected but no key saved → send the user to Settings.
        if self.cfg.provider == "claude" and not self.cfg.has_claude_key():
            if messagebox.askyesno(
                "Claude API key needed",
                "You're set to use the Claude API, but no API key is saved "
                "yet.\n\n"
                "Open Settings now to paste your Claude API key?\n\n"
                "(In Settings, hover the “?” next to the key box for "
                "step-by-step instructions on getting one, or ask a team "
                "member for an existing key.)",
                parent=self.root,
            ):
                self._open_settings()
            return

        ok, msg = ping_ollama(self.cfg)
        if not ok:
            if not messagebox.askyesno(
                "Ollama not reachable",
                msg + "\n\nDo you want to try extraction anyway?",
            ):
                return

        self.extract_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal", text="■ Stop")
        self.stop_btn.pack(side="right", padx=(0, 8))
        self.progress.set(0)
        self.percent_lbl.configure(text="0%")
        self._log("=" * 60, "stage")

        # Reset per-batch stage tracking and show the starting banner.
        self._batch_total = len(self.files)
        self._batch_current = 0
        self._run_saw_success = False
        self._run_saw_fatal = False
        self._run_fatal_detail = ""
        self._run_success_count = None
        self._run_success_file = ""
        self._set_stage("work", "Starting…", "Preparing extraction")

        import time
        self._running = True
        self._start_time = time.monotonic()
        self._stop_event = threading.Event()
        self._tick()

        survey_kind = self.survey_var.get()
        files_snapshot = list(self.files)
        stop_event = self._stop_event
        extras_snapshot = self._get_extra_instructions()
        if extras_snapshot:
            self._log(f"Using additional instructions: "
                      f"{extras_snapshot[:140]}"
                      f"{'...' if len(extras_snapshot) > 140 else ''}",
                      "stage")
        review_on = bool(self.review_var.get())
        review_callback = self._make_review_callback() if review_on else None
        if review_on and len(files_snapshot) > 1:
            self._log(
                f"Review mode is ON — flyers will be processed one at a "
                f"time, each opening a review dialog before saving.",
                "stage",
            )

        # Append target: re-validate at extract-time.
        target_path: Optional[Path] = None
        if self._output_mode_var.get() == "append":
            if self._append_target_path is None:
                messagebox.showerror(
                    "No append target selected",
                    "You chose 'Append to existing file' but didn't pick a "
                    "file. Click Browse to choose one, or switch back to "
                    "'Create new file'.",
                    parent=self.root,
                )
                self._reset_after_aborted_start()
                return
            from .excel_writer import detect_target_survey_kind
            kind = detect_target_survey_kind(self._append_target_path)
            if kind != survey_kind:
                messagebox.showerror(
                    "Survey type mismatch",
                    f"The append target is a {kind or 'unknown'} survey but "
                    f"the current run is {survey_kind}. Change one or the "
                    f"other before continuing.",
                    parent=self.root,
                )
                self._reset_after_aborted_start()
                return
            target_path = self._append_target_path
            self._log(f"Will append to {target_path}", "stage")

        # Output name override (Create-new-file mode only). Snapshot the
        # entry NOW so a worker thread never touches Tk state. Empty
        # string means "use the auto-timestamped default".
        output_name: Optional[str] = None
        if self._output_mode_var.get() == "new":
            raw = self._output_name_var.get().strip()
            if raw:
                # Windows-illegal characters. Catching these at submit
                # time (rather than silently sanitizing) is intentional.
                invalid_chars = set('<>:"/\\|?*')
                bad = sorted(set(raw) & invalid_chars)
                if bad:
                    messagebox.showerror(
                        "Invalid characters in output name",
                        f"The output filename can't contain: "
                        f"{' '.join(repr(c) for c in bad)}\n\n"
                        f"Pick a name using letters, numbers, spaces, "
                        f"dashes, and underscores.",
                        parent=self.root,
                    )
                    self._reset_after_aborted_start()
                    return
                if any(ord(c) < 32 for c in raw):
                    messagebox.showerror(
                        "Invalid characters in output name",
                        "The output filename contains control characters "
                        "that aren't allowed.",
                        parent=self.root,
                    )
                    self._reset_after_aborted_start()
                    return
                # Ensure .xlsx extension without destroying any other ext.
                if not raw.lower().endswith(".xlsx"):
                    raw = raw + ".xlsx"
                if not Path(raw).stem:
                    messagebox.showerror(
                        "Invalid output name",
                        "Please provide a filename (not just an extension).",
                        parent=self.root,
                    )
                    self._reset_after_aborted_start()
                    return
                candidate = Path(self.cfg.default_output_dir) / raw
                if candidate.exists():
                    overwrite = messagebox.askyesno(
                        "Overwrite existing file?",
                        f"A file named '{raw}' already exists in the "
                        f"output folder:\n  {candidate}\n\n"
                        f"Overwrite it?",
                        parent=self.root,
                    )
                    if not overwrite:
                        self._reset_after_aborted_start()
                        return
                output_name = raw
                self._log(f"Output filename: {raw}", "stage")

        def run():
            try:
                result = process_flyers(
                    files_snapshot,
                    survey_kind=survey_kind,
                    cfg=self.cfg,
                    on_progress=lambda ev: self.log_queue.put(ev),
                    stop_event=stop_event,
                    extra_instructions=extras_snapshot or None,
                    on_review=review_callback,
                    target_path=target_path,
                    output_name=output_name,
                )
                if stop_event.is_set():
                    self.log_queue.put(ProgressEvent(
                        "STOPPED — extraction was cancelled by the user.",
                        level="error"))
                    if result.write_summary:
                        self.log_queue.put(ProgressEvent(
                            f"    Partial output: {result.write_summary.output_path}",
                            level="info"))
                elif result.write_summary:
                    self.log_queue.put(ProgressEvent(
                        f"SUCCESS — {result.total_records} record(s) written to:",
                        level="success"))
                    self.log_queue.put(ProgressEvent(
                        f"    {result.write_summary.output_path}",
                        level="success"))
                else:
                    self.log_queue.put(ProgressEvent(
                        "Finished — no records were extracted, nothing written.",
                        level="error"))
                if result.failures:
                    self.log_queue.put(ProgressEvent(
                        f"{len(result.failures)} file(s) failed (details above).",
                        level="error"))
            except Exception as e:
                self.log_queue.put(ProgressEvent(
                    f"FATAL — {type(e).__name__}: {e}", level="error"))
            finally:
                self.log_queue.put("__DONE__")

        threading.Thread(target=run, daemon=True).start()

    def _reset_after_aborted_start(self):
        """
        Undo the 'running' UI state when a pre-flight validation popup
        cancelled the run before the worker thread started. Without this
        the Extract button would stay disabled and the spinner would spin
        forever.
        """
        self._running = False
        self._stop_event = None
        self._start_time = None
        self.extract_btn.configure(state="normal")
        self.stop_btn.pack_forget()
        self.elapsed_lbl.configure(text="")
        self.percent_lbl.configure(text="")
        self.progress.set(0)
        self._set_stage("idle", "Ready", "Add flyers and press Extract.")

    def _on_output_mode_change(self):
        """User toggled between 'Create new file' and 'Append to existing'."""
        is_append = self._output_mode_var.get() == "append"
        self._browse_target_btn.configure(
            state="normal" if is_append else "disabled")
        self._output_name_entry.configure(
            state="disabled" if is_append else "normal")
        if not is_append:
            self._append_target_path = None
            self._append_target_var.set("(none selected)")

    def _browse_append_target(self):
        """Pick an existing .xlsx survey file to append to."""
        from .excel_writer import detect_target_survey_kind
        initial_dir = (self._append_target_path.parent
                       if self._append_target_path else self.cfg.default_output_dir)
        chosen = filedialog.askopenfilename(
            title="Choose an existing survey file to append to",
            initialdir=str(initial_dir),
            filetypes=[("Excel files", "*.xlsx"), ("All files", "*.*")],
        )
        if not chosen:
            return
        path = Path(chosen)
        target_kind = detect_target_survey_kind(path)
        current_kind = self.survey_var.get()
        if target_kind is None:
            messagebox.showwarning(
                "Unrecognized file",
                f"This file doesn't look like a KBC survey template:\n  {path}\n\n"
                f"It may have been edited in a way that changed the headers "
                f"or sheet name. Pick a file that was originally created by "
                f"this tool, or use 'Create new file' instead.",
                parent=self.root,
            )
            return
        if target_kind != current_kind:
            messagebox.showwarning(
                "Survey type mismatch",
                f"The selected file is a {target_kind.capitalize()} Survey, "
                f"but the current run is set to {current_kind.capitalize()}.\n\n"
                f"Either pick a {current_kind.capitalize()} Survey file, or "
                f"change the Survey selector before appending.",
                parent=self.root,
            )
            return
        self._append_target_path = path
        display = str(path)
        if len(display) > 50:
            display = "..." + display[-47:]
        self._append_target_var.set(display)
        self._log(f"Append target set: {path}", "info")

    def _make_review_callback(self):
        from .review_dialog import request_review_blocking

        def _cb(request):
            return request_review_blocking(self.root, request)

        return _cb

    def _request_stop(self):
        if self._stop_event is not None and not self._stop_event.is_set():
            self._stop_event.set()
            self.stop_btn.configure(state="disabled", text="Stopping...")
            self._log("Stop requested — finishing the current step, please wait...",
                      "error")
            self._set_stage("warn", "Stopping…",
                            "Finishing the current step, please wait.")

    def _tick(self):
        """Self-rescheduling loop: animates the spinner and stopwatch."""
        if not self._running:
            return
        import time
        self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner_frames)
        self.stage_icon.configure(
            text=self._spinner_frames[self._spinner_idx], text_color=WP.ACCENT)
        if self._start_time is not None:
            secs = int(time.monotonic() - self._start_time)
            self.elapsed_lbl.configure(text=f"{secs // 60:d}:{secs % 60:02d}")
        self.root.after(120, self._tick)

    def _on_finished(self):
        """Reset run state once an extraction ends (success, stop, or error)."""
        self._running = False
        self.extract_btn.configure(state="normal")
        self.stop_btn.pack_forget()
        self._stop_event = None
        # Leave the final elapsed time + terminal banner visible.

    # ----- Log pump ---------------------------------------------------------

    def _log(self, msg: str, level: str = "info"):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n", level)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log(self):
        try:
            while True:
                item = self.log_queue.get_nowait()

                # Completion sentinel from the worker thread.
                if item == "__DONE__":
                    self._finalize_banner()
                    self._on_finished()
                    continue

                if isinstance(item, ProgressEvent):
                    if item.message:
                        self._log(item.message, item.level)
                        self._update_stage_from_event(item)
                    if item.total > 0:
                        frac = item.percent / 100.0
                        self.progress.set(frac)
                        self.percent_lbl.configure(text=f"{item.percent}%")
                else:
                    self._log(str(item))
        except queue.Empty:
            pass
        self.root.after(120, self._poll_log)

    def _update_stage_from_event(self, item: ProgressEvent):
        """Drive the big stage banner + outcome tracking from an event."""
        msg = item.message
        low = msg.lower()

        # Advance the per-flyer counter at the start of each flyer.
        if "detecting file type" in low:
            self._batch_current = min(self._batch_total or 1,
                                      self._batch_current + 1)

        # Track terminal outcomes for the final banner.
        if item.level == "success":
            if msg.strip().startswith("SUCCESS"):
                # "SUCCESS — N record(s) written to:"
                import re
                mnum = re.search(r"SUCCESS\s*—\s*(\d+)", msg)
                if mnum:
                    self._run_success_count = int(mnum.group(1))
                self._run_saw_success = True
            if ".xlsx" in low:
                self._run_success_file = Path(msg.strip()).name
        if item.level == "error" and "fatal" in low:
            self._run_saw_fatal = True
            self._run_fatal_detail = msg.strip()

        # Update the live working banner (only while running; terminal
        # states are set by _finalize_banner).
        if self._running:
            title = self._derive_stage_title(msg)
            if title is not None:
                detail = msg.strip()
                # Trim a leading "[name] " for a cleaner detail line.
                if detail.startswith("[") and "] " in detail:
                    detail = detail.split("] ", 1)[1]
                self._set_stage("work", title, detail)

    def _finalize_banner(self):
        """Pick the terminal banner state when the batch ends."""
        if self._stop_event is not None and self._stop_event.is_set():
            self._set_stage("warn", "Stopped",
                            "Cancelled — any partial output is noted in the log.")
        elif self._run_saw_fatal:
            self._set_stage("error", "Something went wrong",
                            self._run_fatal_detail or "See the log for details.")
        elif self._run_saw_success:
            if self._run_success_count is not None and self._run_success_file:
                detail = (f"{self._run_success_count} record(s) written to "
                          f"{self._run_success_file}")
            elif self._run_success_file:
                detail = f"Saved {self._run_success_file}"
            else:
                detail = "Records written successfully."
            self._set_stage("done", "Done", detail)
        else:
            self._set_stage("error", "Finished — nothing written",
                            "No records were extracted from the queued files.")

    # ----- Settings ---------------------------------------------------------

    def _open_settings(self):
        provider_before = self.cfg.provider
        dlg = SettingsDialog(self.root, self.cfg)
        self._apply_window_icon(dlg)
        self.root.wait_window(dlg)
        self.provider_var.set(
            "Claude API" if self.cfg.provider == "claude" else "Local (Ollama)")
        self._refresh_model_choices()
        if provider_before != "claude" and self.cfg.provider == "claude":
            self._show_claude_privacy_warning()

    # ----- Help -------------------------------------------------------------

    def _open_help(self):
        """Show the illustrated 'how it works' flowchart."""
        HelpDialog(self.root)

    # ----- Getting Started --------------------------------------------------

    def _show_getting_started(self):
        """Show the startup guide; persist the 'do not show again' choice."""
        dlg = GettingStartedDialog(self.root)
        self._apply_window_icon(dlg)
        self.root.wait_window(dlg)
        # Persist the user's preferences from the dialog.
        if dlg.dont_show_again:
            if self.cfg.show_getting_started:
                self.cfg.show_getting_started = False
                self.cfg.save()

    # ----- Output folder ----------------------------------------------------

    def _open_output_folder(self):
        """Open the user's output folder in the OS file explorer."""
        folder = Path(self.cfg.default_output_dir or
                      (Path.home() / "Documents" / "FlyerReader"))
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror(
                "Couldn't open folder",
                f"The output folder could not be created:\n{folder}\n\n{e}",
                parent=self.root)
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))  # noqa: F821 (Windows-only)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as e:
            messagebox.showerror(
                "Couldn't open folder",
                f"Tried to open:\n{folder}\n\nbut the file manager could not "
                f"be launched:\n{e}",
                parent=self.root)

    # ----- Run --------------------------------------------------------------

    def run(self):
        self.root.mainloop()


def main():
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    FlyerReaderApp().run()

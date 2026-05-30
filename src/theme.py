"""
Warm Paper theme — a single source of truth for the app's look.

Both the CustomTkinter main window (gui.py) and the ttk-based review
dialog (review_dialog.py) import from here so the two windows stay
visually consistent: cream paper backgrounds, terracotta accent,
rounded corners, soft borders.

Palette reference (the "Warm Paper" mockup):
    page bg .......... #fbf7f0   (cream)
    secondary bg ..... #f4ecdf   (toolbar / chip fill)
    panel/card bg .... #fffdf8   (off-white surfaces, inputs)
    accent ........... #d97742   (terracotta)
    text ............. #3a342c   (warm near-black)
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class WP:
    """Warm Paper palette + layout constants."""

    # --- Surfaces -------------------------------------------------------
    PAGE_BG = "#fbf7f0"        # outer window background (cream)
    SECONDARY_BG = "#f4ecdf"   # toolbars, segmented-control track, chips
    PANEL_BG = "#fffdf8"       # cards, inputs, raised surfaces (off-white)
    SUNKEN_BG = "#f7f0e4"      # log / canvas backgrounds, slightly recessed

    # --- Accent (terracotta) -------------------------------------------
    ACCENT = "#d97742"
    ACCENT_HOVER = "#c4632f"
    ACCENT_PRESSED = "#b0571f"
    ACCENT_SOFT = "#f6e6d6"    # very light terracotta fill (badges/highlights)
    ON_ACCENT = "#ffffff"      # text/icon on an accent fill

    # --- Text -----------------------------------------------------------
    TEXT = "#3a342c"           # primary
    TEXT_MUTED = "#8a7f6f"     # secondary / hints
    TEXT_FAINT = "#a8997f"     # faint captions

    # --- Borders --------------------------------------------------------
    BORDER = "#ece3d4"         # default hairline
    BORDER_STRONG = "#e0d2ba"  # hover / emphasis

    # --- Semantic (status banner, log) ---------------------------------
    OK_GREEN = "#5a8a4a"       # warm green for success/done
    OK_GREEN_SOFT = "#e9f0df"
    WARN_AMBER = "#b5612e"     # terracotta-brown for stage/warnings
    DANGER_RED = "#c0492e"     # warm red for errors
    DANGER_RED_SOFT = "#f7e4dd"

    # --- Confidence colours (review dialog field labels) ----------------
    CONF_LOW = "#b3402a"       # < 0.6  (scrutinize)
    CONF_MID = "#b5612e"       # < 0.8
    CONF_OK = "#3a342c"        # >= 0.8 (primary text)

    # --- Log line colours (on SUNKEN_BG) -------------------------------
    LOG_INFO = "#4a4338"
    LOG_STAGE = "#b5612e"
    LOG_SUCCESS = "#4a7c3f"
    LOG_ERROR = "#b3402a"
    LOG_DONE = "#b5612e"

    # --- Layout ---------------------------------------------------------
    RADIUS = 12                # default rounded-corner radius
    RADIUS_SM = 8
    RADIUS_PILL = 20           # pill buttons / toggles

    # --- Fonts ----------------------------------------------------------
    # Segoe UI on Windows; Tk substitutes a default if it's missing, so
    # this is safe cross-platform.
    FONT_FAMILY = "Segoe UI"
    SIZE_TITLE = 19
    SIZE_HEADING = 15
    SIZE_BODY = 13
    SIZE_SMALL = 12
    SIZE_TINY = 11
    SIZE_STAGE = 17            # the big "current stage" banner text


def apply_ctk_appearance(ctk) -> None:
    """
    Configure CustomTkinter global appearance for Warm Paper.

    We keep light mode fixed (the palette is a light theme) and set a
    neutral default colour theme; per-widget colours are passed
    explicitly throughout gui.py, so the global theme only affects the
    handful of internals we don't override.
    """
    ctk.set_appearance_mode("light")
    try:
        ctk.set_default_color_theme("dark-blue")
    except Exception:
        pass


def style_ttk(root: tk.Misc) -> ttk.Style:
    """
    Build a ttk.Style themed for Warm Paper. Used by the review dialog,
    which relies on ttk.Treeview / ttk.Entry / ttk.Button / etc. that
    have no CustomTkinter equivalents.

    Returns the configured Style (also installed on the default root).
    """
    style = ttk.Style(root)
    # 'clam' is the most themable built-in ttk theme — it actually honors
    # background/fieldbackground/bordercolor, unlike the native Win theme.
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    base_font = (WP.FONT_FAMILY, WP.SIZE_BODY)
    small_font = (WP.FONT_FAMILY, WP.SIZE_SMALL)
    heading_font = (WP.FONT_FAMILY, WP.SIZE_SMALL, "bold")

    # Frames / labels --------------------------------------------------
    style.configure("TFrame", background=WP.PAGE_BG)
    style.configure("Card.TFrame", background=WP.PANEL_BG)
    style.configure("TLabel", background=WP.PAGE_BG,
                    foreground=WP.TEXT, font=base_font)
    style.configure("Muted.TLabel", background=WP.PAGE_BG,
                    foreground=WP.TEXT_MUTED, font=small_font)
    style.configure("Accent.TLabel", background=WP.PAGE_BG,
                    foreground=WP.ACCENT, font=small_font)

    # LabelFrame (the "Records" / "Source page" group boxes) -----------
    style.configure("TLabelframe", background=WP.PAGE_BG,
                    bordercolor=WP.BORDER, relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=WP.PAGE_BG,
                    foreground=WP.WARN_AMBER, font=heading_font)

    # Buttons ----------------------------------------------------------
    # Default (ghost) button: paper fill, terracotta text, soft hover.
    style.configure("TButton", background=WP.PANEL_BG, foreground=WP.TEXT,
                    bordercolor=WP.BORDER, focuscolor=WP.ACCENT_SOFT,
                    relief="flat", padding=(12, 6), font=base_font)
    style.map("TButton",
              background=[("active", WP.SECONDARY_BG),
                          ("pressed", WP.SECONDARY_BG)],
              foreground=[("disabled", WP.TEXT_FAINT)],
              bordercolor=[("active", WP.BORDER_STRONG)])

    # Primary (terracotta) button — used for Approve.
    style.configure("Accent.TButton", background=WP.ACCENT,
                    foreground=WP.ON_ACCENT, bordercolor=WP.ACCENT,
                    relief="flat", padding=(14, 7),
                    font=(WP.FONT_FAMILY, WP.SIZE_BODY, "bold"))
    style.map("Accent.TButton",
              background=[("active", WP.ACCENT_HOVER),
                          ("pressed", WP.ACCENT_PRESSED)],
              foreground=[("disabled", "#f3e3d6")])

    # Entries ----------------------------------------------------------
    style.configure("TEntry", fieldbackground=WP.PANEL_BG,
                    background=WP.PANEL_BG, foreground=WP.TEXT,
                    bordercolor=WP.BORDER, insertcolor=WP.TEXT,
                    relief="flat", padding=4)
    style.map("TEntry", bordercolor=[("focus", WP.ACCENT)])

    # Combobox ---------------------------------------------------------
    style.configure("TCombobox", fieldbackground=WP.PANEL_BG,
                    background=WP.PANEL_BG, foreground=WP.TEXT,
                    bordercolor=WP.BORDER, arrowcolor=WP.ACCENT,
                    relief="flat", padding=4)
    style.map("TCombobox",
              fieldbackground=[("readonly", WP.PANEL_BG)],
              bordercolor=[("focus", WP.ACCENT)])

    # Scrollbar --------------------------------------------------------
    style.configure("Vertical.TScrollbar", background=WP.SECONDARY_BG,
                    troughcolor=WP.PAGE_BG, bordercolor=WP.PAGE_BG,
                    arrowcolor=WP.TEXT_MUTED, relief="flat")
    style.configure("Horizontal.TScrollbar", background=WP.SECONDARY_BG,
                    troughcolor=WP.PAGE_BG, bordercolor=WP.PAGE_BG,
                    arrowcolor=WP.TEXT_MUTED, relief="flat")

    # Treeview (records grid) ------------------------------------------
    style.configure("Treeview", background=WP.PANEL_BG,
                    fieldbackground=WP.PANEL_BG, foreground=WP.TEXT,
                    bordercolor=WP.BORDER, relief="flat", rowheight=24,
                    font=small_font)
    style.configure("Treeview.Heading", background=WP.SECONDARY_BG,
                    foreground=WP.TEXT, bordercolor=WP.BORDER,
                    relief="flat", font=heading_font)
    style.map("Treeview",
              background=[("selected", WP.ACCENT_SOFT)],
              foreground=[("selected", WP.TEXT)])
    style.map("Treeview.Heading",
              background=[("active", WP.BORDER)])

    return style

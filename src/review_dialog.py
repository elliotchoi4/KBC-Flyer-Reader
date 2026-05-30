"""
Per-flyer review dialog: inspect, edit, and approve extracted records
before they are written to Excel.

Layout (roughly):

    +-----------------------------------------------------------+
    |  <flyer name>                                             |
    +-----------------------------------------------------------+
    |  Records (1 row per record)                |              |
    |  [grid: #, property, address, area_sf, ...]|              |
    |                                            |  <PDF page>  |
    +--------------------------------------------+              |
    |  Edit selected record:                     |  rendered    |
    |  field            value                    |  here, with  |
    |  property_name    [______________]         |  source      |
    |  city             [______________]   ▶ p.2 |  snippet     |
    |  state            [______________]         |  highlighted |
    |  ...                                       |              |
    +--------------------------------------------+              |
    |       [Delete this record]  [+ Add record] |  ◀ p.1 ▶     |
    +-----------------------------------------------------------+
    |                  [Skip flyer]    [Approve & write Excel]  |
    +-----------------------------------------------------------+

Threading model
---------------
The pipeline runs on a worker thread and must BLOCK until the user
finishes review. The Tk dialog itself must run on the main GUI thread.
`request_review_blocking()` is the bridge:

  worker thread                       GUI thread
  -------------                       ----------
  request_review_blocking(req)  -->   root.after(0, _open_dialog, req, slot, done)
  done.wait()  (blocked)              [dialog opens, user interacts]
                                      slot["response"] = response
                                      done.set()
  return slot["response"]    <--

The implementation works for any GUI loop that exposes `root.after`.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Optional

from .pipeline import ReviewRequest, ReviewResponse
from .theme import WP, style_ttk

log = logging.getLogger(__name__)


# Which fields to show as columns in the records grid. We keep this short
# so the table fits at common window sizes; the full field set is editable
# in the detail panel below. Building and land share `property_name`,
# `city`, `state`, `address`, so the grid columns work for both.
_GRID_COLUMNS_BUILDING = [
    ("property_name", "Property", 200),
    ("address", "Address", 200),
    ("city", "City", 100),
    ("total_building_area_sf", "Total SF", 90),
    ("available_sf", "Available SF", 100),
]
_GRID_COLUMNS_LAND = [
    ("property_name", "Property", 200),
    ("address", "Address", 200),
    ("city", "City", 100),
    ("total_acres", "Acres", 80),
    ("zoning", "Zoning", 100),
]


def request_review_blocking(
    root: tk.Tk, request: ReviewRequest,
) -> ReviewResponse:
    """
    Block the caller (a pipeline worker thread) while the review dialog
    runs on the Tk main thread. Returns the user's ReviewResponse.

    Safe to call from a non-Tk thread: we marshal the dialog creation
    onto the Tk thread via root.after and wait on a threading.Event.
    """
    slot: dict[str, Any] = {}
    done = threading.Event()

    def _open():
        try:
            dlg = ReviewDialog(root, request)
            dlg.wait_until_closed()
            slot["response"] = dlg.response
        except Exception as e:
            log.exception("Review dialog crashed")
            # On crash, default to approving the records as-is rather
            # than dropping them.
            slot["response"] = ReviewResponse(
                approved=True, records=list(request.records))
        finally:
            done.set()

    root.after(0, _open)
    done.wait()
    return slot["response"]


class ReviewDialog(tk.Toplevel):
    """
    Modal review dialog. Construction must happen on the Tk thread.
    Use the helper request_review_blocking() from worker threads.
    """

    def __init__(self, parent: tk.Tk, request: ReviewRequest):
        super().__init__(parent)
        self.title(f"Review — {request.source_path.name}")
        self.configure(bg=WP.PAGE_BG)
        # Install the Warm Paper ttk theme. Normally already installed by
        # the main app, but doing it here makes the dialog self-sufficient
        # (e.g. when exercised in isolation).
        style_ttk(self)
        self.request = request
        # Work on copies of the records so we don't mutate the originals
        # until the user clicks Approve.
        self._records = [r.model_copy(deep=True) for r in request.records]
        self.response: Optional[ReviewResponse] = None

        # PDF rendering state ----------------------------------------------
        # _pdf_doc is a fitz.Document; we keep it open for the lifetime of
        # the dialog and close it in _on_close. _page_count is 1 for image
        # flyers (rendered as a single "page"). _current_page is 1-indexed.
        self._pdf_doc = None
        self._page_count = 1
        self._current_page = 1
        # The PhotoImage reference must be held on the dialog object;
        # otherwise Tk garbage-collects it and the canvas shows nothing.
        self._photo: Optional[tk.PhotoImage] = None
        # Cached source-page guess per (record_index, field_name).
        # Computed lazily on first field click. Value is (page, matched
        # variant string) on success or None when nothing matched.
        self._source_cache: dict[tuple[int, str], Optional[tuple[int, str]]] = {}
        # Most recent highlight rectangles, in source-PDF coordinates.
        # Used by the canvas redraw on page change.
        self._highlight_phrase: Optional[str] = None

        # Try to open the source file. PDF -> fitz.Document. Image ->
        # fitz still works for many images (PNG/JPG), but we handle that
        # in _render_page. Failure here is non-fatal: the user can still
        # review without the source panel.
        self._open_source()

        self._build_ui()

        # Center on parent and make modal.
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_close_no_decision)
        # A reasonable default size — tall enough for the grid + edit
        # panel, wide enough to fit a typical letter-size PDF preview.
        self.geometry("1400x850")
        self.update_idletasks()
        # Render the first page once geometry has settled.
        self._render_current_page()

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def wait_until_closed(self):
        """Block the Tk event loop on this dialog until it closes."""
        self.wait_window(self)

    # ----------------------------------------------------------------
    # UI construction
    # ----------------------------------------------------------------

    def _build_ui(self):
        # Top-level layout: left side has records + edit panel, right
        # side has the source-page viewer. Bottom row has Approve/Skip.
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)

        # Two columns: left=records, right=PDF
        body.columnconfigure(0, weight=1, uniform="cols")
        body.columnconfigure(1, weight=1, uniform="cols")
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        self._build_records_panel(left)
        self._build_pdf_panel(right)

        # Bottom action row.
        btns = ttk.Frame(root)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Skip flyer (drop these records)",
                   command=self._on_skip).pack(side="left")
        ttk.Button(btns, text="Approve & write Excel",
                   style="Accent.TButton",
                   command=self._on_approve).pack(side="right")
        ttk.Label(
            btns,
            text=(f"Reviewing {self.request.source_path.name}  •  "
                  f"{len(self._records)} record(s)"),
            foreground=WP.TEXT_MUTED,
        ).pack(side="left", padx=(16, 0))

    def _build_records_panel(self, parent: ttk.Frame):
        # Records grid: one row per record, key fields as columns.
        grid_frame = ttk.LabelFrame(parent, text="Records", padding=4)
        grid_frame.pack(fill="both", expand=True)

        cols = (_GRID_COLUMNS_LAND if self.request.survey_kind == "land"
                else _GRID_COLUMNS_BUILDING)
        col_ids = ["#"] + [c[0] for c in cols]
        self._grid = ttk.Treeview(
            grid_frame, columns=col_ids[1:], show="tree headings",
            selectmode="browse", height=8,
        )
        self._grid.heading("#0", text="#")
        self._grid.column("#0", width=40, anchor="center", stretch=False)
        for field, label, width in cols:
            self._grid.heading(field, text=label)
            self._grid.column(field, width=width, anchor="w")
        self._grid.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(grid_frame, orient="vertical",
                               command=self._grid.yview)
        scroll.pack(side="right", fill="y")
        self._grid.configure(yscrollcommand=scroll.set)
        self._grid.bind("<<TreeviewSelect>>", self._on_grid_select)

        self._refresh_grid()

        # Grid-row actions row.
        row_btns = ttk.Frame(parent)
        row_btns.pack(fill="x", pady=(4, 0))
        ttk.Button(row_btns, text="Delete selected record",
                   command=self._on_delete_record).pack(side="left")
        ttk.Button(row_btns, text="+ Add blank record",
                   command=self._on_add_record).pack(side="left", padx=(4, 0))

        # Detail / edit panel below the grid.
        self._detail_frame = ttk.LabelFrame(parent, text="Edit fields",
                                            padding=4)
        self._detail_frame.pack(fill="both", expand=True, pady=(8, 0))

        # We use a Canvas + inner Frame for the scrollable edit form.
        # Building has ~60 fields, so the form must scroll.
        self._detail_canvas = tk.Canvas(self._detail_frame,
                                        background=WP.PAGE_BG,
                                        highlightthickness=0)
        self._detail_canvas.pack(side="left", fill="both", expand=True)
        det_scroll = ttk.Scrollbar(self._detail_frame, orient="vertical",
                                   command=self._detail_canvas.yview)
        det_scroll.pack(side="right", fill="y")
        self._detail_canvas.configure(yscrollcommand=det_scroll.set)

        self._detail_inner = ttk.Frame(self._detail_canvas)
        self._detail_window_id = self._detail_canvas.create_window(
            (0, 0), window=self._detail_inner, anchor="nw")
        self._detail_inner.bind(
            "<Configure>",
            lambda e: self._detail_canvas.configure(
                scrollregion=self._detail_canvas.bbox("all")),
        )
        self._detail_canvas.bind(
            "<Configure>",
            lambda e: self._detail_canvas.itemconfigure(
                self._detail_window_id, width=e.width),
        )
        # Bind mousewheel so scrolling works when the cursor is over the form.
        self._detail_canvas.bind(
            "<Enter>",
            lambda e: self._detail_canvas.bind_all(
                "<MouseWheel>", self._on_detail_mousewheel))
        self._detail_canvas.bind(
            "<Leave>",
            lambda e: self._detail_canvas.unbind_all("<MouseWheel>"))

        # Backing store for the per-field StringVars of the currently-
        # displayed record. Rebuilt on every selection change.
        self._field_vars: dict[str, tk.StringVar] = {}
        self._current_record_idx: Optional[int] = None

        # Select the first record by default so the form populates.
        children = self._grid.get_children()
        if children:
            self._grid.selection_set(children[0])
            self._grid.focus(children[0])

    def _build_pdf_panel(self, parent: ttk.Frame):
        wrap = ttk.LabelFrame(parent, text="Source page", padding=4)
        wrap.pack(fill="both", expand=True)

        # Top: page navigation controls.
        nav = ttk.Frame(wrap)
        nav.pack(fill="x", pady=(0, 4))
        self._prev_btn = ttk.Button(nav, text="◀ Prev",
                                    command=self._prev_page)
        self._prev_btn.pack(side="left")
        self._page_var = tk.StringVar(value="")
        ttk.Label(nav, textvariable=self._page_var,
                  width=14, anchor="center").pack(side="left", padx=8)
        self._next_btn = ttk.Button(nav, text="Next ▶",
                                    command=self._next_page)
        self._next_btn.pack(side="left")
        self._source_hint_var = tk.StringVar(value="")
        ttk.Label(nav, textvariable=self._source_hint_var,
                  foreground=WP.ACCENT).pack(side="left", padx=(16, 0))

        # The page canvas.
        canvas_frame = ttk.Frame(wrap)
        canvas_frame.pack(fill="both", expand=True)
        self._page_canvas = tk.Canvas(canvas_frame, background=WP.SUNKEN_BG,
                                      highlightthickness=0)
        self._page_canvas.pack(fill="both", expand=True)
        # We re-render on resize so the page fills the available space.
        self._page_canvas.bind("<Configure>",
                               lambda e: self._render_current_page())

    # ----------------------------------------------------------------
    # Records grid + edit form
    # ----------------------------------------------------------------

    def _refresh_grid(self):
        # Wipe + repopulate.
        for iid in self._grid.get_children():
            self._grid.delete(iid)
        cols = (_GRID_COLUMNS_LAND if self.request.survey_kind == "land"
                else _GRID_COLUMNS_BUILDING)
        for i, rec in enumerate(self._records):
            data = rec.model_dump()
            values = []
            for field, _, _ in cols:
                v = data.get(field)
                values.append("" if v is None else str(v))
            self._grid.insert("", "end", iid=str(i),
                              text=str(i + 1), values=values)

    def _on_grid_select(self, _event):
        sel = self._grid.selection()
        if not sel:
            self._current_record_idx = None
            self._clear_detail()
            return
        idx = int(sel[0])
        self._current_record_idx = idx
        self._populate_detail(idx)

    def _clear_detail(self):
        for w in self._detail_inner.winfo_children():
            w.destroy()
        self._field_vars.clear()

    def _populate_detail(self, idx: int):
        """Build the edit form for record at index idx."""
        self._clear_detail()
        rec = self._records[idx]
        data = rec.model_dump()
        confidence = data.get("confidence_notes", {}) or {}

        # Build a deterministic field order: schema order (Pydantic preserves
        # declaration order), skipping confidence_notes which is internal.
        fields = [k for k in data.keys() if k != "confidence_notes"]

        # Two-column grid: name | value entry (+ source-page button)
        for row_i, field_name in enumerate(fields):
            value = data.get(field_name)
            display_value = "" if value is None else str(value)
            conf = confidence.get(field_name, 1.0)

            # Color-code label by confidence so the user knows what to scrutinize.
            label_fg = WP.CONF_OK
            if isinstance(conf, (int, float)):
                if conf < 0.6:
                    label_fg = WP.CONF_LOW  # warm red for very low
                elif conf < 0.8:
                    label_fg = WP.CONF_MID  # terracotta-brown for low

            lbl = ttk.Label(self._detail_inner, text=field_name,
                            foreground=label_fg, width=28, anchor="w")
            lbl.grid(row=row_i, column=0, sticky="w",
                     padx=(2, 4), pady=1)

            var = tk.StringVar(value=display_value)
            self._field_vars[field_name] = var
            ent = ttk.Entry(self._detail_inner, textvariable=var)
            ent.grid(row=row_i, column=1, sticky="we", padx=(0, 4), pady=1)

            # "Show source" button: jump to the page where this value
            # appears in the flyer text. Only meaningful for non-empty
            # values.
            if display_value:
                btn = ttk.Button(
                    self._detail_inner, text="↪ source", width=8,
                    command=lambda f=field_name, v=display_value, i=idx:
                            self._jump_to_source(i, f, v),
                )
                btn.grid(row=row_i, column=2, padx=(0, 2), pady=1)

        self._detail_inner.columnconfigure(1, weight=1)

    def _commit_current_edits(self):
        """
        Apply the current StringVar values back to the selected record
        in-place. Called before switching to another record and before
        approving.
        """
        if self._current_record_idx is None:
            return
        if not self._field_vars:
            return
        rec = self._records[self._current_record_idx]
        original = rec.model_dump()
        updates: dict[str, Any] = {}
        for field_name, var in self._field_vars.items():
            new_text = var.get().strip()
            old_value = original.get(field_name)
            # If the entry is blank, the field becomes None.
            if new_text == "":
                if old_value is not None:
                    updates[field_name] = None
                continue
            # Decide whether to coerce to int / float based on the existing
            # type (when there was one) or by trying parsing. We always
            # attempt an int parse before a float parse, since the schema
            # frequently uses ints for whole-number fields like dock_doors.
            coerced = _coerce_for_field(new_text, old_value)
            if coerced != old_value:
                updates[field_name] = coerced
        if updates:
            try:
                self._records[self._current_record_idx] = rec.model_copy(
                    update=updates)
            except Exception as e:
                log.warning("Failed to apply edits: %s", e)

    def _on_delete_record(self):
        if self._current_record_idx is None:
            return
        del self._records[self._current_record_idx]
        self._field_vars.clear()
        self._current_record_idx = None
        self._refresh_grid()
        self._clear_detail()
        # Re-select the first record if any remain.
        children = self._grid.get_children()
        if children:
            self._grid.selection_set(children[0])

    def _on_add_record(self):
        """Insert a blank record by copying the type of an existing record."""
        if not self._records:
            return
        # Clone the first record and clear every editable field. This
        # preserves the Pydantic class so the writer still accepts it.
        template = self._records[0]
        cleared = template.model_dump()
        for k in list(cleared.keys()):
            if k == "confidence_notes":
                cleared[k] = {}
            else:
                cleared[k] = None
        try:
            new_rec = type(template).model_validate(cleared)
        except Exception as e:
            log.warning("Could not create blank record: %s", e)
            return
        self._records.append(new_rec)
        self._refresh_grid()
        # Select the new one.
        new_iid = str(len(self._records) - 1)
        self._grid.selection_set(new_iid)
        self._grid.focus(new_iid)
        self._grid.see(new_iid)

    def _on_detail_mousewheel(self, event):
        # Tk on Windows: event.delta is ±120 per wheel notch.
        self._detail_canvas.yview_scroll(int(-1 * (event.delta / 120)),
                                         "units")

    # ----------------------------------------------------------------
    # Source-page resolution + PDF rendering
    # ----------------------------------------------------------------

    def _open_source(self):
        """Open the flyer in fitz. Falls back gracefully on errors."""
        try:
            import fitz  # PyMuPDF
            path = self.request.source_path
            # fitz handles PDFs natively. For images, we open via fitz too
            # (it supports common raster formats), or fall back to PIL.
            self._pdf_doc = fitz.open(str(path))
            self._page_count = len(self._pdf_doc)
            if self._page_count == 0:
                self._page_count = 1
        except Exception as e:
            log.warning("Could not open source for preview: %s", e)
            self._pdf_doc = None
            self._page_count = 1

    def _value_variants(self, value: str, field_name: str) -> list[str]:
        """
        Generate plausible flyer-form variants of an extracted value.

        Motivation: the LLM normalizes values during extraction. A flyer
        that says "50'" becomes "50.0"; "1,250,000 SF" becomes "1250000";
        "$5.50/SF" becomes "5.5". The literal extracted string then
        doesn't appear anywhere in the PDF text layer, and the source
        button reports "source could not be found" even though the
        information is right there.

        This produces a small ordered list of strings to search for. We
        prioritize unit-anchored forms ("50'", "250,000 SF") over bare
        numeric forms because short bare numbers match incidentally in
        addresses, zip codes, page numbers, etc.

        Order matters — the first match wins.
        """
        # The literal `value` goes first ONLY if it's not a bare number.
        # For bare numerics, we want unit-anchored variants to be tried
        # first ("36 dock" before "36"), because a bare "36" matches
        # incidentally in dates, ordinals ("36th"), addresses, etc.
        # Non-numeric strings ("Acme Corp") DO want the literal first.
        is_bare_numeric_literal = bool(re.fullmatch(r"-?\d+(?:\.\d+)?", value))
        out: list[str] = []
        if not is_bare_numeric_literal:
            out.append(value)

        # --- Numeric normalization ----------------------------------------
        # Detect what kind of numeric shape we have, if any.
        bare_int = re.fullmatch(r"-?\d+", value)
        bare_float = re.fullmatch(r"-?\d+\.\d+", value)
        bare_decimal_zero = re.fullmatch(r"-?\d+\.0+", value)
        comma_num = re.fullmatch(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?", value)

        # Build the "canonical numeric forms" we'll then unit-suffix.
        # Each is a tuple (string_form, is_strictly_numeric_for_wb).
        numeric_forms: list[str] = []
        if bare_int:
            numeric_forms.append(value)
            # Try with .0 too — covers the rare reverse case.
            numeric_forms.append(f"{value}.0")
            # Try with comma thousands if it's long enough.
            if len(value.lstrip("-")) >= 4:
                try:
                    numeric_forms.append(f"{int(value):,}")
                except ValueError:
                    pass
        elif bare_decimal_zero:
            # 50.0 -> also 50
            stripped = value.split(".", 1)[0]
            numeric_forms.append(stripped)
            numeric_forms.append(value)
            # And with comma thousands.
            if len(stripped.lstrip("-")) >= 4:
                try:
                    numeric_forms.append(f"{int(stripped):,}")
                except ValueError:
                    pass
        elif bare_float:
            numeric_forms.append(value)
            # 2.5 -> nothing to strip; no integer alias.
        elif comma_num:
            numeric_forms.append(value)
            numeric_forms.append(value.replace(",", ""))

        # --- Field-name-driven unit suffixes ------------------------------
        # We try unit-suffixed variants BEFORE bare numerics because
        # they're much more anchored (a flyer rarely contains "50" as
        # a standalone but often says "50'" or "50 SF").
        fn = field_name.lower()
        unit_variants: list[str] = []

        def _with_units(num_form: str, *suffixes: str) -> None:
            for s in suffixes:
                unit_variants.append(f"{num_form}{s}")

        for nf in numeric_forms:
            # Feet / heights — clear height, ceiling, etc.
            if any(k in fn for k in ("height", "clear", "ceiling")):
                _with_units(nf, "'", "' clear", "' clr", " ft", "ft",
                            " feet", "'-0\"", "' - 0\"")
            # Square feet — building area, available, office, etc.
            if any(k in fn for k in ("_sf", "area_sf", "space", "office",
                                     "building_area", "available")):
                _with_units(nf, " SF", " sf", "SF", "sf", " S.F.",
                            " sq ft", " square feet", " sq. ft.")
            # Acres / land area
            if "acre" in fn or fn.endswith("_acres") or "land_area" in fn:
                _with_units(nf, " acres", " AC", " ac", "AC", " ac.")
            # Doors — dock doors, drive-in, etc. (often appear as "32 DH")
            if any(k in fn for k in ("door", "dock", "drive_in", "_dh")):
                _with_units(nf, " dock", " DH", " D.H.", " doors", " door")
            # Money — rent, rate, price
            if any(k in fn for k in ("rate", "rent", "price", "psf", "nnn")):
                _with_units(nf, "/SF", "/sf", "/SF/yr", " PSF", " psf")
                unit_variants.append(f"${nf}")
                unit_variants.append(f"$ {nf}")
            # Year — rare transforms (1999 vs '99); skip unless we see one
            if "year" in fn and bare_int:
                yr = value.lstrip("-")
                if len(yr) == 4:
                    unit_variants.append(f"'{yr[-2:]}")  # 1999 -> '99

        out.extend(unit_variants)

        # Bare numeric forms come AFTER unit-suffixed ones for the reason
        # above. word-boundary handling happens in the searcher.
        out.extend(numeric_forms)

        # Deduplicate, preserve order, drop empties.
        seen: set[str] = set()
        deduped: list[str] = []
        for v in out:
            if v and v not in seen:
                seen.add(v)
                deduped.append(v)
        return deduped

    def _resolve_source_page(
        self, rec_idx: int, field_name: str, value: str,
    ) -> Optional[tuple[int, str]]:
        """
        Best-effort: find the 1-indexed page number where `value` (or a
        plausible flyer-form variant of it) appears in the extracted
        text. Returns (page_number, matched_variant) on success — the
        matched variant string is the one to highlight on the rendered
        page, NOT the original `value` (the original may not exist in
        the PDF at all if the LLM transformed it).

        Returns None if no variant matches.

        Heuristic ranking:
          1. Try each variant in order from _value_variants(); first hit
             wins. Unit-anchored variants come before bare numbers, so
             "50'" is tried before "50" — avoids matching short numbers
             incidentally in addresses, page numbers, etc.
          2. Bare numbers use word-boundary regex so "60" doesn't match
             inside "1960".
          3. All other variants use case-insensitive substring match.
          4. If nothing matches, return None — better to admit ignorance
             than point at a wrong page.
        """
        cache_key = (rec_idx, field_name)
        if cache_key in self._source_cache:
            return self._source_cache[cache_key]

        page_text = self.request.page_text
        if not page_text or not value:
            self._source_cache[cache_key] = None
            return None

        ordered_pages = sorted(page_text.keys())
        variants = self._value_variants(value, field_name)

        result: Optional[tuple[int, str]] = None
        for variant in variants:
            is_bare_numeric = bool(re.fullmatch(r"-?\d+(?:\.\d+)?", variant))
            if is_bare_numeric:
                pattern = re.compile(r"(?<!\d)" + re.escape(variant) + r"(?!\d)")
                for pno in ordered_pages:
                    if pattern.search(page_text[pno] or ""):
                        result = (pno, variant)
                        break
            else:
                vlow = variant.lower()
                for pno in ordered_pages:
                    if vlow in (page_text[pno] or "").lower():
                        result = (pno, variant)
                        break
            if result is not None:
                break

        self._source_cache[cache_key] = result
        return result

    def _jump_to_source(self, rec_idx: int, field_name: str, value: str):
        # Commit any pending edits so the underlying record matches the
        # current entry contents before we change the highlight.
        self._commit_current_edits()
        resolved = self._resolve_source_page(rec_idx, field_name, value)
        if resolved is None:
            self._source_hint_var.set(
                "Source not located in flyer text — value may have been inferred.")
            self._highlight_phrase = None
        else:
            page, matched = resolved
            # Show the user what we actually matched on when it differs
            # from the extracted value — useful feedback for cases where
            # the LLM normalized "50'" to "50.0".
            if matched != value:
                self._source_hint_var.set(
                    f"{field_name} → page {page}  (matched: {matched!r})")
            else:
                self._source_hint_var.set(f"{field_name} → page {page}")
            # IMPORTANT: highlight the matched variant, not the original
            # value. fitz.search_for needs a string that actually appears
            # in the PDF text layer; the original value may have been
            # normalized away from anything that's literally there.
            self._highlight_phrase = matched
            self._current_page = page
            self._render_current_page()

    def _render_current_page(self):
        """Render the current PDF page into the canvas, with optional highlight."""
        canvas = self._page_canvas
        canvas.delete("all")
        self._photo = None

        # Update the navigation label and button enabled state.
        self._page_var.set(
            f"Page {self._current_page} / {self._page_count}")
        self._prev_btn.configure(
            state="normal" if self._current_page > 1 else "disabled")
        self._next_btn.configure(
            state="normal" if self._current_page < self._page_count else "disabled")

        if self._pdf_doc is None:
            canvas.create_text(
                canvas.winfo_width() // 2 or 50,
                canvas.winfo_height() // 2 or 50,
                text="(Source preview unavailable)",
                fill=WP.TEXT_MUTED,
            )
            return

        # Render the page at a size that fits the canvas.
        try:
            page = self._pdf_doc[self._current_page - 1]
            canvas_w = max(canvas.winfo_width(), 200)
            canvas_h = max(canvas.winfo_height(), 200)

            # Choose a zoom that fits the page within the canvas while
            # preserving aspect ratio. Account for the page's intrinsic
            # rotation by querying the rotated rectangle.
            pr = page.rect
            zoom = min(canvas_w / max(pr.width, 1),
                       canvas_h / max(pr.height, 1))
            # Clamp to a reasonable range; very tall canvases can produce
            # absurd zooms otherwise.
            zoom = max(0.4, min(zoom, 3.5))
            import fitz
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)

            # Convert to a Tk-friendly PhotoImage via PIL.
            from PIL import Image, ImageTk, ImageDraw
            img = Image.frombytes(
                "RGB", (pix.width, pix.height), pix.samples)

            # If we have a highlight phrase, ask fitz for its locations on
            # this page and draw a red box around each match — no fill,
            # so the underlying text stays fully visible.
            if self._highlight_phrase:
                try:
                    hits = page.search_for(self._highlight_phrase)
                except Exception:
                    hits = []
                if hits:
                    # Outline-only box — no fill, so the PDF text
                    # underneath is never obscured. The box alone is
                    # enough to point the user at the source location.
                    draw = ImageDraw.Draw(img)
                    for rect in hits:
                        x0, y0 = rect.x0 * zoom, rect.y0 * zoom
                        x1, y1 = rect.x1 * zoom, rect.y1 * zoom
                        # Pad the box slightly for visibility.
                        draw.rectangle(
                            [x0 - 2, y0 - 2, x1 + 2, y1 + 2],
                            outline=(220, 0, 0),
                            width=2,
                        )

            self._photo = ImageTk.PhotoImage(img)
            # Center the image on the canvas.
            x = canvas_w // 2
            y = canvas_h // 2
            canvas.create_image(x, y, image=self._photo, anchor="center")
        except Exception as e:
            log.warning("Failed to render page %s: %s",
                        self._current_page, e)
            canvas.create_text(
                canvas.winfo_width() // 2 or 50,
                canvas.winfo_height() // 2 or 50,
                text=f"(Page render failed: {type(e).__name__})",
                fill=WP.DANGER_RED,
            )

    def _prev_page(self):
        if self._current_page > 1:
            self._current_page -= 1
            self._render_current_page()

    def _next_page(self):
        if self._current_page < self._page_count:
            self._current_page += 1
            self._render_current_page()

    # ----------------------------------------------------------------
    # Decision handlers
    # ----------------------------------------------------------------

    def _on_approve(self):
        # Commit any pending edits to the current record, then close
        # with approved=True.
        self._commit_current_edits()
        self.response = ReviewResponse(
            approved=True, records=list(self._records))
        self._cleanup()
        self.destroy()

    def _on_skip(self):
        self.response = ReviewResponse(approved=False, records=[])
        self._cleanup()
        self.destroy()

    def _on_close_no_decision(self):
        """User closed the dialog via the window manager — treat as approve."""
        # Defaulting to approve preserves the user's extraction work; the
        # alternative (defaulting to skip) would silently discard records
        # someone might have waited several minutes to get.
        self._on_approve()

    def _cleanup(self):
        if self._pdf_doc is not None:
            try:
                self._pdf_doc.close()
            except Exception:
                pass
            self._pdf_doc = None


def _coerce_for_field(text: str, old_value: Any) -> Any:
    """
    Convert a user-entered string back to the right type for the field.

    Decision rule:
      - If the old value was an int, try to parse text as int (rejecting
        "12.5"); fall back to str if it's not a clean integer.
      - If the old value was a float, try float.
      - Otherwise return the string as-is.
    The Pydantic model_copy will reject obviously bad types; we catch
    exceptions at the call site.
    """
    if isinstance(old_value, bool):
        # bool first because bool is a subclass of int in Python
        low = text.strip().lower()
        if low in ("true", "yes", "y", "1"):
            return True
        if low in ("false", "no", "n", "0"):
            return False
        return text
    if isinstance(old_value, int):
        # Strip commas — humans often type "60,000".
        cleaned = text.replace(",", "").strip()
        try:
            return int(cleaned)
        except ValueError:
            try:
                # Allow "60.0" -> 60 if the user typed a decimal where
                # an int is expected.
                f = float(cleaned)
                if f.is_integer():
                    return int(f)
            except ValueError:
                pass
            return text  # last resort: let Pydantic validate/reject
    if isinstance(old_value, float):
        cleaned = text.replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return text
    return text

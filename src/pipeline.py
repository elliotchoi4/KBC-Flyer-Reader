"""
Top-level orchestrator.

Threads the five stages together:

    file_path
        |
        v
    [1] detect()                     -> DetectionResult
        |
        v
    [2] extract_text()                -> raw text
        |
        v
    [3] extract_buildings/land()      -> Pydantic-validated records
        |
        v
    [4] normalizers (per-record)      -> cleaned-up records
        |
        v
    [5] write_*_records()             -> .xlsx (aggregates across files)

Batch processing: process_flyers() runs stages 1-3 in parallel across all
input files, then aggregates the records and runs stage 5 once at the end
so all flyers land in a single output file.

Progress reporting
------------------
Every flyer has a fixed budget of 3 progress steps (detect, extract text,
LLM extract). The whole batch is therefore `len(paths) * 3 + 1` steps —
the final +1 being the single Excel write. A thread-safe `_ProgressTracker`
counts steps as they complete and emits a `ProgressEvent` for each one, so
a GUI can drive a determinate progress bar and a detailed log at the same
time. Files that fail partway still consume their full 3-step budget (the
unused steps are flushed silently) so the bar stays accurate.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

from .config import Config
from .detector import DetectionResult, FileKind, detect
from .excel_writer import WriteSummary, write_building_records, write_land_records
from .extractor import extract_text, extract_text_with_pages
from .llm_client import extract_buildings, extract_land, StopRequested
from .schemas import BuildingRecord, LandRecord


log = logging.getLogger("flyer_reader")


SurveyKind = Literal["building", "land"]

# Progress-step budget per flyer. The LLM stage is the long pole, so we
# break it into multiple checkpoints rather than counting it as one step:
#
#   1. Detect       — millisecond-scale; one step
#   2. Extract text — seconds; one step
#   3. Phase 1      — small "count buildings" call; one step
#   4. Phase 2      — main extraction; allotted 5 sub-steps that are
#                     interpolated from streaming token progress
#
# Why 5 sub-steps for Phase 2 specifically: tokens stream in continuously,
# so the bar advances smoothly during the longest part of the run. Five
# is granular enough that a Surface-class user sees the bar move every
# 30-60 seconds during a multi-minute generation, but coarse enough that
# we don't spam a thousand redraw events.
STEPS_PER_FLYER = 4
LLM_STREAMING_SUBSTEPS = 5
# Effective bar capacity per flyer: 4 hard checkpoints + the 5 fractional
# ones that Phase 2 streaming gets to consume. Phase 2's hard "done" step
# uses up the streaming substeps that Phase 2 streaming did NOT spend.
STEP_BUDGET_PER_FLYER = STEPS_PER_FLYER + LLM_STREAMING_SUBSTEPS - 1  # 8


# ---------------------------------------------------------------------------
# Progress events
# ---------------------------------------------------------------------------

@dataclass
class ProgressEvent:
    """
    One unit of progress feedback.

    message   : human-readable log line ("" for a silent bar-only tick)
    completed : steps completed so far
    total     : total steps in the batch
    level     : one of info | stage | success | error | silent | done
                (the GUI uses this to colour the log line)
    """
    message: str
    completed: int = 0
    total: int = 0
    level: str = "info"

    @property
    def fraction(self) -> float:
        """Completed fraction in the range 0.0 - 1.0."""
        if self.total <= 0:
            return 0.0
        return min(1.0, self.completed / self.total)

    @property
    def percent(self) -> int:
        return round(self.fraction * 100)


ProgressCallback = Callable[[ProgressEvent], None]


class _ProgressTracker:
    """Thread-safe step counter shared across parallel flyer workers."""

    def __init__(self, total: float, callback: Optional[ProgressCallback]):
        # Stored as float so we can advance by fractional amounts when a
        # streaming LLM call reports its token-by-token progress mid-step.
        self._total = max(1.0, float(total))
        self._done = 0.0
        # Track the last percentage we emitted to avoid spamming the GUI
        # with thousands of identical-percent redraws during streaming.
        self._last_emitted_pct = -1
        self._lock = threading.Lock()
        self._cb = callback

    def emit(self, message: str, level: str = "info", *,
             advance: float = 0.0) -> None:
        """
        Emit a progress event.

        advance > 0  -> advance the completed-steps counter by that amount.
                        Use 1.0 for a discrete checkpoint, fractions like
                        0.2 for streaming interpolation.
        advance == 0 -> log/status only, bar position unchanged.

        If a fractional advance would not change the displayed integer
        percentage AND no message is attached, the event is suppressed —
        this avoids hundreds of identical redraws during token streaming.
        """
        with self._lock:
            if advance > 0:
                self._done = min(self._total, self._done + advance)
            done = self._done
            pct = int((done / self._total) * 100) if self._total > 0 else 0
            # Decide whether to actually emit. Always emit if there's a
            # message, or if the integer percentage moved.
            if not message and pct == self._last_emitted_pct:
                return
            self._last_emitted_pct = pct

        # Mirror to the standard logger so console runs show everything too.
        if message:
            (log.error if level == "error" else log.info)(message)

        if self._cb:
            # ProgressEvent uses ints; round here for display, but the
            # internal float counter keeps the real precision.
            self._cb(ProgressEvent(message, int(round(done)),
                                   int(round(self._total)), level))


class _FlyerProgress:
    """
    Per-flyer view onto the shared tracker. Guarantees the flyer consumes
    exactly STEP_BUDGET_PER_FLYER worth of bar advancement even if it
    fails early, so the overall bar never gets stuck short of 100%.

    The budget is split between:
      - Hard checkpoints (step) — each advances the bar by 1.0
      - Streaming sub-progress (substep) — fractional advances within
        the LLM phase, smoothed by token count
    """

    def __init__(self, tracker: _ProgressTracker):
        self._tracker = tracker
        self._used = 0.0   # float to allow fractional advances

    def note(self, message: str, level: str = "info") -> None:
        """Log a sub-step message without moving the bar."""
        self._tracker.emit(message, level, advance=0.0)

    def step(self, message: str, level: str = "info") -> None:
        """Log a message and advance the bar by one full checkpoint."""
        self._used += 1.0
        self._tracker.emit(message, level, advance=1.0)

    def substep(self, fraction: float, message: str = "",
                level: str = "info") -> None:
        """
        Advance the bar fractionally inside an unfinished step.

        `fraction` is the cumulative progress within this sub-step phase
        (0.0–1.0); this method computes the delta from the last call and
        emits only that delta. If the consumer goes backwards we silently
        ignore it — bars should only ever move forward.

        The total budget allocated to streaming substeps is
        LLM_STREAMING_SUBSTEPS - 1 (one of the substeps is consumed when
        Phase 2 hits its hard checkpoint).
        """
        substep_budget = float(LLM_STREAMING_SUBSTEPS - 1)
        # How much of the substep budget should be marked complete now.
        target_used_within_substeps = max(0.0, min(1.0, fraction)) * substep_budget
        # We track the "streaming-only" portion separately by computing it
        # from current self._used minus the integer checkpoints used so
        # far. This works because substep() is only called between the
        # checkpoints that bracket Phase 2.
        # Simpler approach: keep a streaming accumulator.
        if not hasattr(self, "_streamed"):
            self._streamed = 0.0
        delta = target_used_within_substeps - self._streamed
        if delta <= 0:
            # Don't move backwards, and don't redraw if no change.
            if message:
                self._tracker.emit(message, level, advance=0.0)
            return
        self._streamed += delta
        self._used += delta
        self._tracker.emit(message, level, advance=delta)

    def flush(self) -> None:
        """Silently consume any unused budget (called once the flyer is done)."""
        remaining = STEP_BUDGET_PER_FLYER - self._used
        if remaining > 0:
            self._used += remaining
            self._tracker.emit("", "silent", advance=remaining)


# ---------------------------------------------------------------------------
# Per-file result envelope
# ---------------------------------------------------------------------------

@dataclass
class FlyerResult:
    """One flyer's processing outcome — success or failure."""
    source_path: Path
    success: bool
    records: list = field(default_factory=list)   # list[BuildingRecord] or list[LandRecord]
    park_name: Optional[str] = None
    is_multi: bool = False
    error: Optional[str] = None
    # Per-page text map (1-indexed page number -> page text). Populated for
    # successful runs; used by the review dialog to attribute extracted
    # fields back to specific pages of the source PDF.
    page_text: dict = field(default_factory=dict)


@dataclass
class ReviewRequest:
    """
    Passed to the review callback after a single flyer finishes extraction
    and BEFORE its records are written to Excel. The callback inspects/edits
    the records and returns a ReviewResponse telling the pipeline what to
    actually write.
    """
    source_path: Path
    survey_kind: str            # "building" or "land"
    records: list               # list[BuildingRecord] or list[LandRecord]
    page_text: dict[int, str]   # 1-indexed page number -> page text
    park_name: Optional[str] = None
    is_multi: bool = False


@dataclass
class ReviewResponse:
    """
    Returned from the review callback. If approved=False, the flyer's
    records are dropped (the Excel write skips them); if approved=True,
    `records` are written. The records may have been edited by the user.
    """
    approved: bool
    records: list = field(default_factory=list)


# Type alias for the callback. The callback is invoked from a worker thread,
# so the implementation is responsible for marshalling onto its own GUI
# thread (e.g. via root.after) and blocking the worker until the user
# decides. May be None (no review — write directly to Excel, legacy behavior).
ReviewCallback = Optional[Callable[[ReviewRequest], ReviewResponse]]


@dataclass
class BatchResult:
    """Aggregated outcome across all flyers in a batch."""
    per_file: list[FlyerResult]
    write_summary: Optional[WriteSummary] = None

    @property
    def total_records(self) -> int:
        return sum(len(r.records) for r in self.per_file)

    @property
    def failures(self) -> list[FlyerResult]:
        return [r for r in self.per_file if not r.success]

    @property
    def successes(self) -> list[FlyerResult]:
        return [r for r in self.per_file if r.success]


# ---------------------------------------------------------------------------
# Single-flyer pipeline (stages 1-3)
# ---------------------------------------------------------------------------

def process_one_flyer(
    path: Path,
    survey_kind: SurveyKind,
    cfg: Config,
    progress: _FlyerProgress,
    stop_event: Optional["threading.Event"] = None,
    extra_instructions: Optional[str] = None,
    on_review: ReviewCallback = None,
) -> FlyerResult:
    """
    Run stages 1-3 for a single flyer. No Excel I/O here.

    If `on_review` is supplied, it is invoked synchronously after a
    successful extraction. The callback returns a ReviewResponse:
      - approved=True  -> the returned records (possibly edited) replace
                          this flyer's records in the final result.
      - approved=False -> this flyer's records are cleared, so process_flyers
                          will skip writing them. The flyer is still marked
                          success=True (extraction worked; the user just
                          chose to skip).

    The callback is called on this worker thread. The implementation is
    responsible for marshalling onto a GUI thread and blocking until the
    user makes a decision.
    """
    name = path.name
    try:
        # If a stop was requested before this flyer even started, skip it
        # cleanly (its step budget is flushed in the finally block).
        if stop_event is not None and stop_event.is_set():
            progress.note(f"[{name}] Skipped — stop requested.")
            return FlyerResult(path, False, error="Cancelled before processing.")
        # --- Stage 1: detect -------------------------------------------------
        progress.note(f"[{name}] Detecting file type...")
        det = detect(path)
        if det.kind == FileKind.UNSUPPORTED:
            progress.step(
                f"[{name}] FAILED — unsupported file: {det.detail or det.mime}",
                "error",
            )
            return FlyerResult(path, False, error=f"Unsupported file: {det.detail or det.mime}")
        kind_label = {
            FileKind.PDF_DIGITAL: "digital PDF",
            FileKind.PDF_SCANNED: "scanned PDF (will OCR)",
            FileKind.IMAGE: "image (will OCR)",
        }.get(det.kind, det.kind.value)
        progress.step(f"[{name}] Detected: {kind_label}")

        # --- Stage 2: extract text ------------------------------------------
        # We use the per-pages variant so we can hand the page map to the
        # review dialog later. The joined text is identical to what the
        # legacy extract_text would have produced, so the LLM call below
        # sees no behavior change.
        progress.note(f"[{name}] Extracting text...")
        text, page_text = extract_text_with_pages(det, cfg)
        char_count = len(text.strip())
        if not char_count:
            progress.step(f"[{name}] FAILED — no text could be extracted", "error")
            return FlyerResult(path, False, error="No text could be extracted from the file.")
        progress.step(f"[{name}] Extracted {char_count:,} characters of text")

        # Checkpoint: bail before the long LLM stage if a stop was requested.
        if stop_event is not None and stop_event.is_set():
            progress.note(f"[{name}] Stopped before LLM extraction.")
            return FlyerResult(path, False, error="Cancelled before extraction.")

        # --- Stage 3: LLM extraction ---------------------------------------
        # For Ollama, this is two phases (count-units, then full extract)
        # with an ETA emitted between them. For Claude, the existing
        # schema-driven two-phase code handles it. Timing recording for
        # Ollama happens inside extract_buildings/extract_land — only
        # successful calls produce a sample, so timeouts / failures cannot
        # poison the running average.
        active_model = (cfg.claude_model if cfg.provider == "claude"
                        else cfg.ollama_model)
        progress.note(f"[{name}] Sending text to {active_model} (this can take a while)...")
        # Relay the LLM client's phase-by-phase status into the log.
        _status = lambda m: progress.note(f"[{name}] {m}")
        # Phase 1 finishes -> hard checkpoint advance.
        _on_checkpoint = lambda m: progress.step(f"[{name}] {m}")
        # Phase 2 streaming -> fractional sub-progress advance.
        _on_stream = lambda frac: progress.substep(frac)
        try:
            if survey_kind == "building":
                result = extract_buildings(text, name, cfg, status=_status,
                                           extra_instructions=extra_instructions,
                                           stop_event=stop_event,
                                           on_checkpoint=_on_checkpoint,
                                           on_stream_progress=_on_stream)
                records = list(result.records)
                is_multi = result.is_multi_building_park
                park_name = result.park_name
            else:
                result = extract_land(text, name, cfg, status=_status,
                                      extra_instructions=extra_instructions,
                                      stop_event=stop_event,
                                      on_checkpoint=_on_checkpoint,
                                      on_stream_progress=_on_stream)
                records = list(result.records)
                is_multi = result.is_multi_parcel
                park_name = result.park_name
        except StopRequested:
            # User cancelled mid-LLM-call. Mark this flyer as cancelled
            # rather than failed, so the batch reports it correctly and
            # the outer pipeline still writes any successfully-completed
            # earlier flyers to the output Excel.
            progress.note(f"[{name}] Cancelled by user mid-extraction.")
            return FlyerResult(path, False,
                               error="Cancelled by user during LLM extraction.")

        # Diagnostics: report how many fields the LLM actually populated.
        # A small model can return the right *shape* (N record objects)
        # but leave every field null — which looks like "success" but
        # produces a blank spreadsheet. Surface that here.
        total_fields = 0
        filled_fields = 0
        for rec in records:
            data = rec.model_dump()
            for key, val in data.items():
                if key == "confidence_notes":
                    continue
                total_fields += 1
                if val is not None and val != "":
                    filled_fields += 1
        if records and filled_fields == 0:
            progress.note(
                f"[{name}] WARNING: {len(records)} record(s) returned but every "
                f"field is empty — the model did not extract any values. The "
                f"flyer text may be too noisy, or the model too small.",
                "error",
            )
        elif records:
            progress.note(
                f"[{name}] Populated {filled_fields}/{total_fields} fields "
                f"across {len(records)} record(s)."
            )
            # Log the first record's non-empty fields so the user can see
            # exactly what was captured.
            first = {k: v for k, v in records[0].model_dump().items()
                     if v not in (None, "") and k != "confidence_notes"}
            preview = ", ".join(f"{k}={v!r}" for k, v in list(first.items())[:6])
            progress.note(f"[{name}] Record 1 sample: {preview}")

        suffix = f" (park: {park_name})" if is_multi and park_name else ""
        progress.step(
            f"[{name}] DONE — {len(records)} record(s) extracted{suffix}",
            "success",
        )

        # Review hook: if a callback was provided, give the user a chance
        # to inspect/edit/skip the records before they hit Excel. The
        # callback is responsible for marshalling onto its GUI thread and
        # BLOCKING this worker thread until the user decides. Any error
        # from the callback is logged but treated as "approved as-is" so
        # the run still produces output rather than silently dropping it.
        if on_review is not None and records:
            try:
                review_req = ReviewRequest(
                    source_path=path,
                    survey_kind=survey_kind,
                    records=list(records),
                    page_text=page_text,
                    park_name=park_name,
                    is_multi=is_multi,
                )
                progress.note(f"[{name}] Awaiting user review...")
                response = on_review(review_req)
                if not response.approved:
                    progress.note(f"[{name}] User chose to skip this flyer.")
                    records = []
                else:
                    edited = list(response.records)
                    if len(edited) != len(records):
                        progress.note(
                            f"[{name}] User adjusted record count: "
                            f"{len(records)} -> {len(edited)}."
                        )
                    records = edited
            except Exception as e:
                log.exception("Review callback failed for %s; "
                              "writing records as-is", path)
                progress.note(
                    f"[{name}] Review dialog failed ({type(e).__name__}); "
                    f"writing records as extracted.",
                    "error",
                )

        return FlyerResult(
            source_path=path,
            success=True,
            records=records,
            park_name=park_name,
            is_multi=is_multi,
            page_text=page_text,
        )

    except Exception as e:
        log.exception("Failed processing %s", path)
        progress.step(f"[{name}] FAILED — {type(e).__name__}: {e}", "error")
        return FlyerResult(path, False, error=f"{type(e).__name__}: {e}")
    finally:
        # Make sure this flyer consumed its full step budget.
        progress.flush()


# ---------------------------------------------------------------------------
# Batch pipeline (stages 1-3 in parallel, stage 5 once at the end)
# ---------------------------------------------------------------------------

def process_flyers(
    paths: list[Path],
    survey_kind: SurveyKind,
    cfg: Config,
    output_dir: Optional[Path] = None,
    on_progress: Optional[ProgressCallback] = None,
    stop_event: Optional["threading.Event"] = None,
    extra_instructions: Optional[str] = None,
    on_review: ReviewCallback = None,
    target_path: Optional[Path] = None,
    output_name: Optional[str] = None,
) -> BatchResult:
    """
    Run the whole pipeline on a batch. Returns a single Excel file containing
    every record from every flyer.

    `on_progress` receives ProgressEvent objects: each carries a log message,
    the completed/total step counts (for a progress bar), and a severity
    level. It is called from worker threads, so a GUI callback must marshal
    back onto the UI thread (e.g. via a queue).

    `stop_event`, if given, is checked between flyers and between pipeline
    stages. When it is set the batch halts as soon as the in-flight work
    reaches a checkpoint, and whatever records were extracted before the
    stop are still written to the output file.

    `target_path`, if given, points at an existing Excel survey file that
    this batch should APPEND to (rather than creating a fresh timestamped
    output). The caller is responsible for ensuring the target's survey
    kind matches the batch's survey_kind — see
    excel_writer.detect_target_survey_kind. When set, output_dir is
    ignored.

    `output_name`, if given, overrides the auto-timestamped filename for
    the fresh-write path. Ignored when target_path is set (append mode
    writes to the existing file's name). Caller is expected to have
    already validated the name (legal characters, .xlsx extension,
    overwrite confirmation).

    `extra_instructions`, if given, is appended to the LLM prompt for
    every flyer in this batch. Used for per-job nuances the user enters
    in the GUI's "Additional instructions" box.

    `on_review`, if given, is invoked after each flyer's LLM stage and
    before that flyer's records are written to Excel. The callback gets
    a chance to edit or drop records (typically via a GUI review dialog).
    See ReviewCallback. When None, records are written as extracted —
    the legacy behavior.
    """
    if not paths:
        return BatchResult(per_file=[])

    # Total bar capacity = per-flyer budget + 1 for the final Excel write.
    # The per-flyer budget includes hard checkpoints (detect / extract /
    # phase1 / phase2) AND interpolation budget for streaming progress
    # during phase 2.
    total_steps = len(paths) * STEP_BUDGET_PER_FLYER + 1
    tracker = _ProgressTracker(total_steps, on_progress)

    workers = max(1, cfg.max_parallel_extractions)
    # Show the model that will actually be used, based on the active
    # provider. Previously this hardcoded cfg.ollama_model, which meant
    # runs against Claude (or any time the Ollama model happened to be
    # the stale default) printed the wrong name. The same provider-aware
    # selection happens per-flyer below — this just brings the batch
    # header line into line with it.
    active_model = (cfg.claude_model if cfg.provider == "claude"
                    else cfg.ollama_model)
    tracker.emit(
        f"Starting batch: {len(paths)} flyer(s) | survey={survey_kind} | "
        f"model={active_model} | parallel={workers}",
        level="stage",
    )

    # When the user wants per-flyer review, we can't run flyers in
    # parallel — the review dialog is modal and the user reviews each
    # one in turn. Drop to one worker so the order is deterministic and
    # the dialogs appear in a sensible sequence.
    if on_review is not None:
        workers = 1

    results: list[FlyerResult] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(
                process_one_flyer, p, survey_kind, cfg,
                _FlyerProgress(tracker), stop_event,
                extra_instructions, on_review,
            ): p
            for p in paths
        }
        for fut in as_completed(futs):
            results.append(fut.result())

    # Order results to match the input order (as_completed scrambles them).
    by_path = {r.source_path: r for r in results}
    results = [by_path[p] for p in paths if p in by_path]

    ok_count = sum(1 for r in results if r.success)
    fail_count = len(results) - ok_count
    tracker.emit(
        f"Extraction phase complete: {ok_count} succeeded, {fail_count} failed.",
        level="stage",
    )

    # Aggregate every successful flyer's records.
    all_records = []
    for r in results:
        if r.success:
            all_records.extend(r.records)

    if not all_records:
        tracker.emit("No records extracted — nothing to write.", level="error",
                     advance=True)
        tracker.emit("Batch finished.", level="done")
        return BatchResult(per_file=results, write_summary=None)

    # --- Stage 5: single Excel write ---------------------------------------
    if target_path is not None:
        tracker.emit(
            f"Appending {len(all_records)} record(s) to {target_path.name}...",
            level="info")
    else:
        tracker.emit(
            f"Writing {len(all_records)} record(s) to the Excel template...",
            level="info")
    if survey_kind == "building":
        summary = write_building_records(all_records, cfg, output_dir,
                                         target_path=target_path,
                                         output_name=output_name)
    else:
        summary = write_land_records(all_records, cfg, output_dir,
                                     target_path=target_path,
                                     output_name=output_name)
    if summary.appended:
        end_row = summary.start_row + summary.rows_written - 1
        tracker.emit(
            f"Appended {summary.rows_written} record(s) to "
            f"{summary.output_path} (rows {summary.start_row}-{end_row}).",
            level="success", advance=True,
        )
    else:
        tracker.emit(f"Saved output file: {summary.output_path}",
                     level="success", advance=True)
    if summary.flagged_cells:
        tracker.emit(
            f"{len(summary.flagged_cells)} cell(s) flagged low-confidence "
            f"(highlighted yellow for review).",
            level="info",
        )

    tracker.emit("Batch finished.", level="done")
    return BatchResult(per_file=results, write_summary=summary)

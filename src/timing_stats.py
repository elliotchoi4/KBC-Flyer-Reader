"""
Per-model extraction timing statistics — token- and building-count-based.

Why this metric
---------------
The slow part of local LLM extraction is generating output tokens (the
flyer's input is read in parallel almost instantly; output is generated one
token at a time). Two facts:

  1. tokens/second is HARDWARE-BOUND and very stable for a given model on
     a given machine.
  2. The number of OUTPUT tokens scales with the number of buildings the
     flyer describes — more buildings means more JSON to emit. Input
     character count is at best a weak proxy for this.

So we track per-model:
  total_tokens     — sum of output tokens across all completed runs
  total_seconds    — sum of seconds spent generating those tokens
  total_buildings  — sum of buildings (or parcels) extracted across runs
  runs             — number of successful samples

Two derived quantities:
  tokens_per_second   = total_tokens   / total_seconds   (hardware speed)
  tokens_per_building = total_tokens   / total_buildings (content density)

Prediction for a new flyer with N buildings:
  expected_tokens  = N * tokens_per_building
  expected_seconds = expected_tokens / tokens_per_second
                   = N * total_seconds / total_buildings

That last form is what `estimate_seconds` actually computes — the tokens
cancel out mathematically. Tracking them separately is still useful for
diagnostics (tokens/sec is a hardware sanity check that you can read off
the stats file at a glance).

Storage strategy
----------------
Per-model aggregate (constant size — three small numbers per model, plus a
bounded ring buffer for the last few rates). The file lives next to the
user config (`%APPDATA%\\FlyerReader\\timing_stats.json` on Windows).

If the file already exists in the older char-based format, we throw it out
and start fresh — the two formats aren't compatible and trying to mix
them would produce garbage predictions.

Edge cases (all handled, all tested)
------------------------------------
- First time a model is used: estimate_seconds returns None.
- Run with no buildings (LLM returned 0 records): we don't pretend; the
  sample is recorded but only contributes to tokens/second, not to
  tokens/building.
- Run where Ollama didn't report eval_count / eval_duration: we still
  record the (None tokens, wall-clock seconds, buildings) sample; the
  prediction still works because the math collapses to seconds-per-building.
- Failed / timed-out runs: NEVER recorded. The caller decides what counts
  as success; this module just stores what it's given.
- Concurrent writes: a process-wide lock serialises read-modify-write.
- Corrupted file: parse errors logged, file treated as empty, next write
  rebuilds it.
- Atomic writes: stage to .tmp, rename over the real file.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

from .config import user_data_dir

log = logging.getLogger("flyer_reader")

# A small ring buffer of recent tokens/sec rates, for diagnostics only.
_RECENT_KEEP = 10

# Process-wide lock around read-modify-write on the stats file.
_LOCK = threading.Lock()

# A magic key that marks this file as the v2 (token-based) format. If a
# file is missing the marker, we assume it's the older char-based format
# and discard it.
_FORMAT_VERSION = "v2-tokens"


def _stats_path() -> Path:
    return user_data_dir() / "timing_stats.json"


def _normalise_model(model_id: str) -> str:
    """Model IDs are case-insensitive and trimmed. Empty -> 'unknown'."""
    s = (model_id or "").strip().lower()
    return s or "unknown"


def _empty_doc() -> dict:
    return {"__format__": _FORMAT_VERSION, "models": {}}


def _load() -> dict:
    """
    Read the stats file. On any error (missing, malformed, wrong format)
    return an empty doc — the caller will just not have an estimate this
    run, and the next successful run will populate fresh stats.
    """
    p = _stats_path()
    if not p.is_file():
        return _empty_doc()
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning("Could not read timing_stats.json (%s); resetting.", e)
        return _empty_doc()

    if not isinstance(data, dict):
        return _empty_doc()

    # If the file lacks the v2 format marker, it's the old chars-based
    # format. Throw it out — mixing schemas would produce garbage estimates.
    if data.get("__format__") != _FORMAT_VERSION:
        log.info("timing_stats.json is in the old format; starting fresh.")
        return _empty_doc()

    if not isinstance(data.get("models"), dict):
        data["models"] = {}
    return data


def _save(data: dict) -> None:
    """Atomic write: stage to .tmp, rename over the real file."""
    p = _stats_path()
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        # Always stamp the format marker so future reads recognise it.
        data["__format__"] = _FORMAT_VERSION
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        tmp.replace(p)
    except Exception as e:
        log.warning("Could not write timing_stats.json: %s", e)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_run(
    model_id: str,
    output_tokens: Optional[int],
    seconds: float,
    n_buildings: int,
) -> None:
    """
    Record one successful main-extraction run.

    Parameters
    ----------
    model_id : str
        e.g. "qwen2.5:3b". Case-insensitive.
    output_tokens : int or None
        Number of tokens the model generated (Ollama's `eval_count`).
        Pass None if the provider does not report this — the run is still
        recorded, and the seconds/building rate is still meaningful for
        future predictions.
    seconds : float
        Time spent generating, in seconds. For Ollama with reported
        timing this is `eval_duration` (ns -> s); otherwise wall-clock.
    n_buildings : int
        Number of records the LLM ultimately produced.

    Invalid samples (seconds <= 0, n_buildings < 0) are silently dropped.
    Never raises — recording stats must not break the surrounding pipeline.
    """
    if seconds is None or seconds <= 0:
        return
    if n_buildings is None or n_buildings < 0:
        return
    model = _normalise_model(model_id)

    with _LOCK:
        doc = _load()
        models = doc["models"]
        entry = models.get(model)
        if not isinstance(entry, dict):
            entry = {"total_tokens": 0, "total_seconds": 0.0,
                     "total_buildings": 0, "runs": 0, "recent_rates": []}

        try:
            entry["total_tokens"]    = int(entry.get("total_tokens", 0)) + (int(output_tokens) if output_tokens else 0)
            entry["total_seconds"]   = float(entry.get("total_seconds", 0.0)) + float(seconds)
            entry["total_buildings"] = int(entry.get("total_buildings", 0)) + int(n_buildings)
            entry["runs"]            = int(entry.get("runs", 0)) + 1
        except (TypeError, ValueError):
            entry = {"total_tokens": int(output_tokens or 0),
                     "total_seconds": float(seconds),
                     "total_buildings": int(n_buildings),
                     "runs": 1, "recent_rates": []}

        # Recent tokens/sec rates, capped — diagnostic only.
        if output_tokens and seconds > 0:
            rate = output_tokens / seconds
            recent = entry.get("recent_rates", [])
            if not isinstance(recent, list):
                recent = []
            recent.append(round(rate, 2))
            entry["recent_rates"] = recent[-_RECENT_KEEP:]

        models[model] = entry
        _save(doc)


def estimate_seconds(model_id: str, n_buildings: int) -> Optional[float]:
    """
    Estimate how long the main extraction will take for `n_buildings`
    units, based on history for this model.

    Returns None if:
      - this model has no usable history yet
      - n_buildings is non-positive
      - the stored stats don't have a positive buildings total (would
        require dividing by zero)

    The formula:
        expected_seconds = n_buildings * (total_seconds / total_buildings)

    This is mathematically equivalent to "tokens_per_building / tokens_per_second"
    but the tokens cancel, so we just track seconds and buildings.
    """
    if n_buildings is None or n_buildings <= 0:
        return None
    model = _normalise_model(model_id)
    doc = _load()
    entry = doc["models"].get(model)
    if not isinstance(entry, dict):
        return None

    try:
        total_seconds = float(entry.get("total_seconds", 0.0))
        total_buildings = int(entry.get("total_buildings", 0))
        runs = int(entry.get("runs", 0))
    except (TypeError, ValueError):
        return None

    if total_seconds <= 0 or total_buildings <= 0 or runs <= 0:
        return None

    return n_buildings * (total_seconds / total_buildings)


def history_summary(model_id: str) -> Optional[dict]:
    """
    Friendly summary for log messages. Returns None if no usable history.
    """
    model = _normalise_model(model_id)
    doc = _load()
    entry = doc["models"].get(model)
    if not isinstance(entry, dict):
        return None
    try:
        runs = int(entry.get("runs", 0))
        total_seconds = float(entry.get("total_seconds", 0.0))
        total_buildings = int(entry.get("total_buildings", 0))
        total_tokens = int(entry.get("total_tokens", 0))
    except (TypeError, ValueError):
        return None
    if runs <= 0:
        return None
    out = {"runs": runs, "total_seconds": total_seconds}
    if total_buildings > 0:
        out["seconds_per_building"] = total_seconds / total_buildings
    if total_tokens > 0 and total_seconds > 0:
        out["tokens_per_second"] = total_tokens / total_seconds
    return out


def format_duration(seconds: float) -> str:
    """Format a seconds count as a friendly 'Xm Ys' string."""
    if seconds is None:
        return "—"
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"

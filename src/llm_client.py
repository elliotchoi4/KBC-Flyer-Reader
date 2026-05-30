"""
Stage 3: Local LLM extraction via Ollama + instructor.

We hit Ollama's OpenAI-compatible endpoint (Ollama exposes one at /v1) and
wrap the client with `instructor`, which enforces the Pydantic response
schema by validating the output and re-prompting on parse failure.

Two-phase extraction
--------------------
Asking a local model to fill a 38-field schema across N buildings in a
single call fails — it returns the right shape but every field is null.
So extraction runs in two phases:

  Phase 1 (segment): one lightweight call identifies how many buildings/
          parcels the flyer describes and labels each.
  Phase 2 (extract): one focused call per building/parcel, each filling
          the full schema for that single unit.

Phase-2 calls run in parallel (bounded by cfg.max_parallel_extractions).
A unit that fails extraction does not sink the others — its slot is just
skipped and logged.

Ollama auto-start
-----------------
If the Ollama HTTP server is not responding when we first need it, we
locate the ollama executable (PATH first, then the Windows/macOS default
install paths) and start it ourselves in the background.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import urllib.request
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional, TypeVar

from .config import Config
from .prompts import (
    BUILDING_SEGMENT_PROMPT,
    LAND_SEGMENT_PROMPT,
    BUILDING_EXTRACT_PROMPT,
    LAND_EXTRACT_PROMPT,
    build_segment_user_prompt,
    build_extract_user_prompt,
)
from .schemas import (
    BuildingExtractionResult,
    LandExtractionResult,
    BuildingSegmentation,
    LandSegmentation,
    BuildingRecord,
    LandRecord,
)


log = logging.getLogger("flyer_reader")
T = TypeVar("T")


# ---------------------------------------------------------------------------
# Ollama server management
# ---------------------------------------------------------------------------

# Default install locations per platform, in priority order.
_OLLAMA_CANDIDATES: dict[str, list[str]] = {
    "win32": [
        r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe",
        r"C:\Program Files\Ollama\ollama.exe",
    ],
    "darwin": [
        "/usr/local/bin/ollama",
        "/opt/homebrew/bin/ollama",
        str(Path.home() / ".ollama" / "ollama"),
    ],
    "linux": [
        "/usr/local/bin/ollama",
        "/usr/bin/ollama",
        str(Path.home() / ".ollama" / "ollama"),
    ],
}


def _find_ollama_exe() -> str | None:
    """
    Return the path to the ollama executable, or None if not found.

    Checks (in order):
      1. 'ollama' on the system PATH (most reliable).
      2. Known default install locations per platform.
    """
    import shutil
    found = shutil.which("ollama")
    if found:
        return found

    candidates = _OLLAMA_CANDIDATES.get(sys.platform, [])
    for raw in candidates:
        # Expand environment variables (e.g. %LOCALAPPDATA%)
        expanded = os.path.expandvars(raw)
        if Path(expanded).is_file():
            return expanded

    return None


def _is_server_up(base_url: str, timeout: float = 2.0) -> bool:
    """Return True if the Ollama HTTP server answers on /api/tags."""
    # Use the native /api/tags endpoint (always present, doesn't need a model).
    tags_url = base_url.rstrip("/").replace("/v1", "") + "/api/tags"
    try:
        urllib.request.urlopen(tags_url, timeout=timeout)
        return True
    except Exception:
        return False


def ensure_ollama_running(cfg: Config, timeout: int = 20) -> tuple[bool, str]:
    """
    Make sure the Ollama server is up. Starts it automatically if not.

    Returns (ok: bool, message: str).

    Strategy:
      1. If the server is already responding → done.
      2. Locate the ollama executable.
      3. Launch `ollama serve` in the background (detached, no console window
         on Windows).
      4. Poll every second for up to `timeout` seconds.
    """
    if _is_server_up(cfg.ollama_base_url):
        log.debug("Ollama server already running.")
        return True, "Ollama server already running."

    log.info("Ollama server not responding — attempting auto-start...")

    exe = _find_ollama_exe()
    if not exe:
        return (
            False,
            "Ollama is not installed or could not be found.\n"
            "Download from https://ollama.com/download and re-run the installer.",
        )

    log.info("Starting Ollama server: %s serve", exe)
    try:
        # On Windows: CREATE_NO_WINDOW keeps the terminal hidden.
        # On Unix: start_new_session=True detaches from the parent process group.
        if sys.platform == "win32":
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                [exe, "serve"],
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                [exe, "serve"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except OSError as e:
        return False, f"Failed to start Ollama: {e}"

    # Poll until the server is up or timeout expires.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        if _is_server_up(cfg.ollama_base_url):
            log.info("Ollama server started successfully.")
            return True, "Ollama server started automatically."

    return (
        False,
        f"Ollama server did not respond within {timeout} seconds after launch.\n"
        "Try opening the Ollama app manually, then retry.",
    )


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------

def _build_client(cfg: Config):
    """
    Construct an instructor-wrapped client for the configured provider.

    provider == "claude":
        Uses the Anthropic API. The API key is read from the claude-key
        text file (see Config.read_claude_key). Claude fills the survey
        schema far more reliably than any local model — this is the
        recommended option when extraction accuracy matters.

    provider == "ollama":
        Uses the local Ollama server via its OpenAI-compatible endpoint.
        Uses JSON_SCHEMA mode for token-level constrained decoding, which
        small local models need to fill a large schema. Falls back to
        plain JSON mode if the installed versions reject JSON_SCHEMA.
    """
    import instructor

    if cfg.provider == "claude":
        from anthropic import Anthropic
        # Raises a clear error if the claude-key file is missing/empty.
        api_key = cfg.read_claude_key()
        base = Anthropic(api_key=api_key)
        return instructor.from_anthropic(base)

    # Default: local Ollama.
    from openai import OpenAI
    base = OpenAI(
        base_url=cfg.ollama_base_url,
        # Ollama ignores the key but the SDK requires a non-empty string.
        api_key="ollama",
    )
    try:
        return instructor.from_openai(base, mode=instructor.Mode.JSON_SCHEMA)
    except Exception as e:
        log.warning("JSON_SCHEMA mode unavailable (%s); falling back to JSON mode.", e)
        return instructor.from_openai(base, mode=instructor.Mode.JSON)


def _active_model(cfg: Config) -> str:
    """Return the model string for the configured provider."""
    return cfg.claude_model if cfg.provider == "claude" else cfg.ollama_model


# A generous output budget for one extraction call. One filled survey
# record is well under this; the segmentation result is tiny.
_MAX_OUTPUT_TOKENS = 4096


def _create(client, cfg: Config, *, messages, response_model):
    """
    Provider-aware wrapper around instructor's chat.completions.create.

    The Anthropic API REQUIRES max_tokens on every request; the OpenAI
    API treats it as optional. Passing it unconditionally is safe for
    both, so we always include it — this avoids the
    "Missing required arguments" error when provider == claude.
    """
    return client.chat.completions.create(
        model=_active_model(cfg),
        messages=messages,
        response_model=response_model,
        max_retries=3,
        temperature=0.1,
        max_tokens=_MAX_OUTPUT_TOKENS,
    )


def _prepare_provider(cfg: Config, status: "StatusCallback" = None) -> None:
    """
    Provider-specific pre-flight. For Ollama this auto-starts the local
    server; for Claude it validates that the key file exists and is
    non-empty (reading it now gives a clear error before any API call).
    """
    if cfg.provider == "claude":
        # Touch the key file early so a missing key fails fast and clearly.
        cfg.read_claude_key()
        _say(status, f"Using Claude API ({cfg.claude_model}).")
        return
    ok, msg = ensure_ollama_running(cfg)
    if not ok:
        raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# A small status callback so the pipeline can log phase-by-phase progress.
# ---------------------------------------------------------------------------
StatusCallback = Optional[Callable[[str], None]]
# Callback fired when a hard progress checkpoint is reached (Phase 1 done,
# Phase 2 done, etc.). Receives a message string. Distinct from
# StatusCallback because checkpoints should also advance the progress bar.
CheckpointCallback = Optional[Callable[[str], None]]
# Callback fired during streaming generation with a cumulative 0.0–1.0
# fraction representing how far through generation we are. Used to
# interpolate the progress bar smoothly during the long LLM phase.
StreamProgressCallback = Optional[Callable[[float], None]]


def _say(cb: StatusCallback, msg: str) -> None:
    if cb:
        cb(msg)
    log.info(msg)


def _checkpoint(cb: CheckpointCallback, msg: str) -> None:
    """Notify a hard-checkpoint callback. Falls back to logging only."""
    if cb:
        cb(msg)
    log.info(msg)


# Words that mark a label as a PARK/campus name rather than a single
# building. Used to drop phantom "buildings" the segmenter sometimes adds.
_PARK_WORDS = (
    "logistics center", "logistics park", "business park", "industrial park",
    "commerce center", "commerce park", "distribution center", "campus",
    "master-planned", "master planned", "trade center", "logistics centre",
)


def _looks_like_building_label(label: str) -> bool:
    """
    True if a segmentation label looks like an individual building rather
    than the overall park/campus.

    A real building label is short and usually contains 'building', a bare
    number, or a unit-style token (A, B, Phase II...). A park label is
    longer and contains park/campus vocabulary.
    """
    low = label.lower().strip()
    if not low:
        return False
    # Explicit park vocabulary => not a building.
    if any(w in low for w in _PARK_WORDS):
        return False
    # Strong positive signal: the word "building" / "bldg" / "unit" / "suite".
    if any(t in low for t in ("building", "bldg", "unit", "suite", "phase")):
        return True
    # A short label with a number is probably a building ("2", "Lot 3").
    if len(label) <= 20 and any(ch.isdigit() for ch in low):
        return True
    # Long, wordy labels with no building token are most likely a park name.
    if len(label.split()) >= 3:
        return False
    # Default: keep it — better to over-extract than silently drop.
    return True


def _filter_building_labels(
    labels: list[str], park_name: Optional[str]
) -> tuple[list[str], list[str]]:
    """
    Drop labels that are clearly the park name rather than a building.
    Returns (kept_labels, dropped_labels). Never returns an empty kept
    list — if everything looks park-like, keep the original list so the
    flyer still produces at least one record.
    """
    kept, dropped = [], []
    for lbl in labels:
        # Drop if it matches the detected park name, or looks park-like.
        if (park_name and lbl.strip().lower() == park_name.strip().lower()) \
                or not _looks_like_building_label(lbl):
            dropped.append(lbl)
        else:
            kept.append(lbl)
    if not kept:
        return labels, []
    return kept, dropped


# ---------------------------------------------------------------------------
# Ollama "flat-array" extraction path
#
# A separate, simpler extraction pipeline used ONLY when the provider is
# Ollama (a local small model). Claude continues to use the two-phase
# schema-based path below — it handles complex schemas fine. Small local
# models do not: they see a 38-field Pydantic schema, produce the correct
# empty skeleton, and run out of capacity for values. This path sidesteps
# that by:
#
#   1. Using a simple flat-array JSON prompt: "return a JSON array, each
#      object has these fields, here is the flyer text." No nested types,
#      no Pydantic schema injection.
#   2. One single call per flyer rather than (segment + N per-building).
#      Less inference time, less chance of an empty result.
#   3. Tolerant parsing — accept array OR single object OR wrapper, strip
#      code fences, fall back to regex JSON finder.
#   4. Per-field cleanup — strings → numbers, "N/A" → null, range
#      handling. If one field is malformed, just that one field is dropped
#      rather than the whole record.
#
# This is the approach the previous version of the extractor used
# successfully on the same small models.
# ---------------------------------------------------------------------------

# Field documentation pairs (name, short description) — used to build the prompt.
# The descriptions list common flyer terminology so the LLM connects what the
# flyer actually says to our schema field name. Kept terse on purpose: long
# descriptions dilute the multi-building rule and other top-level instructions.
_BUILDING_FIELDS_DOC: list[tuple[str, str]] = [
    ("property_name",            "Building name. For one building in a multi-building park, use '<Park> - Bldg <N>'."),
    ("address",                  "Street address"),
    ("city",                     "City"),
    ("state",                    "2-letter state code"),
    ("zip_code",                 "ZIP code"),
    ("property_owner",           "Owner / developer / landlord name"),
    ("total_building_area_sf",   "This building's total SF (matches 'Building Size', 'Total Size', 'SF', 'GBA'). Bare number."),
    ("available_space_sf",       "Available SF for this building. Bare number."),
    ("office_space_sf",          "Office portion SF (matches 'Office', 'Office Space'). Number, or 'BTS' if build-to-suit."),
    ("land_area_acres",          "Site acreage for this building (matches 'Site', 'Acres'). Bare number."),
    ("year_built",               "Integer year"),
    ("building_status",          "Existing / Planned/Proposed / Under Construction / Demolished"),
    ("building_type",            "Spec / BTS"),
    ("occupancy_status",         "Occupied / Vacant. Only set this if the flyer says so. 'Available For Lease' / 'For Lease' / 'Available' with no tenant named => Vacant. If the flyer doesn't address occupancy at all, leave NULL — do NOT default to Occupied."),
    ("tenancy_type",             "Single-Tenant / Multi-Tenant / Owner Occupied"),
    ("current_tenant",           "Tenant name if named"),
    ("date_available",           "When available (matches 'Delivering', 'Available'). If the flyer gives a quarter like 'Q2 2027', convert to the middle month of that quarter: Q1->2/1/yyyy, Q2->5/1/yyyy, Q3->8/1/yyyy, Q4->11/1/yyyy. Otherwise keep the flyer's wording (e.g. '2027-04-01')."),
    ("sprinkler_type",           "ESFR / Wet / Dry / None / Other"),
    ("current_zoning",           "Zoning code"),
    ("clear_height_min_ft",      "Min clear height in feet (matches 'Clear Height'). Number."),
    ("clear_height_max_ft",      "Max clear height in feet. Number."),
    ("load_type",                "Cross-dock / Front Load / Rear Load / Other / L-shape. Only set this if the flyer or site plan explicitly says so (e.g. 'Cross-Dock Configuration'). If load type is not mentioned, leave NULL — do NOT default to Front Load."),
    ("dock_doors",               "Dock door count (matches 'Dock Doors', 'Dock High'). Integer."),
    ("grade_level_doors",        "Drive-in / grade-level door count (matches 'Drive-in', 'Ramps'). Integer."),
    ("column_spacing_width_ft",  "Column spacing width in ft (first number in e.g. \"60' x 56'\")."),
    ("column_spacing_depth_ft",  "Column spacing depth in ft (second number in e.g. \"60' x 56'\")."),
    ("auto_parking_spaces",      "Car parking count (matches 'Car Parking', 'Auto Parking', 'Parking'). Integer."),
    ("trailer_parking_spaces",   "Trailer parking count (matches 'Trailer Parking', 'Trailer Stalls'). Integer."),
    ("existing_power",           "Power spec as written (matches 'Electrical', 'Power'). Full string."),
    ("sale_lease",               "Sale / Lease / Sale/Lease"),
    ("annual_asking_rate_psf",   "Asking rate $/SF/year. Bare number."),
    ("sale_price",               "Total sale price $. Bare number."),
    ("estimated_annual_opex_psf", "OpEx $/SF/year. Bare number."),
    ("comments",                 "Brief notes / highlights"),
]

_LAND_FIELDS_DOC: list[tuple[str, str]] = [
    ("property_name",            "Site name. For one parcel in a multi-parcel project, use '<Project> - Parcel <N>'."),
    ("address",                  "Address or cross-streets"),
    ("city",                     "City"),
    ("state",                    "2-letter state code"),
    ("zip_code",                 "ZIP code"),
    ("property_owner",           "Owner / developer / landlord"),
    ("land_area_acres",          "Acres for this parcel. Bare number."),
    ("current_zoning",           "Zoning code"),
    ("land_construction_status", "Raw Land / Site Graded / Utilities to Site / Entitled / Entitlements in Progress / Paved/Parking"),
    ("existing_power",           "Power availability (full string)"),
    ("sale_lease",               "Sale / Lease / Sale/Lease"),
    ("sale_price",               "Total sale price $. Bare number."),
    ("comments",                 "Brief notes / highlights"),
]

# Closed-set normalisation: maps freeform LLM output to the locked Excel value.
_CLOSED_SETS: dict[str, dict[str, str]] = {
    "building_status": {
        "existing": "Existing", "exist": "Existing",
        "planned": "Planned/Proposed", "proposed": "Planned/Proposed",
        "planned/proposed": "Planned/Proposed", "planned proposed": "Planned/Proposed",
        "under construction": "Under Construction", "construction": "Under Construction",
        "demolished": "Demolished", "demoed": "Demolished",
    },
    "building_type": {
        "spec": "Spec", "speculative": "Spec",
        "bts": "BTS", "build-to-suit": "BTS", "build to suit": "BTS",
    },
    "occupancy_status": {
        "occupied": "Occupied", "vacant": "Vacant",
    },
    "tenancy_type": {
        "single": "Single-Tenant", "single-tenant": "Single-Tenant", "single tenant": "Single-Tenant",
        "multi": "Multi-Tenant", "multi-tenant": "Multi-Tenant", "multi tenant": "Multi-Tenant",
        "owner occupied": "Owner Occupied", "owner-occupied": "Owner Occupied",
    },
    "sprinkler_type": {
        "esfr": "ESFR", "wet": "Wet", "dry": "Dry", "none": "None",
        "other": "Other",
    },
    "load_type": {
        "cross-dock": "Cross-dock", "cross dock": "Cross-dock", "crossdock": "Cross-dock",
        "front load": "Front Load", "front-load": "Front Load",
        "rear load": "Rear Load", "rear-load": "Rear Load",
        "l-shape": "L-shape", "l shape": "L-shape",
        "other": "Other",
    },
    "sale_lease": {
        "sale": "Sale", "lease": "Lease", "for lease": "Lease", "for sale": "Sale",
        "sale or lease": "Sale/Lease", "sale/lease": "Sale/Lease",
        "lease/sale": "Sale/Lease", "sale or for lease": "Sale/Lease",
    },
    "land_construction_status": {
        "raw land": "Raw Land", "raw": "Raw Land",
        "site graded": "Site Graded", "graded": "Site Graded",
        "utilities to site": "Utilities to Site", "utilities": "Utilities to Site",
        "entitled": "Entitled",
        "entitlements in progress": "Entitlements in Progress",
        "paved": "Paved/Parking", "paved/parking": "Paved/Parking", "parking": "Paved/Parking",
        "pad ready": "Site Graded", "fully improved": "Utilities to Site",
    },
}

# Values that mean "no data" — normalised to None.
_NULL_MARKERS: set[str] = {
    "", "n/a", "na", "none", "null", "tbd", "tba",
    "unknown", "?", "-", "--", "—", "call broker", "call for info",
    "call", "see broker", "see flyer", "contact broker", "inquire",
    "not specified", "not stated",
}


# Quarter-of-year shorthand the model often returns for date_available
# (e.g. "Q2 2027", "2Q27", "second quarter 2027"). Convention is to use
# the middle month of each quarter (M/1/YYYY):
#   Q1 -> 2/1/YYYY   Q2 -> 5/1/YYYY   Q3 -> 8/1/YYYY   Q4 -> 11/1/YYYY
# Returns None if the input doesn't look like a quarter reference.
_QUARTER_MIDDLE_MONTH: dict[int, int] = {1: 2, 2: 5, 3: 8, 4: 11}


def _normalize_quarter_date(raw: str) -> Optional[str]:
    """
    If `raw` is a quarter-of-year reference, return the canonical
    mid-quarter date as 'M/1/YYYY'. Otherwise return None and let the
    caller keep the original string.

    Accepted forms (case-insensitive, whitespace-tolerant):
      "Q2 2027"   "Q2-2027"   "Q2/2027"
      "2Q 2027"   "2Q-2027"   "2q27"
      "Q2 '27"    (2-digit year — assumes 20xx)
      "second quarter 2027" / "1st quarter 2027" etc.
    """
    import re as _re
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()

    # Pattern 1: "Q2 2027", "Q2-2027", "Q2/2027", "Q2 '27"
    m = _re.match(r"q\s*([1-4])\s*[-/ ]?\s*'?(\d{2}|\d{4})$", s)
    if m:
        q = int(m.group(1)); y = int(m.group(2))
    else:
        # Pattern 2: "2Q 2027", "2Q27", "2Q-27"
        m = _re.match(r"([1-4])\s*q\s*[-/ ]?\s*'?(\d{2}|\d{4})$", s)
        if m:
            q = int(m.group(1)); y = int(m.group(2))
        else:
            # Pattern 3: "first/second/third/fourth quarter 2027"
            word_to_q = {"first": 1, "1st": 1, "second": 2, "2nd": 2,
                         "third": 3, "3rd": 3, "fourth": 4, "4th": 4}
            m = _re.match(
                r"(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\s+(\d{2}|\d{4})$",
                s,
            )
            if not m:
                return None
            q = word_to_q[m.group(1)]
            y = int(m.group(2))

    # 2-digit year — assume 20xx (real estate flyers don't reference
    # past or far-future centuries).
    if y < 100:
        y += 2000

    month = _QUARTER_MIDDLE_MONTH[q]
    return f"{month}/1/{y}"


# Field name aliases the LLM tends to emit instead of our schema names.
# When the model writes back e.g. {"office": 6245}, we map that to office_space_sf.
# This is the safety net for when the prompt doesn't fully steer the LLM.
_FIELD_ALIASES: dict[str, str] = {
    # land_area
    "land_area_acreage": "land_area_acres",
    "acreage": "land_area_acres",
    "acres": "land_area_acres",
    "site_acres": "land_area_acres",
    "site_size": "land_area_acres",
    "site": "land_area_acres",
    # sale_lease
    "sale_or_lease": "sale_lease",
    "sale_lease_type": "sale_lease",
    "for_sale_or_lease": "sale_lease",
    "lease_or_sale": "sale_lease",
    "transaction_type": "sale_lease",
    "listing_type": "sale_lease",
    # total_building_area
    "size_sf": "total_building_area_sf",
    "building_size_sf": "total_building_area_sf",
    "building_size": "total_building_area_sf",
    "total_sf": "total_building_area_sf",
    "total_size": "total_building_area_sf",
    "total_size_sf": "total_building_area_sf",
    "gba": "total_building_area_sf",
    "gross_building_area": "total_building_area_sf",
    "square_footage": "total_building_area_sf",
    # available_space
    "available_sf": "available_space_sf",
    "available": "available_space_sf",
    "divisible_to": "available_space_sf",
    "divisible_to_sf": "available_space_sf",
    # office_space
    "office_sf": "office_space_sf",
    "office": "office_space_sf",
    "office_size": "office_space_sf",
    "office_size_sf": "office_space_sf",
    "office_buildout": "office_space_sf",
    "office_build_out": "office_space_sf",
    # clear_height
    "clear_height_ft": "clear_height_max_ft",
    "clear_height": "clear_height_max_ft",
    "ceiling_height": "clear_height_max_ft",
    "min_clear_height": "clear_height_min_ft",
    "max_clear_height": "clear_height_max_ft",
    "clear_height_min": "clear_height_min_ft",
    "clear_height_max": "clear_height_max_ft",
    # dock_doors
    "dock_high_doors": "dock_doors",
    "dock_door_count": "dock_doors",
    "dock_high": "dock_doors",
    "loading_doors": "dock_doors",
    # grade_level_doors
    "drive_in_doors": "grade_level_doors",
    "drive-in_doors": "grade_level_doors",
    "drivein_doors": "grade_level_doors",
    "drive_in_door_count": "grade_level_doors",
    "ramps": "grade_level_doors",
    "ramp_doors": "grade_level_doors",
    "grade_doors": "grade_level_doors",
    # parking
    "car_parking": "auto_parking_spaces",
    "car_parking_spaces": "auto_parking_spaces",
    "car_spaces": "auto_parking_spaces",
    "auto_parking": "auto_parking_spaces",
    "auto_spaces": "auto_parking_spaces",
    "parking": "auto_parking_spaces",
    "parking_spaces": "auto_parking_spaces",
    "trailer_spaces": "trailer_parking_spaces",
    "trailer_stalls": "trailer_parking_spaces",
    "trailer_parking": "trailer_parking_spaces",
    # column_spacing
    "column_spacing": "column_spacing_width_ft",
    "typical_bay": "column_spacing_width_ft",
    # owner
    "owner": "property_owner",
    "developer": "property_owner",
    "land_owner": "property_owner",
    "landlord": "property_owner",
    # power
    "electrical": "existing_power",
    "power": "existing_power",
    "amps": "existing_power",
    # status
    "status": "building_status",
    "construction_status": "building_status",
    # type
    "type": "building_type",
    "product_type": "building_type",
    # rate
    "asking_rate": "annual_asking_rate_psf",
    "asking_rent": "annual_asking_rate_psf",
    "rental_rate": "annual_asking_rate_psf",
    "rent": "annual_asking_rate_psf",
    "rate_psf": "annual_asking_rate_psf",
    "opex": "estimated_annual_opex_psf",
    "operating_expenses": "estimated_annual_opex_psf",
    "nnn": "estimated_annual_opex_psf",
    # date
    "available_date": "date_available",
    "delivery_date": "date_available",
    "delivering": "date_available",
    "availability": "date_available",
    "occupancy_date": "date_available",
}


# Max length of free-form user instructions. Long enough for a few
# sentences; short enough to keep the prompt small and predictable. Anything
# longer is truncated with a notice in the log so the user knows.
_EXTRA_INSTRUCTIONS_MAX_CHARS = 2000


def _normalize_extra_instructions(extra: Optional[str]) -> str:
    """
    Clean and length-cap the user's free-form 'additional instructions'.

    Returns "" if there is nothing usable. Whitespace-only / None input
    yields "" so callers can safely test `if normalised:` to decide
    whether to inject the instructions block at all.
    """
    if not extra:
        return ""
    s = extra.strip()
    if not s:
        return ""
    if len(s) > _EXTRA_INSTRUCTIONS_MAX_CHARS:
        log.info("Truncating user extra-instructions from %d to %d chars.",
                 len(s), _EXTRA_INSTRUCTIONS_MAX_CHARS)
        s = s[:_EXTRA_INSTRUCTIONS_MAX_CHARS].rstrip() + " [truncated]"
    return s


def _extra_instructions_block(extra: Optional[str]) -> str:
    """
    Format the user's extra instructions as a clearly-labelled prompt
    section, or empty string if none. The label makes it obvious to the
    LLM that this is the human operator's request, distinct from the
    system rules. Returns text with a trailing newline so it can be
    safely concatenated into any prompt; returns "" if no extras.
    """
    norm = _normalize_extra_instructions(extra)
    if not norm:
        return ""
    return (
        "USER INSTRUCTIONS (follow these in addition to the rules above; "
        "they may override default behaviour like which units to include "
        "or how to format values, but they MUST NOT change the JSON "
        "output format):\n"
        f"{norm}\n\n"
    )


def _build_flat_prompt(
    survey_kind: str,
    flyer_text: str,
    extra_instructions: Optional[str] = None,
) -> str:
    """
    Build the simple flat-array prompt used for the Ollama provider.

    Layout, in order:
      1. STEP 1 — count the units (buildings or parcels) on the flyer.
         This goes FIRST so the model addresses it consciously instead of
         skimming past one buried sentence and defaulting to one record.
      2. STEP 2 — for EACH unit emit one object.
      3. Field list (short descriptions with the relevant flyer
         terminology).
      4. Worked examples of single-unit vs multi-unit output.
      5. (Optional) the user's free-form extra instructions.
      6. The flyer text.

    The user instructions sit AFTER the field list / examples but BEFORE
    the flyer text. That way the model has the schema and shape rules in
    its head before reading the user's nuance, and the user's nuance is
    the freshest thing in context when it starts to extract.
    """
    fields = _BUILDING_FIELDS_DOC if survey_kind == "building" else _LAND_FIELDS_DOC
    unit = "building" if survey_kind == "building" else "parcel"
    units = "buildings" if survey_kind == "building" else "parcels"
    field_doc = "\n".join(f'  "{n}": <{desc}>' for n, desc in fields)
    extras_block = _extra_instructions_block(extra_instructions)

    return f"""You extract real-estate flyer data into a JSON array.

STEP 1 — Count the {units}.
Read the flyer and count how many DISTINCT {units} it markets. A flyer that \
lists separate specs for 'Bldg 1', 'Bldg 2', 'Bldg 3', etc. has multiple \
{units}. A flyer that markets a single building inside a larger park has ONE \
{unit} (the park itself is NOT a {unit}). State your count silently and use \
it in STEP 2.

STEP 2 — Output ONE JSON object per {unit}.
- If you counted 1 {unit}: output an array with 1 object.
- If you counted N {units} (N > 1): output an array with N objects, one per \
{unit}. Use each {unit}'s OWN specs (its own size, its own dock count, etc.), \
not the park total.

Output the JSON array only. No markdown fences, no prose before or after, no \
trailing explanation. Start with [ and end with ].

FIELDS (each {unit} object has these keys):
{{
{field_doc}
}}

EXAMPLES OF THE SHAPE:

Single-{unit} flyer:
[{{ "property_name": "Acme Industrial Park - {unit.title()} 1", "city": "Reno", "state": "NV", ... }}]

Multi-{unit} flyer (3 {units}):
[
  {{ "property_name": "Acme Park - {unit.title()} 1", "city": "Reno", "state": "NV", "total_building_area_sf": 250000, ... }},
  {{ "property_name": "Acme Park - {unit.title()} 2", "city": "Reno", "state": "NV", "total_building_area_sf": 400000, ... }},
  {{ "property_name": "Acme Park - {unit.title()} 3", "city": "Reno", "state": "NV", "total_building_area_sf": 180000, ... }}
]

RULES:
- LEAVE A FIELD AS null IF THE FLYER DOES NOT STATE IT. Do not guess, do not fill in a "typical" value, do not pick the most common option as a default. If the flyer is silent about occupancy, load type, sprinkler, zoning, year built, etc., that field MUST be null in your output.
- After leaving unmentioned fields as null, DO fill in every field the flyer does state or clearly implies. Pull values out of tables, captions, headers, and site-plan callouts — anywhere on the flyer.
- Numbers: bare digits only — write 500000, not "500,000 SF" or "$500K".
- Each object stands alone. Park-wide facts (city, state, owner) apply to every {unit}, so repeat them in each object.

{extras_block}FLYER TEXT:
---
{flyer_text}
---

JSON array:"""


class StopRequested(Exception):
    """Raised when the user clicked Stop during an in-flight LLM call."""
    pass


def _ollama_chat(
    prompt: str,
    cfg: Config,
    stop_event: Optional["threading.Event"] = None,
    on_stream_progress: StreamProgressCallback = None,
    expected_output_tokens: Optional[int] = None,
) -> tuple:
    """
    Call Ollama's /api/chat with streaming, returning when generation
    completes (or when stop_event fires).

    Progress callback
    -----------------
    `on_stream_progress(fraction)` is invoked periodically during
    generation with a cumulative 0.0-1.0 fraction. It is called both
    DURING prompt evaluation (the long initial wait before any tokens
    arrive) and DURING token generation:

      - Before the first token: progress nudges very slowly toward ~10%
        based on elapsed time, just enough that the bar doesn't look
        frozen during the multi-minute prompt-eval phase.
      - After the first token: progress = tokens_received / expected,
        where `expected` is the caller's estimate (typically from
        timing_stats.history). If no estimate is provided we fall back
        to a conservative cap.

    `expected_output_tokens` lets the caller (Phase 2 in particular)
    pass its own forecast of output token count, so the bar tracks the
    real expected total rather than the worst-case `num_predict`.

    Cancellation strategy
    ---------------------
    Stop responsiveness has two regimes that need different handling:

      1. AFTER first token: we iterate streamed JSON frames; checking
         stop_event between frames gives sub-second latency since one
         frame arrives every ~70-200ms on small models.

      2. BEFORE first token (prompt evaluation): Ollama's CPU is busy
         processing the entire prompt to build its KV cache. On a Surface
         this can take 30-90 seconds for a long flyer, during which the
         HTTP stream emits nothing. The frame-by-frame check is useless
         here — `for raw_line in resp` is blocked waiting for the first
         chunk.

    To cover both regimes, a background watchdog thread polls stop_event
    every 100ms and forcibly closes the HTTP response when set. That
    aborts the in-progress read (whether we're waiting for prompt-eval
    or between tokens), the for-loop sees the connection drop, and
    Ollama's server sees the disconnect and abandons the model run.

    Returns (content, eval_count, eval_seconds).
    """
    import urllib.request

    base = cfg.ollama_base_url.rstrip("/").rsplit("/", 1)[0]  # strip "/v1"
    payload = json.dumps({
        "model": cfg.ollama_model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,                # KEY: stream so we can poll stop_event
        "keep_alive": "30m",
        "options": {
            "temperature": 0.1,
            "num_predict": 4096,
            "num_ctx": 16384,
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    # If stop was already requested before we even built the request, bail.
    if stop_event is not None and stop_event.is_set():
        raise StopRequested("Cancelled before LLM call.")

    parts: list[str] = []
    eval_count: Optional[int] = None
    eval_seconds: Optional[float] = None

    resp = urllib.request.urlopen(req, timeout=cfg.ollama_timeout_seconds)

    # Reach down through urllib's wrappers to find the underlying socket.
    # We need this to interrupt blocked reads from another thread —
    # resp.close() alone does NOT interrupt an in-progress read, but
    # socket.shutdown() does (it triggers EOF immediately on any blocked
    # read). This matters for cancellation during Ollama's prompt-eval
    # phase, when the server is busy processing the prompt and has not
    # yet sent any data, so the main thread is blocked inside the kernel
    # waiting for bytes.
    def _find_underlying_socket(obj, seen=None, depth=0):
        import socket as _sock_mod
        if seen is None:
            seen = set()
        if depth > 6 or id(obj) in seen:
            return None
        seen.add(id(obj))
        if isinstance(obj, _sock_mod.socket):
            return obj
        for attr in ("_sock", "sock", "socket", "raw", "fp"):
            sub = getattr(obj, attr, None)
            if sub is not None and sub is not obj:
                found = _find_underlying_socket(sub, seen, depth + 1)
                if found is not None:
                    return found
        return None

    underlying_sock = _find_underlying_socket(resp)

    # Background watchdog: polls stop_event every 100ms. If it fires,
    # call socket.shutdown(SHUT_RDWR) on the underlying socket — this
    # interrupts ANY in-progress read on the main thread, including the
    # long wait during Ollama's prompt-eval phase before the first
    # token has been emitted. Falls back to resp.close() if we couldn't
    # locate the raw socket.
    #
    # The same thread also emits time-based progress nudges during the
    # prompt-eval phase (before the first token arrives), so the GUI
    # progress bar doesn't sit completely frozen during the multi-minute
    # initial wait. The fraction asymptotes toward ~0.10 — once tokens
    # start flowing the main thread takes over with token-based progress.
    import threading as _t
    import socket as _socket_mod
    import time as _time
    watchdog_done = _t.Event()
    watchdog_cancelled = _t.Event()  # set when we actually tore down the conn
    first_token_seen = _t.Event()

    def _watchdog():
        wd_start = _time.monotonic()
        last_progress_emit = 0.0
        while not watchdog_done.is_set():
            if stop_event is not None and stop_event.is_set():
                watchdog_cancelled.set()
                # socket.shutdown forces an immediate EOF on the blocked
                # read; resp.close() alone is not enough on POSIX/Windows.
                if underlying_sock is not None:
                    try:
                        underlying_sock.shutdown(_socket_mod.SHUT_RDWR)
                    except Exception:
                        pass
                try:
                    resp.close()
                except Exception:
                    pass
                return
            # Pre-token progress nudge: asymptotic curve toward ~10% so the
            # bar visibly moves during prompt-eval without overrunning the
            # eventual token-based progress. Throttled to ~2 emits/sec so
            # we don't spam the GUI redraw queue.
            now = _time.monotonic()
            if (on_stream_progress is not None
                    and not first_token_seen.is_set()
                    and (now - last_progress_emit) >= 0.5):
                elapsed = now - wd_start
                # 1 - exp(-elapsed / 60) -> half-life ~42s, max 0.10
                import math as _math
                frac = 0.10 * (1.0 - _math.exp(-elapsed / 60.0))
                try:
                    on_stream_progress(frac)
                except Exception:
                    pass  # progress reporting must never break the LLM call
                last_progress_emit = now
            # Short poll interval — sub-second stop response, negligible CPU.
            watchdog_done.wait(timeout=0.1)

    wd_thread = (_t.Thread(target=_watchdog, daemon=True)
                 if (stop_event is not None or on_stream_progress is not None)
                 else None)
    if wd_thread is not None:
        wd_thread.start()

    try:
        # Iterate streamed JSON lines. Each line is one frame from Ollama.
        # If the watchdog has closed the connection, this for-loop will
        # raise (ConnectionResetError, ValueError on closed file, etc.) —
        # we catch that and convert it to StopRequested.
        #
        # Progress model during streaming:
        #   - Prompt-eval phase (before any tokens): watchdog emits a
        #     slow asymptotic curve up to ~10%.
        #   - Token-generation phase: each frame is roughly one token.
        #     We map the cumulative token count to a 0.10 - 0.95 range
        #     so the bar moves visibly with each token but doesn't
        #     reach 100% until generation actually completes.
        #
        # The expected token count is the caller's estimate; if absent
        # we fall back to num_predict / 2 (assumes the model usually
        # doesn't emit the absolute maximum).
        expected = expected_output_tokens if (
            expected_output_tokens and expected_output_tokens > 0
        ) else 2048  # half of num_predict=4096
        last_token_progress_emit = 0.0
        TOKEN_PROGRESS_EMIT_INTERVAL = 0.5  # seconds

        try:
            for raw_line in resp:
                if stop_event is not None and stop_event.is_set():
                    raise StopRequested("Ollama generation cancelled by user.")

                if not raw_line:
                    continue
                try:
                    frame = json.loads(raw_line)
                except json.JSONDecodeError:
                    # Ollama sends one JSON object per line; if a line was
                    # malformed (very unusual), skip it rather than aborting.
                    continue

                msg = frame.get("message") or {}
                piece = msg.get("content", "")
                if piece:
                    parts.append(piece)
                    # First-token transition: tell the watchdog to stop
                    # emitting time-based pre-token progress.
                    if not first_token_seen.is_set():
                        first_token_seen.set()
                    # Token-based progress, throttled so we don't redraw
                    # the GUI on every single token.
                    if on_stream_progress is not None:
                        now = _time.monotonic()
                        if (now - last_token_progress_emit
                                ) >= TOKEN_PROGRESS_EMIT_INTERVAL:
                            tokens_so_far = len(parts)
                            # Map tokens to 0.10 - 0.95 fraction range.
                            frac = 0.10 + (0.85 * min(
                                1.0, tokens_so_far / float(expected)))
                            try:
                                on_stream_progress(frac)
                            except Exception:
                                pass
                            last_token_progress_emit = now

                if frame.get("done"):
                    ec = frame.get("eval_count")
                    ed = frame.get("eval_duration")
                    try:
                        eval_count = int(ec) if ec is not None else None
                    except (TypeError, ValueError):
                        eval_count = None
                    try:
                        eval_seconds = (float(ed) / 1_000_000_000.0
                                        if ed is not None else None)
                    except (TypeError, ValueError):
                        eval_seconds = None
                    # Final progress: 1.0 means "everything that this
                    # _ollama_chat call was going to contribute is done".
                    if on_stream_progress is not None:
                        try:
                            on_stream_progress(1.0)
                        except Exception:
                            pass
                    break
        except StopRequested:
            raise
        except Exception:
            # If the watchdog tore down the connection, surface a clean
            # StopRequested instead of whatever error the read raised.
            # Otherwise it was a genuine I/O failure — re-raise it.
            if watchdog_cancelled.is_set() or (stop_event is not None
                                               and stop_event.is_set()):
                raise StopRequested("Ollama generation cancelled by user.")
            raise

        # The for-loop can also exit cleanly with EOF when the watchdog
        # called socket.shutdown() — that produces a normal end-of-stream
        # rather than an exception. Check explicitly.
        if watchdog_cancelled.is_set() or (stop_event is not None
                                           and stop_event.is_set()):
            raise StopRequested("Ollama generation cancelled by user.")
    finally:
        # Tell the watchdog to exit (whether we finished normally or are
        # raising), then close the response in case it's still open.
        watchdog_done.set()
        try:
            resp.close()
        except Exception:
            pass
        if wd_thread is not None:
            wd_thread.join(timeout=1.0)

    content = "".join(parts)
    return content, eval_count, eval_seconds


def _parse_flat_json(raw: str):
    """
    Tolerant JSON extractor: handles markdown fences, prose preamble, and
    accepts an array or a single object.
    """
    import re as _re
    text = raw.strip()
    # Strip markdown code fences.
    text = _re.sub(r"^```(?:json)?\s*", "", text, flags=_re.MULTILINE)
    text = _re.sub(r"```\s*$", "", text, flags=_re.MULTILINE)
    text = text.strip()
    # Try direct parse.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try to locate an array.
    arr = _re.search(r"\[\s*\{.*?\}\s*\]", text, _re.DOTALL)
    if arr:
        try:
            return json.loads(arr.group(0))
        except json.JSONDecodeError:
            pass
    # Try to locate a single object.
    obj = _re.search(r"\{.*\}", text, _re.DOTALL)
    if obj:
        try:
            return json.loads(obj.group(0))
        except json.JSONDecodeError:
            pass
    return None


# Numeric field names per schema.
_NUMERIC_BUILDING: set[str] = {
    "total_building_area_sf", "available_space_sf",
    "land_area_acres", "year_built", "clear_height_min_ft",
    "clear_height_max_ft", "dock_doors", "grade_level_doors",
    "column_spacing_width_ft", "column_spacing_depth_ft",
    "auto_parking_spaces", "trailer_parking_spaces",
    "annual_asking_rate_psf", "sale_price", "estimated_annual_opex_psf",
}
_NUMERIC_LAND: set[str] = {"land_area_acres", "sale_price"}
_INTEGER_FIELDS: set[str] = {
    "year_built", "dock_doors", "grade_level_doors",
    "auto_parking_spaces", "trailer_parking_spaces",
}


def _clean_value(field: str, raw_val, is_numeric: bool) -> tuple:
    """
    Convert a single LLM value into something Pydantic will accept.

    Returns a (cleaned_value, confidence) tuple.

      cleaned_value: the value, or None to drop the field entirely.
      confidence: 0.0 - 1.0. 1.0 = clean direct conversion, lower = the
                  value required reshaping (range-collapse, fuzzy closed-set
                  match, suffix parsing). The Excel writer highlights any
                  field with confidence < cfg.low_confidence_threshold.
    """
    import re as _re
    if raw_val is None:
        return None, 1.0

    if isinstance(raw_val, bool):
        return None, 1.0  # booleans aren't valid for any of our fields

    if isinstance(raw_val, (int, float)):
        if field in _INTEGER_FIELDS:
            try:
                return int(raw_val), 1.0
            except (TypeError, ValueError):
                return None, 1.0
        return raw_val, 1.0

    if isinstance(raw_val, list):
        if not raw_val:
            return None, 1.0
        # List collapse: flag as lower confidence — the LLM gave us
        # multiple values where we expected one.
        v, c = _clean_value(field, raw_val[0], is_numeric)
        return v, min(c, 0.6)

    if isinstance(raw_val, dict):
        return None, 1.0

    if not isinstance(raw_val, str):
        return None, 1.0

    s = raw_val.strip()
    if s.lower() in _NULL_MARKERS:
        return None, 1.0

    # office_space_sf: accept literal "BTS"
    if field == "office_space_sf" and s.upper() == "BTS":
        return "BTS", 1.0

    # Closed-set fields: try to map to the canonical value.
    if field in _CLOSED_SETS:
        # Exact match (case-insensitive) is high confidence.
        mapped = _CLOSED_SETS[field].get(s.lower().strip())
        if mapped:
            return mapped, 1.0
        # Keyword-substring fallback is lower confidence — the LLM didn't
        # give us a clean canonical value and we guessed.
        sl = s.lower()
        for k, v in _CLOSED_SETS[field].items():
            if k in sl:
                return v, 0.6
        # No mapping — drop rather than send an invalid value to Pydantic.
        return None, 1.0

    if is_numeric:
        # Strip $ , and K/M/B suffix; pull the first number out.
        s2 = s.lower().replace(",", "").replace("$", "").strip()
        mult = 1
        suffix_used = False
        if s2.endswith("k"):
            mult = 1_000; s2 = s2[:-1]; suffix_used = True
        elif s2.endswith("m"):
            mult = 1_000_000; s2 = s2[:-1]; suffix_used = True
        elif s2.endswith("b"):
            mult = 1_000_000_000; s2 = s2[:-1]; suffix_used = True

        # Look for a numeric pattern. Confidence drops if the original
        # string contained multiple numbers (range like "32-40" or
        # "32' - 40'") — we picked one but the flyer gave a range.
        nums = _re.findall(r"-?\d+(?:\.\d+)?", s2)
        if not nums:
            return None, 1.0
        confidence = 1.0
        if len(nums) > 1:
            confidence = 0.6   # range — we collapsed it
        elif suffix_used:
            confidence = 0.85  # we interpreted "500K" — usually fine
        elif s2.strip() != nums[0]:
            confidence = 0.85  # had extra text we stripped (e.g. units)

        try:
            val = float(nums[0]) * mult
            if field in _INTEGER_FIELDS:
                return int(val), confidence
            return val, confidence
        except ValueError:
            return None, 1.0

    # state: keep as-is for the schema's validator to normalise.
    if field == "state":
        return s, 1.0

    # zip_code: keep first 5 digits.
    if field == "zip_code":
        digits = _re.sub(r"\D", "", s)
        if len(digits) >= 5:
            return digits[:5], 1.0
        return (digits or None), 0.5  # partial zip — low confidence

    # date_available: if the LLM returned a quarter like "Q2 2027", convert
    # to the conventional mid-quarter date (5/1/2027 etc.). Otherwise the
    # raw flyer wording is fine.
    if field == "date_available":
        quarter_date = _normalize_quarter_date(s)
        if quarter_date is not None:
            return quarter_date, 1.0
        return s, 1.0

    return s, 1.0


def _build_records_from_dicts(
    survey_kind: str,
    raw_list: list[dict],
):
    """
    Build a list of Pydantic records from the cleaned LLM array.

    Per-field forgiveness: if Pydantic rejects ONE field's value, we drop
    just that field and keep the rest of the record. We never lose a whole
    record because of a single odd field.
    """
    from pydantic import ValidationError

    if survey_kind == "building":
        Schema = BuildingRecord
        numeric = _NUMERIC_BUILDING
    else:
        Schema = LandRecord
        numeric = _NUMERIC_LAND
    valid_fields = set(Schema.model_fields.keys())

    records = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        # Drop any nested confidence the LLM might have added (we compute
        # our own per-field confidence below).
        raw.pop("confidence", None)
        raw.pop("confidence_notes", None)

        cleaned: dict = {}
        # Per-field confidence: maps field_name -> 0.0–1.0. Anything below
        # the Excel writer's threshold (default 0.8) gets a yellow highlight.
        confidence: dict[str, float] = {}

        for key, val in raw.items():
            norm_key = str(key).lower().strip().replace(" ", "_").replace("-", "_")
            aliased = norm_key in _FIELD_ALIASES
            norm_key = _FIELD_ALIASES.get(norm_key, norm_key)
            if norm_key not in valid_fields:
                continue
            cleaned_val, conf = _clean_value(norm_key, val, norm_key in numeric)
            if cleaned_val is None:
                continue
            # If the LLM used a non-canonical field name and we had to
            # alias it, knock the confidence down a notch — we *think*
            # it's the right schema field but we're inferring.
            if aliased:
                conf = min(conf, 0.7)

            # Try this field — if Pydantic objects, drop just this field.
            candidate = dict(cleaned)
            candidate[norm_key] = cleaned_val
            try:
                Schema(**candidate)
                cleaned[norm_key] = cleaned_val
                # Only record sub-1.0 confidences (the writer's threshold
                # check is "< threshold", so anything ≥ threshold is
                # implicitly fine and we save bytes).
                if conf < 1.0:
                    confidence[norm_key] = conf
            except ValidationError:
                pass
            except Exception:
                pass

        if confidence:
            cleaned["confidence_notes"] = confidence

        try:
            rec = Schema(**cleaned)
        except Exception:
            rec = Schema()
        records.append(rec)

    return records


def _build_segment_count_prompt(
    survey_kind: str,
    flyer_text: str,
    extra_instructions: Optional[str] = None,
) -> str:
    """
    Build the tiny "how many units?" prompt for Ollama Phase 1.

    Kept deliberately simple: counting is a task small models do well, and
    keeping the prompt short means Phase 1 finishes in seconds rather than
    minutes — giving us the building count we need to predict the much
    longer Phase 2.

    If the user supplied extra instructions that constrain which units to
    include (e.g. "only extract building 2"), they're honoured here so the
    Phase 1 count matches Phase 2's output and the ETA is accurate.
    """
    unit  = "building" if survey_kind == "building" else "parcel"
    units = "buildings" if survey_kind == "building" else "parcels"
    plural_examples = (
        "['Building 1', 'Building 2', 'Building 3']"
        if survey_kind == "building" else
        "['Lot 1', 'Lot 2', 'Lot 3']"
    )
    single_example = (
        "['Building 2']" if survey_kind == "building" else "['Lot A']"
    )
    extras_block = _extra_instructions_block(extra_instructions)
    return f"""Read this real-estate flyer and identify how many distinct \
{units} it markets.

A "{unit}" is one physical {unit}. The overall park / campus / project is \
NOT a {unit} — never list the park name as one.

Return ONLY a JSON array of short labels, one per {unit}, in the order they \
appear on the flyer. No prose, no markdown, no explanation.

Examples of valid output:
  Single-{unit} flyer: {single_example}
  Multi-{unit} flyer:  {plural_examples}

{extras_block}FLYER TEXT:
---
{flyer_text}
---

JSON array of {unit} labels:"""


def _parse_unit_labels(raw: str) -> list:
    """Parse the segmentation response into a list of unit labels."""
    import re as _re
    if not raw:
        return []
    text = raw.strip()
    # Strip markdown fences.
    text = _re.sub(r"^```(?:json)?\s*", "", text, flags=_re.MULTILINE)
    text = _re.sub(r"```\s*$", "", text, flags=_re.MULTILINE).strip()
    # Direct parse.
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Regex-find an array.
        m = _re.search(r"\[.*?\]", text, _re.DOTALL)
        if not m:
            return []
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(parsed, list):
        return [str(x).strip() for x in parsed if x is not None]
    if isinstance(parsed, dict):
        # Accept {"buildings": [...]} or {"labels": [...]} etc.
        for key in ("buildings", "parcels", "labels", "units", "names", "items"):
            if isinstance(parsed.get(key), list):
                return [str(x).strip() for x in parsed[key] if x is not None]
    return []


def _extract_via_ollama_flat(
    extracted_text: str,
    source_filename: str,
    survey_kind: str,
    cfg: Config,
    status: StatusCallback = None,
    extra_instructions: Optional[str] = None,
    stop_event: Optional["threading.Event"] = None,
    on_checkpoint: CheckpointCallback = None,
    on_stream_progress: StreamProgressCallback = None,
):
    """
    Two-call Ollama extraction with ETA between the calls.

    Phase 1 — count units. Small fast prompt asking "how many buildings?"
        Gives us the building count so we can predict Phase 2's length
        from history. Counting is easy for small models.

    Phase 2 — full extraction. The existing flat-array prompt; produces
        one JSON object per unit with all fields filled.

    Between the phases we emit the ETA. Timing for Phase 2 is recorded
    using the token counts Ollama returns (eval_count / eval_duration),
    falling back to wall-clock if the version of Ollama is too old.

    `extra_instructions` is the user's free-form additional prompt text
    from the GUI's "Additional instructions" box. Honoured in BOTH phases
    so that e.g. "only extract building 2" produces a count of 1 in
    Phase 1 (matching what Phase 2 will return) and the ETA stays
    accurate.
    """
    from . import timing_stats
    import time as _time

    unit_label_kind = "building" if survey_kind == "building" else "parcel"
    if extra_instructions:
        _say(status, f"Honouring additional instructions: {extra_instructions[:120]}"
                     f"{'...' if len(extra_instructions) > 120 else ''}")

    # --- Phase 1: count the units --------------------------------------
    _say(status, f"Phase 1: counting {unit_label_kind}s in the flyer...")
    seg_prompt = _build_segment_count_prompt(
        survey_kind, extracted_text, extra_instructions)
    try:
        seg_raw, _seg_tokens, _seg_seconds = _ollama_chat(
            seg_prompt, cfg, stop_event=stop_event)
    except StopRequested:
        _say(status, "Phase 1 stopped by user.")
        raise
    except Exception as e:
        # If Phase 1 fails the whole extraction can't proceed — re-raise.
        _say(status, f"Phase 1 FAILED: {type(e).__name__}: {e}")
        raise

    if cfg.debug_dump:
        try:
            _dump_debug(cfg, source_filename, "ollama_phase1_response.txt", seg_raw)
        except Exception:
            pass

    labels = _parse_unit_labels(seg_raw)
    n_units = len(labels) if labels else 1  # If model returned nothing, assume 1
    if labels:
        _say(status, f"Phase 1 done: {n_units} {unit_label_kind}(s) found "
                     f"({', '.join(labels[:6])}{'...' if n_units > 6 else ''}).")
    else:
        _say(status, f"Phase 1: could not parse a count — assuming 1 {unit_label_kind}.")

    # Hard progress checkpoint — Phase 1 finished, the bar advances.
    if on_checkpoint is not None:
        on_checkpoint(f"Phase 1 complete ({n_units} {unit_label_kind}(s)).")

    # --- ETA based on history -------------------------------------------
    est = timing_stats.estimate_seconds(cfg.ollama_model, n_units)
    if est is not None:
        _say(status, f"Estimated Phase 2 time: ~{timing_stats.format_duration(est)} "
                     f"({n_units} {unit_label_kind}(s) on {cfg.ollama_model}).")
    else:
        _say(status, f"No timing history for {cfg.ollama_model} yet — "
                     f"this run will become a data point.")

    # Estimate the output token count Phase 2 will produce, so we can
    # interpolate streaming progress against a reasonable expected total
    # rather than the worst-case num_predict=4096. Heuristic: 250 tokens
    # per unit is a reasonable midpoint for a filled record; if the
    # timing-stats file has historical data for this model, use that
    # average instead (it's far more accurate).
    expected_phase2_tokens: Optional[int] = None
    try:
        hist = timing_stats.history_summary(cfg.ollama_model)
        if hist and hist.get("runs", 0) > 0:
            # history_summary doesn't directly expose tokens-per-building
            # so compute from total_tokens / total_buildings if available.
            # Fall back to the rough heuristic if not.
            tps = hist.get("tokens_per_second")
            spb = hist.get("seconds_per_building")
            if tps and spb:
                # tokens/building = (tokens/sec) * (sec/building)
                tpb = tps * spb
                expected_phase2_tokens = int(max(50, n_units * tpb))
    except Exception:
        pass
    if expected_phase2_tokens is None:
        expected_phase2_tokens = max(50, n_units * 250)

    # --- Phase 2: full flat-array extraction ----------------------------
    _say(status, f"Phase 2: extracting full {unit_label_kind} specs...")
    main_prompt = _build_flat_prompt(survey_kind, extracted_text,
                                     extra_instructions)
    wall_start = _time.monotonic()
    try:
        raw, eval_count, eval_seconds = _ollama_chat(
            main_prompt, cfg, stop_event=stop_event,
            on_stream_progress=on_stream_progress,
            expected_output_tokens=expected_phase2_tokens)
    except StopRequested:
        # User clicked Stop mid-generation. Surface the cancellation
        # distinctly so the outer pipeline doesn't log it as a "FAILED"
        # error. NO timing recorded for a cancelled call.
        _say(status, "Phase 2 stopped by user mid-generation.")
        raise
    except Exception as e:
        # Phase 2 failure — re-raise so the outer pipeline marks the
        # flyer as failed. NO timing recorded for a failed call.
        _say(status, f"Phase 2 FAILED: {type(e).__name__}: {e}")
        raise
    wall_seconds = _time.monotonic() - wall_start

    _say(status, f"Got {len(raw)} chars from {cfg.ollama_model}. Parsing...")

    if cfg.debug_dump:
        try:
            _dump_debug(cfg, source_filename, "ollama_raw_response.txt", raw)
        except Exception:
            pass

    # Parse the response and build records.
    parsed = _parse_flat_json(raw)
    park_name: Optional[str] = None
    records_raw: list = []
    if parsed is None:
        _say(status, "WARNING: LLM returned no parseable JSON.")
    elif isinstance(parsed, list):
        records_raw = [p for p in parsed if isinstance(p, dict)]
    elif isinstance(parsed, dict):
        if "properties" in parsed and isinstance(parsed["properties"], list):
            records_raw = [p for p in parsed["properties"] if isinstance(p, dict)]
            park_name = parsed.get("park_name") if isinstance(parsed.get("park_name"), str) else None
        elif "data" in parsed and isinstance(parsed["data"], list):
            records_raw = [p for p in parsed["data"] if isinstance(p, dict)]
        elif "buildings" in parsed and isinstance(parsed["buildings"], list):
            records_raw = [p for p in parsed["buildings"] if isinstance(p, dict)]
            park_name = parsed.get("park_name") if isinstance(parsed.get("park_name"), str) else None
        elif "result" in parsed and isinstance(parsed["result"], list):
            records_raw = [p for p in parsed["result"] if isinstance(p, dict)]
        else:
            records_raw = [parsed]

    records = _build_records_from_dicts(survey_kind, records_raw) if records_raw else []

    # --- Record Phase 2 timing -----------------------------------------
    # Prefer Ollama's own token timing (hardware-bound, very stable);
    # fall back to wall-clock if the Ollama version didn't report it.
    record_seconds = eval_seconds if (eval_seconds and eval_seconds > 0) else wall_seconds
    try:
        timing_stats.record_run(
            cfg.ollama_model,
            output_tokens=eval_count,
            seconds=record_seconds,
            n_buildings=len(records),
        )
        if eval_count is not None and eval_seconds:
            tok_per_sec = eval_count / eval_seconds
            _say(status, f"Phase 2 done in {timing_stats.format_duration(record_seconds)} "
                         f"({eval_count} tokens, {tok_per_sec:.1f} tok/s).")
        else:
            _say(status, f"Phase 2 done in {timing_stats.format_duration(record_seconds)}.")
    except Exception as e:
        log.warning("Could not record timing stats for %s: %s", source_filename, e)

    is_multi = len(records) > 1
    return records, park_name, is_multi


# ---------------------------------------------------------------------------
# Public extraction entry points (two-phase, used by Claude)
# ---------------------------------------------------------------------------

def extract_buildings(
    extracted_text: str,
    source_filename: str,
    cfg: Config,
    status: StatusCallback = None,
    extra_instructions: Optional[str] = None,
    stop_event: Optional["threading.Event"] = None,
    on_checkpoint: CheckpointCallback = None,
    on_stream_progress: StreamProgressCallback = None,
) -> BuildingExtractionResult:
    """
    Building Survey extraction.

    For Ollama (small local models): single-call flat-array prompt that
    actually works on 3B-class models. See _extract_via_ollama_flat.

    For Claude: two-phase schema-driven extraction — Claude has the
    capacity to handle complex schemas reliably.

    `extra_instructions` is free-form text from the user that gets
    appended to the system prompt for both phases. Used for per-job
    nuances like "only extract building 2" or "convert measurements to
    square meters". Optional; None or "" means no extras.

    `stop_event`, if set during the Ollama generation, will close the
    HTTP stream and raise StopRequested so the user's Stop button is
    responsive mid-generation. The Claude path is fast enough that we
    don't need mid-call cancellation — it only checks stop_event at
    phase boundaries (handled in the pipeline).

    `on_checkpoint(msg)` fires at hard progress checkpoints inside the
    Ollama path (currently: after Phase 1 segmentation completes).
    `on_stream_progress(fraction)` fires throughout Phase 2 generation,
    receiving a cumulative 0.0–1.0 fraction. Both are best-effort —
    raising from them won't break the extraction.
    """
    _prepare_provider(cfg, status)

    if cfg.debug_dump:
        _dump_debug(cfg, source_filename, "extracted_text.txt", extracted_text)

    # --- Ollama path: single call, flat array ----------------------------
    if cfg.provider == "ollama":
        records, park_name, is_multi = _extract_via_ollama_flat(
            extracted_text, source_filename, "building", cfg, status,
            extra_instructions=extra_instructions,
            stop_event=stop_event,
            on_checkpoint=on_checkpoint,
            on_stream_progress=on_stream_progress,
        )
        if cfg.debug_dump:
            try:
                tmp = BuildingExtractionResult(
                    is_multi_building_park=is_multi,
                    park_name=park_name, records=records,
                )
                _dump_debug(cfg, source_filename, "llm_result.json",
                            tmp.model_dump_json(indent=2))
            except Exception:
                pass
        return BuildingExtractionResult(
            is_multi_building_park=is_multi,
            park_name=park_name,
            records=records,
        )

    # --- Claude path: two-phase schema-driven ----------------------------
    client = _build_client(cfg)

    # --- Phase 1: segmentation --------------------------------------------
    # Append the user's free-form extras (if any) to BOTH the segmentation
    # and per-building extraction system prompts.
    _extras_norm = _normalize_extra_instructions(extra_instructions)
    if _extras_norm:
        _say(status, f"Honouring additional instructions: {_extras_norm[:120]}"
                     f"{'...' if len(_extras_norm) > 120 else ''}")
    _seg_system = BUILDING_SEGMENT_PROMPT + (
        f"\n\nUSER INSTRUCTIONS (follow these in addition to the rules above):\n"
        f"{_extras_norm}" if _extras_norm else ""
    )
    _ext_system = BUILDING_EXTRACT_PROMPT + (
        f"\n\nUSER INSTRUCTIONS (follow these in addition to the rules above):\n"
        f"{_extras_norm}" if _extras_norm else ""
    )

    _say(status, "Phase 1/2: identifying buildings in the flyer...")
    seg: BuildingSegmentation = _create(
        client, cfg,
        messages=[
            {"role": "system", "content": _seg_system},
            {"role": "user",
             "content": build_segment_user_prompt(extracted_text, source_filename)},
        ],
        response_model=BuildingSegmentation,
    )
    labels = [b.label for b in seg.buildings] or ["the building"]
    # Drop phantom "buildings" that are actually the park/campus name.
    labels, dropped = _filter_building_labels(labels, seg.park_name)
    if dropped:
        _say(status, f"Ignored non-building label(s): {', '.join(dropped)}")
        # If the segmenter mislabelled the park, recover it into park_name.
        if not seg.park_name and dropped:
            seg.park_name = dropped[0]
    _say(status, f"Phase 1 done: {len(labels)} building(s) found "
                 f"({', '.join(labels)}).")

    if cfg.debug_dump:
        try:
            _dump_debug(cfg, source_filename, "segmentation.json",
                        seg.model_dump_json(indent=2))
        except Exception:
            pass

    # --- Phase 2: per-building extraction, in parallel --------------------
    _say(status, f"Phase 2/2: extracting specs for {len(labels)} building(s)...")

    def _one(label: str) -> tuple[str, Optional[BuildingRecord]]:
        try:
            rec: BuildingRecord = _create(
                client, cfg,
                messages=[
                    {"role": "system", "content": _ext_system},
                    {"role": "user",
                     "content": build_extract_user_prompt(
                         extracted_text, source_filename, label)},
                ],
                response_model=BuildingRecord,
            )
            return label, rec
        except Exception as e:
            log.exception("Per-building extraction failed for %r", label)
            _say(status, f"  - {label}: extraction FAILED ({type(e).__name__})")
            return label, None

    records: list[BuildingRecord] = []
    workers = max(1, cfg.max_parallel_extractions)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, lbl): lbl for lbl in labels}
        done_by_label: dict[str, Optional[BuildingRecord]] = {}
        for fut in as_completed(futs):
            label, rec = fut.result()
            done_by_label[label] = rec
            if rec is not None:
                _say(status, f"  - {label}: extracted.")
    # Preserve flyer order.
    for lbl in labels:
        rec = done_by_label.get(lbl)
        if rec is not None:
            # If the model didn't carry the building name, use the label.
            if not rec.property_name:
                rec.property_name = lbl
            records.append(rec)

    result = BuildingExtractionResult(
        is_multi_building_park=seg.is_multi_building_park,
        park_name=seg.park_name,
        records=records,
    )

    if cfg.debug_dump:
        try:
            _dump_debug(cfg, source_filename, "llm_result.json",
                        result.model_dump_json(indent=2))
        except Exception:
            pass

    return result


def extract_land(
    extracted_text: str,
    source_filename: str,
    cfg: Config,
    status: StatusCallback = None,
    extra_instructions: Optional[str] = None,
    stop_event: Optional["threading.Event"] = None,
    on_checkpoint: CheckpointCallback = None,
    on_stream_progress: StreamProgressCallback = None,
) -> LandExtractionResult:
    """
    Land Survey extraction.

    For Ollama: single-call flat-array prompt (see _extract_via_ollama_flat).
    For Claude: two-phase schema-driven extraction.

    `extra_instructions` is optional free-form user text appended to the
    prompt — used for per-job nuances.

    `stop_event`, if set during Ollama generation, cancels the in-flight
    HTTP stream mid-token. The Claude path checks stop_event only at
    phase boundaries (handled in the pipeline).

    `on_checkpoint` / `on_stream_progress` work the same as in
    extract_buildings — Phase 1 checkpoint plus streaming token-based
    progress through Phase 2.
    """
    _prepare_provider(cfg, status)

    if cfg.debug_dump:
        _dump_debug(cfg, source_filename, "extracted_text.txt", extracted_text)

    # --- Ollama path: single call, flat array ----------------------------
    if cfg.provider == "ollama":
        records, park_name, is_multi = _extract_via_ollama_flat(
            extracted_text, source_filename, "land", cfg, status,
            extra_instructions=extra_instructions,
            stop_event=stop_event,
            on_checkpoint=on_checkpoint,
            on_stream_progress=on_stream_progress,
        )
        if cfg.debug_dump:
            try:
                tmp = LandExtractionResult(
                    is_multi_parcel=is_multi,
                    park_name=park_name, records=records,
                )
                _dump_debug(cfg, source_filename, "llm_result.json",
                            tmp.model_dump_json(indent=2))
            except Exception:
                pass
        return LandExtractionResult(
            is_multi_parcel=is_multi,
            park_name=park_name,
            records=records,
        )

    # --- Claude path: two-phase schema-driven ----------------------------
    client = _build_client(cfg)

    # Append user's free-form extras to both phase prompts (if any).
    _extras_norm = _normalize_extra_instructions(extra_instructions)
    if _extras_norm:
        _say(status, f"Honouring additional instructions: {_extras_norm[:120]}"
                     f"{'...' if len(_extras_norm) > 120 else ''}")
    _seg_system = LAND_SEGMENT_PROMPT + (
        f"\n\nUSER INSTRUCTIONS (follow these in addition to the rules above):\n"
        f"{_extras_norm}" if _extras_norm else ""
    )
    _ext_system = LAND_EXTRACT_PROMPT + (
        f"\n\nUSER INSTRUCTIONS (follow these in addition to the rules above):\n"
        f"{_extras_norm}" if _extras_norm else ""
    )

    # --- Phase 1: segmentation --------------------------------------------
    _say(status, "Phase 1/2: identifying parcels in the flyer...")
    seg: LandSegmentation = _create(
        client, cfg,
        messages=[
            {"role": "system", "content": _seg_system},
            {"role": "user",
             "content": build_segment_user_prompt(extracted_text, source_filename)},
        ],
        response_model=LandSegmentation,
    )
    labels = [p.label for p in seg.parcels] or ["the parcel"]
    _say(status, f"Phase 1 done: {len(labels)} parcel(s) found "
                 f"({', '.join(labels)}).")

    if cfg.debug_dump:
        try:
            _dump_debug(cfg, source_filename, "segmentation.json",
                        seg.model_dump_json(indent=2))
        except Exception:
            pass

    # --- Phase 2: per-parcel extraction, in parallel ----------------------
    _say(status, f"Phase 2/2: extracting specs for {len(labels)} parcel(s)...")

    def _one(label: str) -> tuple[str, Optional[LandRecord]]:
        try:
            rec: LandRecord = _create(
                client, cfg,
                messages=[
                    {"role": "system", "content": _ext_system},
                    {"role": "user",
                     "content": build_extract_user_prompt(
                         extracted_text, source_filename, label)},
                ],
                response_model=LandRecord,
            )
            return label, rec
        except Exception as e:
            log.exception("Per-parcel extraction failed for %r", label)
            _say(status, f"  - {label}: extraction FAILED ({type(e).__name__})")
            return label, None

    records: list[LandRecord] = []
    workers = max(1, cfg.max_parallel_extractions)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, lbl): lbl for lbl in labels}
        done_by_label: dict[str, Optional[LandRecord]] = {}
        for fut in as_completed(futs):
            label, rec = fut.result()
            done_by_label[label] = rec
            if rec is not None:
                _say(status, f"  - {label}: extracted.")
    for lbl in labels:
        rec = done_by_label.get(lbl)
        if rec is not None:
            if not rec.property_name:
                rec.property_name = lbl
            records.append(rec)

    result = LandExtractionResult(
        is_multi_parcel=seg.is_multi_parcel,
        park_name=seg.park_name,
        records=records,
    )

    if cfg.debug_dump:
        try:
            _dump_debug(cfg, source_filename, "llm_result.json",
                        result.model_dump_json(indent=2))
        except Exception:
            pass

    return result


def _dump_debug(cfg: Config, source_filename: str, suffix: str, content: str) -> None:
    """Write a debug artifact next to the output folder for troubleshooting."""
    try:
        from pathlib import Path
        debug_dir = Path(cfg.default_output_dir) / "_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(source_filename).stem
        out = debug_dir / f"{stem}__{suffix}"
        out.write_text(content, encoding="utf-8")
        log.info("Debug artifact written: %s", out)
    except Exception as e:
        log.warning("Could not write debug artifact: %s", e)


# ---------------------------------------------------------------------------
# Health check used by the GUI Settings dialog
# ---------------------------------------------------------------------------

def ping_ollama(cfg: Config) -> tuple[bool, str]:
    """
    Test connectivity for the configured provider. Kept under the name
    ping_ollama for backward compatibility with the GUI.

    provider == "claude": verifies the claude-key file exists and is
        non-empty, and does a tiny test API call.
    provider == "ollama": starts the local server if needed and checks
        the configured model is installed.

    Returns (ok, message).
    """
    if cfg.provider == "claude":
        # 1. Key file present?
        try:
            key = cfg.read_claude_key()
        except Exception as e:
            return False, str(e)
        # 2. Tiny live call to confirm the key actually works.
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=key)
            client.messages.create(
                model=cfg.claude_model,
                max_tokens=8,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True, f"Claude API OK — model '{cfg.claude_model}' is reachable."
        except Exception as e:
            return False, (
                f"Claude API key was found but the test call failed:\n{e}\n\n"
                f"Check that the key in {cfg.claude_key_path()} is valid."
            )

    # --- Ollama ------------------------------------------------------------
    # Try to start if not already up.
    ok, msg = ensure_ollama_running(cfg)
    if not ok:
        return False, msg

    # Confirm the chosen model is present.
    url = cfg.ollama_base_url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        models = [m.get("id", "") for m in data.get("data", [])]
        if cfg.ollama_model not in models:
            return (
                False,
                f"Ollama is running but model '{cfg.ollama_model}' is not installed.\n"
                f"Run:  ollama pull {cfg.ollama_model}",
            )
        return True, f"Ollama OK — model '{cfg.ollama_model}' is ready."
    except Exception as e:
        return False, f"Could not query Ollama models: {e}"

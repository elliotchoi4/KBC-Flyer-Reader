"""
Stage 4 helpers: post-LLM normalization.

Pydantic gives us type validation for free; this module handles the
"clean up the value Pydantic accepted" cases:

  - "Sept 2024" -> date(2024, 9, 1)
  - "(555) 123 4567" -> "+15551234567"
  - "Cali" / "Calif." / "California" -> "CA"

These run after the LLM extraction so we present clean values to Excel.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional


# ---------------------------------------------------------------------------
# State name -> 2-letter abbreviation (in case the LLM didn't follow instructions)
# ---------------------------------------------------------------------------

_STATE_MAP = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
    "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY", "DISTRICT OF COLUMBIA": "DC",
}


def normalize_state(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.strip().upper().rstrip(".")
    if len(v) == 2 and v.isalpha():
        return v
    if v in _STATE_MAP:
        return _STATE_MAP[v]
    # Try partial match for abbreviated forms like "CALIF"
    for full, abbr in _STATE_MAP.items():
        if full.startswith(v) and len(v) >= 4:
            return abbr
    return v[:2] if len(v) >= 2 else None


# ---------------------------------------------------------------------------
# Dates: Pydantic will already parse ISO. Anything more exotic goes through
# dateutil if the user supplied it as a string somewhere.
# ---------------------------------------------------------------------------

def normalize_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        return None
    try:
        from dateutil import parser
        return parser.parse(value, fuzzy=True).date()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Phone numbers (used only for owner contact fields if present in comments)
# ---------------------------------------------------------------------------

def normalize_phone(value: Optional[str], default_region: str = "US") -> Optional[str]:
    if not value:
        return None
    try:
        import phonenumbers
        num = phonenumbers.parse(value, default_region)
        if phonenumbers.is_valid_number(num):
            return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        pass
    return value


# ---------------------------------------------------------------------------
# Numeric cleanup: strip currency symbols, "SF", "acres", commas, etc.
# Used as a defensive layer in case the LLM didn't follow the strip rule.
# ---------------------------------------------------------------------------

def clean_number(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip().replace(",", "").replace("$", "")
    for token in (" sf", " SF", " acres", " ac", " ac.", " ft", "'"):
        s = s.replace(token, "")
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        return None

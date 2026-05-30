"""
Prompt templates for the two-phase LLM extraction.

Phase 1 (segmentation): identify how many buildings/parcels the flyer
describes. Easy task — small prompt, small schema.

Phase 2 (per-unit extraction): one call per building/parcel, each filling
the full schema for just that single unit. The prompt includes the whole
flyer text for context but tells the model to focus on one named unit.

Why two phases
--------------
Asking a small local model to fill a 38-field schema across 6 buildings in
one call fails: it returns the right shape (6 objects) but every field is
null — it spends its capacity on structure and has nothing left for content.
Splitting into one focused call per building makes each call a tractable
task the model can actually complete.
"""
from __future__ import annotations


# ===========================================================================
# Phase 1 — segmentation
# ===========================================================================

BUILDING_SEGMENT_PROMPT = """\
You analyse an industrial real-estate flyer and identify how many distinct \
BUILDINGS it describes.

CRITICAL: a "building" is a single physical structure. The overall PARK, \
CAMPUS, CENTER, or PROJECT is NOT a building — never list it as one.

Rules:
- List ONLY actual individual buildings (e.g. "Building 1", "Building 2", \
"Phase II Building A"). A flyer for one building lists exactly one.
- The park/campus/center name (e.g. "TriPoint Logistics Center", "Manor \
Crossings Logistics Center") goes in park_name — NEVER in the buildings list.
- "Future" or "proposed" buildings shown on a site plan but not the subject \
of the flyer: do NOT list them. List only the building(s) the flyer is \
actually marketing.
- If the flyer markets ONE building inside a larger park, return ONE \
building and put the park name in park_name. Set is_multi_building_park to \
true ONLY if the flyer itself presents specs for multiple buildings.
- Do NOT extract specs here — only identify and label the building(s).

Examples:
- Flyer titled "Building 2 — TriPoint Logistics Center": buildings = \
["Building 2"], park_name = "TriPoint Logistics Center", \
is_multi_building_park = false.
- Flyer with spec sheets for Buildings 1 through 6 of a park: buildings = \
["Building 1", ... "Building 6"], park_name = the park, \
is_multi_building_park = true.
"""


LAND_SEGMENT_PROMPT = """\
You analyse an industrial LAND flyer and identify how many distinct PARCELS \
it describes.

Rules:
- If the flyer describes multiple parcels/lots, set is_multi_parcel=true and \
list every parcel.
- Give each parcel the exact label the flyer uses (e.g. "Lot 3", "Parcel A").
- If the flyer describes only ONE parcel, return a list with that one parcel, \
is_multi_parcel=false.
- park_name = the overall project name if there is one, else null.
- Do NOT extract specs here — only identify and label the parcels.
"""


# ===========================================================================
# Phase 2 — per-unit extraction
# ===========================================================================

BUILDING_EXTRACT_PROMPT = """\
You extract data for ONE specific building from an industrial real-estate \
flyer into JSON.

You are given the full flyer text for context, and the name of the ONE \
building to extract. Fill the schema for THAT building only.

Your goal is to fill in AS MANY fields as the flyer supports. Be thorough.

Rules:
- Extract every value the flyer gives for the named building. Park-wide \
facts (city, state, address, owner, power, zoning) also apply to the \
building — always fill those in too.
- For free-text and numeric fields: if the flyer states or strongly implies \
a value, FILL IT IN. Make a reasonable best effort rather than leaving \
those fields blank — flag uncertainty in confidence_notes instead.
- For CLOSED-SET dropdown fields (occupancy_status, load_type, \
sprinkler_type, tenancy_type, building_status, building_type, sale_lease): \
only set a value if the flyer explicitly states it. If the flyer is \
silent about one of these, leave it NULL. Do NOT default occupancy to \
"Occupied", do NOT default load_type to "Front Load", etc. — picking a \
common-looking default is WORSE than leaving the cell empty.
- Specifically for occupancy_status: 'Available for Lease' / 'For Lease' \
/ 'Available' with no tenant named => Vacant. Otherwise leave null.
- Numbers = plain digits only (strip $, commas, "SF", "AC", "acres").
- State = 2-letter US code (e.g. Texas -> TX).
- office_space_sf accepts a number, or the literal "BTS" if built-to-suit.
- Closed-set field allowed values:
  - building_status: Existing | Planned/Proposed | Under Construction | Demolished
  - building_type: Spec | BTS
  - occupancy_status: Occupied | Vacant
  - tenancy_type: Single-Tenant | Multi-Tenant | Owner Occupied
  - sprinkler_type: ESFR | Wet | Dry | None | Other
  - load_type: Cross-dock | Front Load | Rear Load | Other | L-shape
  - sale_lease: Sale | Lease | Sale/Lease
- Mapping hints (use ONLY when flyer text actually contains these): \
"Cross Dock" -> load_type "Cross-dock"; "Rear Load" -> load_type "Rear Load"; \
a proposed/not-yet-built park -> building_status "Planned/Proposed"; \
"for lease or build-to-suit" -> sale_lease "Lease".
- If a building lists a size range or "divisible to", total_building_area_sf \
is the TOTAL size; available_space_sf may be the same total unless stated.
- confidence_notes: for any field where you made a guess or are less than \
~80% sure, add the field name mapped to a confidence between 0 and 1.
"""


LAND_EXTRACT_PROMPT = """\
You extract data for ONE specific land parcel from an industrial LAND flyer \
into JSON.

You are given the full flyer text for context, and the name of the ONE \
parcel to extract. Fill the schema for THAT parcel only.

Your goal is to fill in AS MANY fields as the flyer supports. Be thorough.

Rules:
- Extract every value the flyer gives for the named parcel. Site-wide facts \
(city, state, address) also apply — always fill those in too.
- If the flyer states or strongly implies a value, FILL IT IN. Make a \
reasonable best effort rather than leaving a field blank.
- Use null ONLY when the flyer genuinely gives no information for a field. \
Do not leave a field null just because you are not fully certain — fill in \
your best reading and flag it in confidence_notes instead.
- Numbers = plain digits only (strip $, commas, "AC", "acres").
- State = 2-letter US code.
- sale_price = total price, not per-acre.
- Closed-set fields: pick the best-matching allowed value. Use null only if \
nothing fits.
  - land_construction_status: Raw Land | Site Graded | Utilities to Site | \
Entitled | Entitlements in Progress | Paved/Parking
  - sale_lease: Sale | Lease | Sale/Lease
- confidence_notes: this is how you flag uncertainty WITHOUT leaving a field \
blank. For any field where you made a guess or are less than ~80% sure, add \
the field name mapped to a confidence between 0 and 1. Still fill the field \
itself in — confidence_notes just marks it for human review.
"""


# ===========================================================================
# User-message builders
# ===========================================================================

def build_segment_user_prompt(extracted_text: str, source_filename: str) -> str:
    """User message for the phase-1 segmentation call."""
    return (
        f"Flyer file: {source_filename}\n\n"
        f"--- FLYER TEXT ---\n{extracted_text}\n--- END ---\n\n"
        "Identify and label every building/parcel this flyer describes."
    )


def build_extract_user_prompt(
    extracted_text: str, source_filename: str, unit_label: str
) -> str:
    """User message for a phase-2 per-unit extraction call."""
    return (
        f"Flyer file: {source_filename}\n\n"
        f"--- FLYER TEXT ---\n{extracted_text}\n--- END ---\n\n"
        f"Extract the schema for this one unit only: {unit_label!r}.\n"
        f"Use the section of the flyer describing {unit_label!r} for its "
        f"specs, and the park/site-wide sections for shared facts like "
        f"address, city, and state."
    )

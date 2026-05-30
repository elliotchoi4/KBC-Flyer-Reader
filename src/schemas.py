"""
Pydantic schemas defining what the LLM is allowed to extract.

These schemas serve two purposes:
1. They constrain the LLM (via `instructor`) so it can only return valid values
   matching the template's dropdown options.
2. They validate / normalize fields after extraction (state abbreviations,
   numeric fields, etc.).

Both schemas use `Optional[T] = None` everywhere so missing fields stay
blank instead of being filled with hallucinations.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .normalizers import normalize_state as _normalize_state


# ---------------------------------------------------------------------------
# Closed-set enums matching the template's data-validation dropdowns exactly.
# Keep these in sync with the cells in row 5 of each KBC template.
# ---------------------------------------------------------------------------

BuildingStatus = Literal["Existing", "Planned/Proposed", "Under Construction", "Demolished"]
BuildingType = Literal["Spec", "BTS"]
OccupancyStatus = Literal["Occupied", "Vacant"]
TenancyType = Literal["Single-Tenant", "Multi-Tenant", "Owner Occupied"]
SprinklerType = Literal["ESFR", "Wet", "Dry", "None", "Other"]
LoadType = Literal["Cross-dock", "Front Load", "Rear Load", "Other", "L-shape"]
SaleLease = Literal["Sale", "Lease", "Sale/Lease"]
LandConstructionStatus = Literal[
    "Raw Land",
    "Site Graded",
    "Utilities to Site",
    "Entitled",
    "Entitlements in Progress",
    "Paved/Parking",
]


# ---------------------------------------------------------------------------
# Building Survey record (one row per building)
# ---------------------------------------------------------------------------

class BuildingRecord(BaseModel):
    """One row of the Building Survey template."""

    # Location
    property_name: Optional[str] = Field(
        None,
        description="Name of the building or property. If the flyer covers a business park "
        "with multiple buildings, use the individual building name (e.g., 'Building 1', "
        "'Phase II Building A'), NOT the park name.",
    )
    address: Optional[str] = Field(None, description="Street address only, no city/state/zip.")
    city: Optional[str] = None
    state: Optional[str] = Field(
        None,
        description="Two-letter US state abbreviation (e.g., 'CA', 'TX'). Convert full names if needed.",
    )
    zip_code: Optional[str] = Field(None, description="5-digit ZIP code as a string.")
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    # Site Details
    property_owner: Optional[str] = Field(
        None, description="Developer / owner / landlord company name."
    )
    total_building_area_sf: Optional[float] = Field(
        None, description="Total building square footage."
    )
    available_space_sf: Optional[float] = None
    office_space_sf: Optional[str] = Field(
        None,
        description="Office square footage as a number, or the literal string 'BTS' if "
        "the office is built-to-suit.",
    )

    @field_validator("office_space_sf", mode="before")
    @classmethod
    def _office_to_str(cls, v):
        """Allow the LLM to pass a number; stringify it for the schema."""
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return f"{int(v)}" if float(v).is_integer() else f"{v}"
        return v
    land_area_acres: Optional[float] = None
    year_built: Optional[int] = Field(None, ge=1800, le=2100)
    building_status: Optional[BuildingStatus] = None
    building_type: Optional[BuildingType] = None
    occupancy_status: Optional[OccupancyStatus] = None
    tenancy_type: Optional[TenancyType] = None
    current_tenant: Optional[str] = None
    prior_use: Optional[str] = None
    prior_tenant: Optional[str] = None
    date_available: Optional[str] = Field(
        None,
        description="When the building becomes available, as written on "
                    "the flyer (e.g. 'Q1 2027', 'Spring 2027', '2027-04-01').",
    )
    sprinkler_type: Optional[SprinklerType] = None
    current_zoning: Optional[str] = None
    clear_height_min_ft: Optional[float] = None
    clear_height_max_ft: Optional[float] = None
    load_type: Optional[LoadType] = None
    dock_doors: Optional[int] = Field(None, description="Number of existing dock doors.")
    grade_level_doors: Optional[int] = None
    column_spacing_width_ft: Optional[float] = None
    column_spacing_depth_ft: Optional[float] = None
    auto_parking_spaces: Optional[int] = None
    trailer_parking_spaces: Optional[int] = None
    existing_power: Optional[str] = Field(
        None, description="Power spec as written on the flyer, e.g. '4,000 amps 277/480/3ph'."
    )

    # Economics
    sale_lease: Optional[SaleLease] = None
    annual_asking_rate_psf: Optional[float] = Field(
        None, description="Annual asking lease rate per square foot, in dollars."
    )
    sale_price: Optional[float] = None
    estimated_annual_opex_psf: Optional[float] = None
    comments: Optional[str] = Field(
        None, description="Any noteworthy details that don't fit other fields."
    )

    # Per-field confidence map. The LLM should only include entries for
    # fields where confidence < 0.8, so reviewers know what to double-check.
    # Keys must be field names defined above.
    confidence_notes: dict[str, float] = Field(
        default_factory=dict,
        description="Map of field_name -> confidence score (0.0-1.0). Only include "
        "entries for fields you are NOT confident about (confidence < 0.8). "
        "Omit entries for fields you are highly confident about or did not extract.",
    )

    @field_validator("state")
    @classmethod
    def _norm_state(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_state(v)


# ---------------------------------------------------------------------------
# Land Survey record (one row per parcel)
# ---------------------------------------------------------------------------

class LandRecord(BaseModel):
    """One row of the Land Survey template."""

    property_name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = Field(None, description="Two-letter US state abbreviation.")
    zip_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    land_owner: Optional[str] = None
    land_area_acres: Optional[float] = None
    current_zoning: Optional[str] = None
    land_construction_status: Optional[LandConstructionStatus] = None
    existing_power: Optional[str] = None
    sale_lease: Optional[SaleLease] = None
    sale_price: Optional[float] = Field(
        None, description="Total sale price in dollars. Per-acre price is auto-computed."
    )
    comments: Optional[str] = None

    confidence_notes: dict[str, float] = Field(
        default_factory=dict,
        description="Map of field_name -> confidence (0.0-1.0). Include only "
        "entries for fields you are NOT confident about (confidence < 0.8).",
    )

    @field_validator("state")
    @classmethod
    def _norm_state(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_state(v)


# ---------------------------------------------------------------------------
# Top-level extraction result.
#
# This is what the LLM actually returns. The model returns a LIST of records
# (one per building/parcel) because a single flyer can describe multiple
# buildings in a park. The model also flags whether the flyer is describing
# a multi-building park, so the orchestrator can sanity-check.
# ---------------------------------------------------------------------------

class BuildingExtractionResult(BaseModel):
    """LLM response wrapper for building flyers."""

    is_multi_building_park: bool = Field(
        ...,
        description="True if the flyer describes an industrial/business park containing "
        "multiple distinct buildings. False if it describes a single building.",
    )
    park_name: Optional[str] = Field(
        None,
        description="The umbrella park name, if applicable. This is recorded for context "
        "but is NOT written as its own row.",
    )
    records: list[BuildingRecord] = Field(
        ...,
        description="One record per building. If the flyer describes a multi-building park, "
        "return one record per individual building. NEVER add a record for the park "
        "as a whole. If there is only one building, return a list of length 1.",
    )


class LandExtractionResult(BaseModel):
    """LLM response wrapper for land flyers."""

    is_multi_parcel: bool = Field(
        ...,
        description="True if the flyer describes multiple distinct land parcels. "
        "False if it describes a single parcel.",
    )
    park_name: Optional[str] = Field(
        None, description="Umbrella project/park name, if applicable. Not written as its own row."
    )
    records: list[LandRecord] = Field(
        ...,
        description="One record per parcel. If only one parcel, return a list of length 1.",
    )


# ---------------------------------------------------------------------------
# Segmentation schemas (phase 1 of the two-phase extraction)
#
# Rather than asking the model to extract every field of every building in a
# single call (which a small model fails at — it returns the right shape but
# all-null values), extraction runs in two phases:
#
#   Phase 1 — segmentation: a lightweight call that only identifies how many
#             buildings/parcels the flyer describes and gives each a short
#             label (e.g. "Building 1"). This is an easy task even for a 3B
#             model.
#   Phase 2 — per-unit extraction: one focused call per building/parcel, each
#             filling the full schema for just that one unit. Small, tractable
#             calls that the model can actually complete.
# ---------------------------------------------------------------------------

class BuildingSegment(BaseModel):
    """One building identified during the segmentation phase."""

    label: str = Field(
        ...,
        description="Short label for this building exactly as the flyer names it, "
        "e.g. 'Building 1', 'Phase II Building A'.",
    )


class BuildingSegmentation(BaseModel):
    """Phase-1 result: which buildings the flyer describes."""

    is_multi_building_park: bool = Field(
        ...,
        description="True if the flyer describes an industrial/business park with "
        "multiple distinct buildings.",
    )
    park_name: Optional[str] = Field(
        None,
        description="The umbrella park name, if the flyer is a multi-building park. "
        "Recorded for context only — never written as its own row.",
    )
    buildings: list[BuildingSegment] = Field(
        ...,
        description="One entry per distinct building the flyer describes. For a "
        "single-building flyer, return a list of length 1.",
    )


class ParcelSegment(BaseModel):
    """One land parcel identified during the segmentation phase."""

    label: str = Field(
        ...,
        description="Short label for this parcel exactly as the flyer names it, "
        "e.g. 'Lot 3', 'Parcel A'.",
    )


class LandSegmentation(BaseModel):
    """Phase-1 result: which parcels the flyer describes."""

    is_multi_parcel: bool = Field(
        ...,
        description="True if the flyer describes multiple distinct land parcels.",
    )
    park_name: Optional[str] = Field(
        None,
        description="Umbrella project/park name, if applicable. Never written as a row.",
    )
    parcels: list[ParcelSegment] = Field(
        ...,
        description="One entry per distinct parcel. For a single-parcel flyer, "
        "return a list of length 1.",
    )

"""
Field Hints — user-maintained broker shorthand presets.

Brokers state things in idiosyncratic ways. A site-plan callout like
"DH 9'x10' (60)" might mean "60 dock-high doors, 9'x10'". The extraction
model may or may not infer that. Field Hints let a user teach the app these
mappings once, grouped into named presets (typically per broker or per
brokerage), and pick a preset when starting an extraction. The selected
preset's mappings are compiled into an instruction block that is prepended
to the per-run Additional Instructions, so accuracy on the flyers you
actually deal with improves over time — no model retraining involved.

Storage: a single JSON file in the user-data dir:

    {
      "presets": [
        {
          "name": "CBRE - Phoenix",
          "mappings": [
            {"shorthand": "DH", "meaning": "dock-high door (dock_doors)"},
            {"shorthand": "GL", "meaning": "grade-level door (drive_in_doors)"}
          ]
        }
      ]
    }
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from typing import List

from .config import user_data_dir

log = logging.getLogger("flyer_reader")

_PRESETS_FILENAME = "field_hints.json"
# Keep the compiled block well under the extra-instructions cap so it can
# coexist with a user's own per-run notes.
MAX_MAPPINGS_PER_PRESET = 60


@dataclass
class HintMapping:
    shorthand: str = ""
    meaning: str = ""
    # Optional: the template field name this shorthand maps to (e.g.
    # "dock_doors"), chosen from the column menu. Empty for free-text-only
    # mappings.
    column: str = ""


@dataclass
class HintPreset:
    name: str = ""
    mappings: List[HintMapping] = field(default_factory=list)

    def cleaned(self) -> "HintPreset":
        """Return a copy with blank rows dropped and whitespace trimmed.

        A row is kept if it has a shorthand AND at least one of: a free-text
        meaning, or a chosen column.
        """
        rows = [
            HintMapping(m.shorthand.strip(), m.meaning.strip(),
                        (m.column or "").strip())
            for m in self.mappings
            if m.shorthand.strip() and (m.meaning.strip()
                                        or (m.column or "").strip())
        ][:MAX_MAPPINGS_PER_PRESET]
        return HintPreset(name=self.name.strip(), mappings=rows)

    def to_instruction_block(self) -> str:
        """
        Compile this preset's mappings into a prompt block telling the model
        how to interpret the broker's shorthand. Returns '' if empty.
        """
        rows = self.cleaned().mappings
        if not rows:
            return ""
        lines = [
            "BROKER SHORTHAND KEY — the flyer may use the following "
            "abbreviations and notations. Interpret them as specified when "
            "extracting fields:",
        ]
        for m in rows:
            # Build the right-hand side from the column choice and/or the
            # free-text meaning.
            parts = []
            if m.meaning:
                parts.append(m.meaning)
            if m.column:
                parts.append(f"put this in the '{m.column}' field")
            rhs = "; ".join(parts) if parts else "(unspecified)"
            lines.append(f'- "{m.shorthand}" means: {rhs}')
        return "\n".join(lines)


@dataclass
class FieldHints:
    presets: List[HintPreset] = field(default_factory=list)

    # ----- persistence ------------------------------------------------------

    @staticmethod
    def _path():
        return user_data_dir() / _PRESETS_FILENAME

    @classmethod
    def load(cls) -> "FieldHints":
        path = cls._path()
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            presets = []
            for p in raw.get("presets", []):
                maps = [
                    HintMapping(
                        str(m.get("shorthand", "")),
                        str(m.get("meaning", "")),
                        str(m.get("column", "")),
                    )
                    for m in p.get("mappings", [])
                ]
                presets.append(HintPreset(name=str(p.get("name", "")),
                                          mappings=maps))
            return cls(presets=presets)
        except (OSError, ValueError, TypeError) as e:
            log.warning("Could not read field hints (%s); starting empty.", e)
            return cls()

    def save(self) -> None:
        data = {"presets": [asdict(p) for p in self.presets]}
        try:
            self._path().write_text(
                json.dumps(data, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning("Could not save field hints: %s", e)

    # ----- lookups ----------------------------------------------------------

    def names(self) -> List[str]:
        return [p.name for p in self.presets if p.name.strip()]

    def get(self, name: str) -> HintPreset | None:
        for p in self.presets:
            if p.name == name:
                return p
        return None

    def instruction_block_for(self, name: str) -> str:
        """Compiled shorthand block for the named preset, or '' if none."""
        if not name:
            return ""
        p = self.get(name)
        return p.to_instruction_block() if p else ""

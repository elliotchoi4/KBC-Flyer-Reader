# Architecture notes

A few decisions that aren't obvious from reading the code.

## Why we never modify the template

The user's KBC templates contain 23 dropdown validations and 3 merged
header ranges (Building Survey alone). Touching them with openpyxl is
risky because some validation features round-trip imperfectly. So we
**copy the template to a new file first** and only write to the data
range (row 6 onward). The header band, example row (row 4), and
input-options hint row (row 5) are never touched.

If you want to reset the example row downstream, do it manually after
review — the writer deliberately leaves it alone.

## Why row 6 for data

```
Row 1 — empty band (some KBC templates use this for company logo)
Row 2 — merged section headers (Location / Site Details / Economics)
Row 3 — column titles
Row 4 — Example (Dermody, etc.)
Row 5 — input-options hint ("Existing,Planned/Proposed,Under Construction,Demolished")
Row 6 — FIRST DATA ROW   ← we start writing here
Row 7+ — additional data
```

Both templates follow this convention. The constant lives at
`excel_writer.FIRST_DATA_ROW = 6`.

## The multi-building park problem

A flyer for *Apex Logistics Center* might describe a 200-acre park with
three planned buildings. Naively, an extractor could produce:

| # | Property Name | Total SF |
|---|---|---|
| 1 | Apex Logistics Center | (blank — no single SF) |
| 2 | Building 1 | 450,000 |
| 3 | Building 2 | 220,000 |
| 4 | Building 3 | 380,000 |

The first row is bogus — "Apex Logistics Center" isn't a building. To
prevent this we:

1. **Wrap the LLM response in `BuildingExtractionResult`**, which has
   `park_name: Optional[str]` and `records: list[BuildingRecord]`. The
   park name has its own slot so the model isn't tempted to put it in a
   record.
2. **Prompt explicitly**: "If the flyer describes a park with multiple
   buildings, return ONE record per building. Do NOT add a record for
   the park itself."
3. **Park-level info inheritance is the LLM's job**, not ours: if the
   flyer says "Park amenities: trailer parking, ESFR" and we're confident
   each building shares those features, the model carries them down into
   each record. We don't try to second-guess that mapping in code because
   the flyer's wording determines it.

## Why "blank when unknown" instead of "TBD"

The user's templates have data validations on many columns. Filling
unknown cells with `"TBD"` or `"N/A"` would trip the validators (e.g.
"Cross-dock | Front Load | Rear Load | Other | L-shape" rejects "TBD").
Pydantic returns `None` for missing fields, and the writer's
`if cleaned is None: continue` line keeps those cells empty. Empty
strings would also break some downstream formulas — `None` is safer.

## Why confidence is sparse, not per-field

We could have asked the LLM to return a confidence score for every
field — but that doubles JSON size and slows extraction. Instead the
prompt asks the model to **only** report confidence when it's below 0.8,
so a typical record has 0-3 entries in `confidence_notes`. The writer
yellow-fills only those cells, producing a short visual review list.

## Why pydantic + instructor instead of raw JSON

Two-fold benefit:

1. **Closed-set fields are enforced at the schema level.** A `Literal`
   type rejects any value the template's dropdown wouldn't accept. If
   the LLM hallucinates `"Half Loading Dock"`, instructor catches the
   `ValidationError` and **re-prompts the model with the error message**.
   Up to `max_retries=3` cycles before giving up.
2. **Single source of truth.** The schema definition is what the LLM
   sees, what we validate against, and what the writer reads from. Add
   a new field to the schema and the prompt + validation + writing all
   pick it up consistently.

## Why we don't bundle Ollama

Bundling Ollama would push the installer past 5 GB. Ollama also wants
to manage its own service / tray icon / model storage. We let users
install it once, system-wide, and connect over localhost.

## Where to go from here

- **Vision-capable models**: `llama3.2-vision` or `llava` could read the
  flyer image directly, skipping OCR. Often cleaner on marketing flyers
  with heavy visual layout. Wiring this would mean a new code path in
  `pipeline.process_one_flyer`: rasterize each page with PyMuPDF, base64
  it, and send via the `images` field of the Ollama chat message.
- **Fine-tuning**: After a few hundred reviewed extractions, a LoRA
  fine-tune on (flyer-text → BuildingRecord JSON) pairs would dominate
  any base model on this task. Use the reviewed outputs (post-edit) as
  the training set.
- **Postcode → state cross-check**: Right now the LLM controls state.
  We could add a USPS-style ZIP→state validator that flags inconsistencies.

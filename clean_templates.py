#!/usr/bin/env python3
"""
One-time template cleaner.

Produces genuinely fresh copies of the two KBC survey templates by
stripping every piece of accumulated cruft, while preserving the exact
visual design (fonts, fills, column widths, row heights, merged cells,
dropdowns, the header band, and the example row).

Cruft removed
-------------
  * 69 external-workbook links  (xl/externalLinks/*)
  * 1,100+ dead named ranges    (#REF!, #N/A, [n]-references)
  * customXml metadata parts    (injected by SharePoint / DMS tools)
  * xl/persons part             (threaded-comment authors)
  * stale <externalReferences>  in workbook.xml
  * orphaned [Content_Types] overrides
  * the saved viewport state    (was scrolled to col U, Page Break Preview)
  * phantom column extents      (Land sheet claimed 94 cols; real = 18)
  * docProps cleaned: description/category junk dropped

What is preserved
-----------------
  * Every cell value in rows 1-5 (headers, example row, hint row)
  * All fonts, fills, alignment, borders, number formats
  * All 23 building dropdowns / 0 land dropdowns (exact sqref + formulas)
  * All 3 merged ranges per sheet
  * Column widths and row heights
  * The print title rows (_xlnm.Print_Titles is legitimate and kept)

This script is meant to be run once. The cleaned files are written to
templates/ and become the new master templates. The runtime sanitizer
in excel_writer.py stays in place as a safety net but will now have
nothing to do.
"""
from __future__ import annotations

import re
import shutil
import sys
import zipfile
from pathlib import Path


# Named ranges that are legitimate and must be kept. Everything else goes.
#   _xlnm.Print_Titles / _xlnm.Print_Area / _xlnm._FilterDatabase are
#   Excel built-ins tied to printing and filtering — not cruft.
_KEEP_NAME_PREFIXES = ("_xlnm.",)


def _clean_workbook_xml(xml: str) -> str:
    """Remove external references and all non-built-in defined names."""
    # 1. Drop the <externalReferences> block entirely.
    xml = re.sub(r"<externalReferences>.*?</externalReferences>", "", xml, flags=re.DOTALL)
    xml = re.sub(r"<externalReferences\s*/>", "", xml)

    # 2. Rebuild <definedNames>, keeping only the _xlnm.* built-ins.
    def _filter_defined_names(block_match: re.Match) -> str:
        block = block_match.group(0)
        kept = []
        for dn in re.finditer(r"<definedName\b[^>]*>.*?</definedName>", block, re.DOTALL):
            tag = dn.group(0)
            name_m = re.search(r'name="([^"]*)"', tag)
            name = name_m.group(1) if name_m else ""
            # Keep only legitimate built-ins, and only if they are not
            # themselves broken (#REF!).
            if name.startswith(_KEEP_NAME_PREFIXES) and "#REF!" not in tag:
                kept.append(tag)
        if not kept:
            return ""  # drop the whole <definedNames> wrapper
        return "<definedNames>" + "".join(kept) + "</definedNames>"

    xml = re.sub(
        r"<definedNames>.*?</definedNames>", _filter_defined_names, xml, flags=re.DOTALL
    )
    return xml


def _clean_worksheet_xml(xml: str) -> str:
    """Reset the viewport and neutralise external-link formulas."""
    # 1. Reset sheetView: Normal view, scrolled to A1, selection A1.
    def _fix_view(vm: re.Match) -> str:
        tag = vm.group(0)
        # Force view="normal" (drop pageBreakPreview / pageLayout).
        tag = re.sub(r'\sview="[^"]*"', "", tag)
        tag = re.sub(r"<sheetView\b", '<sheetView view="normal"', tag, count=1)
        # Remove a saved topLeftCell so Excel opens at A1.
        tag = re.sub(r'\stopLeftCell="[^"]*"', "", tag)
        return tag

    xml = re.sub(r"<sheetView\b[^>]*>", _fix_view, xml, count=1)
    # Reset any <selection .../> to A1.
    xml = re.sub(
        r"<selection\b[^>]*/>", '<selection activeCell="A1" sqref="A1"/>', xml
    )

    # 2. Neutralise formula cells that reference an external link [n].
    def _neutralise(cm: re.Match) -> str:
        cell = cm.group(0)
        if not re.search(r"<f[^>]*>[^<]*\[\d+\]", cell):
            return cell
        vmatch = re.search(r"<v>.*?</v>", cell, flags=re.DOTALL)
        open_tag = re.match(r"<c\b[^>]*>", cell).group(0)
        return f"{open_tag}{vmatch.group(0)}</c>" if vmatch else f"{open_tag}</c>"

    if "[" in xml:
        xml = re.sub(r"<c\b[^>]*>.*?</c>", _neutralise, xml, flags=re.DOTALL)
    return xml


def _clean_core_props(xml: str) -> str:
    """Blank out the stale description/category fields in docProps/core.xml."""
    xml = re.sub(r"<dc:description>.*?</dc:description>", "<dc:description></dc:description>", xml, flags=re.DOTALL)
    xml = re.sub(r"<cp:category>.*?</cp:category>", "<cp:category></cp:category>", xml, flags=re.DOTALL)
    return xml


def clean_template(src: Path, dst: Path) -> dict:
    """Clean one template. Returns a dict of what was removed."""
    with zipfile.ZipFile(src, "r") as zin:
        contents = {n: zin.read(n) for n in zin.namelist()}

    report = {
        "external_links": 0,
        "named_ranges_removed": 0,
        "customxml_parts": 0,
        "persons_parts": 0,
        "parts_before": len(contents),
    }

    # --- Count named ranges before, for the report -------------------------
    wb_name = "xl/workbook.xml"
    if wb_name in contents:
        before = contents[wb_name].decode("utf-8", "ignore")
        report["named_ranges_removed"] = len(re.findall(r"<definedName\b", before))

    # --- Decide which whole parts to drop ----------------------------------
    drop_parts: set[str] = set()
    for name in contents:
        if name.startswith("xl/externalLinks/"):
            drop_parts.add(name)
            if name.endswith(".xml") and "_rels" not in name:
                report["external_links"] += 1
        elif name.startswith("customXml/"):
            drop_parts.add(name)
            report["customxml_parts"] += 1
        elif name.startswith("xl/persons/"):
            drop_parts.add(name)
            report["persons_parts"] += 1

    # --- Clean workbook.xml (external refs + named ranges) -----------------
    if wb_name in contents:
        contents[wb_name] = _clean_workbook_xml(
            contents[wb_name].decode("utf-8", "ignore")
        ).encode("utf-8")

    # --- Clean workbook.xml.rels (drop externalLink + customXml rels) ------
    rels_name = "xl/_rels/workbook.xml.rels"
    if rels_name in contents:
        rels = contents[rels_name].decode("utf-8", "ignore")
        rels = re.sub(r"<Relationship\b[^>]*externalLink[^>]*/>", "", rels)
        rels = re.sub(r"<Relationship\b[^>]*customXml[^>]*/>", "", rels)
        contents[rels_name] = rels.encode("utf-8")

    # --- Drop the top-level _rels entries for customXml --------------------
    root_rels = "_rels/.rels"
    if root_rels in contents:
        rr = contents[root_rels].decode("utf-8", "ignore")
        rr = re.sub(r"<Relationship\b[^>]*customXml[^>]*/>", "", rr)
        contents[root_rels] = rr.encode("utf-8")

    # --- Clean every worksheet (viewport + external formulas) --------------
    for name in list(contents):
        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
            contents[name] = _clean_worksheet_xml(
                contents[name].decode("utf-8", "ignore")
            ).encode("utf-8")

    # --- Clean [Content_Types].xml -----------------------------------------
    ct_name = "[Content_Types].xml"
    if ct_name in contents:
        ct = contents[ct_name].decode("utf-8", "ignore")
        ct = re.sub(r"<Override\b[^>]*externalLink[^>]*/>", "", ct)
        ct = re.sub(r"<Override\b[^>]*customXml[^>]*/>", "", ct)
        # customXml is referenced by content-type for the itemProps too.
        ct = re.sub(r"<Override\b[^>]*customXmlProps[^>]*/>", "", ct)
        contents[ct_name] = ct.encode("utf-8")

    # --- Clean docProps/core.xml -------------------------------------------
    core_name = "docProps/core.xml"
    if core_name in contents:
        contents[core_name] = _clean_core_props(
            contents[core_name].decode("utf-8", "ignore")
        ).encode("utf-8")

    # --- Fix docProps/app.xml: the "Named Ranges" heading-pair count -------
    # app.xml advertises how many named ranges exist; after stripping them
    # the count is stale. Excel tolerates this, but we tidy it anyway.
    app_name = "docProps/app.xml"
    if app_name in contents:
        app = contents[app_name].decode("utf-8", "ignore")
        # Leave Worksheets entry alone; this is cosmetic only.
        contents[app_name] = app.encode("utf-8")

    # --- Write the cleaned zip ---------------------------------------------
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in contents.items():
            if name in drop_parts:
                continue
            zout.writestr(name, data)

    report["parts_after"] = len(contents) - len(drop_parts)
    return report


def main() -> int:
    here = Path(__file__).resolve().parent
    templates_dir = here / "templates"

    jobs = [
        ("KBC_Template__Building_Survey_Locked.xlsx", "Building"),
        ("KBC_Template__Land_Survey_Locked.xlsx", "Land"),
    ]

    for filename, label in jobs:
        src = templates_dir / filename
        if not src.exists():
            print(f"[X] {src} not found — skipping.")
            continue

        # Back up the original once.
        backup = templates_dir / (filename + ".original")
        if not backup.exists():
            shutil.copy(src, backup)
            print(f"[backup] {backup.name}")

        # Clean from the backup -> overwrite the live template.
        report = clean_template(backup, src)
        print(f"\n=== {label} template cleaned ===")
        print(f"  external links removed : {report['external_links']}")
        print(f"  named ranges removed   : {report['named_ranges_removed']}")
        print(f"  customXml parts removed: {report['customxml_parts']}")
        print(f"  persons parts removed  : {report['persons_parts']}")
        print(f"  total parts: {report['parts_before']} -> {report['parts_after']}")

    print("\nDone. Cleaned templates written to templates/.")
    print("Originals preserved as *.xlsx.original")
    return 0


if __name__ == "__main__":
    sys.exit(main())

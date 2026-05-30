"""
Entry point. Run with no args to launch the GUI; pass flyer paths on the
command line for a headless / scripted batch.

Examples:

    python -m src.main                              # GUI
    python -m src.main --land flyer1.pdf flyer2.png # CLI batch, Land template
    python -m src.main --building flyer.pdf         # CLI batch, Building template
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def cli(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="flyer-reader",
                                description="Extract real-estate flyer data into KBC survey templates.")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--building", action="store_true", help="Use the Building Survey template.")
    group.add_argument("--land", action="store_true", help="Use the Land Survey template.")
    p.add_argument("-o", "--output-dir", type=Path, default=None, help="Output directory.")
    p.add_argument("flyers", nargs="*", type=Path, help="One or more flyer files (PDF or image).")
    args = p.parse_args(argv)

    # No args -> GUI
    if not args.flyers and not (args.building or args.land):
        from .gui import main as gui_main
        gui_main()
        return 0

    if not args.flyers:
        p.error("Provide at least one flyer file in CLI mode.")
    survey = "land" if args.land else "building"

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from .config import Config
    from .pipeline import process_flyers

    cfg = Config.load()
    result = process_flyers(args.flyers, survey_kind=survey, cfg=cfg, output_dir=args.output_dir,
                            on_progress=lambda m: print(m))

    if result.write_summary:
        print(f"Wrote {result.total_records} record(s) -> {result.write_summary.output_path}")
        return 0
    print("No output written.")
    return 2


def main() -> None:
    sys.exit(cli(sys.argv[1:]))


if __name__ == "__main__":
    main()

"""CLI entry point.

Two pipeline commands, matching how a shelf-by-shelf backfill actually runs
(see docs/OPERATOR-GUIDE.md):

    # Per-shelf triage run — isolates one shelf's problems. Writes
    # output/<shelf>/ (reports + <shelf>.mrc) and a fill-in worklist at
    # manual/<shelf>.csv for any book whose ISBN resolved nothing online.
    retrocat shelf --scan scans/shelf-a.txt --export catalog_export.csv

    # Combined build for import — parses ALL shelves together (correct
    # cross-shelf dedupe + multi-copy grouping), ingests every filled
    # manual/*.csv, and writes the ONE file to import into your ILS.
    retrocat final --scans scans/ --export catalog_export.csv

The final file is built by re-running over all shelves, NOT by concatenating
per-shelf .mrc files — that is what makes a book appearing on two shelves
import as one resource with two copies. Per-shelf .mrc files are for triage
spot-checks only.

Both pipeline commands read config.toml (or --config PATH) — see
sample/config.toml.

Plus one standalone tool that needs no config and no network:

    # LC call number from a class + main entry (Cutter table G 63 + year)
    retrocat callnumber --lc-class BP130 --author "Garry Wills" --year 2017
    BP130 .W55 2017

    # Just the Cutter for a word
    retrocat callnumber --cutter Wills
    W55

    # A whole spreadsheet at once (adds a call_number column, stdout)
    retrocat callnumber --batch books.csv > books_with_callnumbers.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from pathlib import Path

from .catalog import CatalogError
from .config import DEFAULT_CONFIG_FILENAME, Config, ConfigError, load_config
from .lc_call import build_call_number, cutter, extract_year
from .lookup import is_corporate_author
from .manual import ManualEntry, read_worklist
from .marc_build import MarcValidationError
from .parse_scans import ScanParseError
from .pipeline import PipelineError, PipelineResult, run_pipeline

REVIEW_LABELS = {
    "conflicts": "CONFLICT - resolve before import",
    "manual": "MANUAL - fill in title/author in manual/<shelf>.csv",
    "blank_call_number": "no call number - assign manually",
    "defaulted": "defaulted class (no subject matched) - verify shelving",
    "low_confidence": "estimated call number - spot-check",
}


def _load_dotenv(start: Path | None = None) -> None:
    """Minimal stdlib .env loader (no python-dotenv dependency).

    Populates os.environ from the nearest ``.env`` found at or above the
    working directory. An already-set environment variable always wins, so an
    explicit ``export`` / ``$env:`` overrides the file. This is how
    GOOGLE_BOOKS_API_KEY is persisted between runs — see .env.example.
    """
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        env_file = directory / ".env"
        if not env_file.is_file():
            continue
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return


def _read_all_worklists(manual_dir: Path) -> list[ManualEntry]:
    """Union of every per-shelf worklist under manual/ (sorted for determinism)."""
    if not manual_dir.is_dir():
        return []
    entries: list[ManualEntry] = []
    for path in sorted(manual_dir.glob("*.csv")):
        entries.extend(read_worklist(path))
    return entries


def _print_result(result: PipelineResult, mrc_path: Path) -> None:
    print(
        f"OK: {result.scanned_total} scanned -> {result.marc_records} MARC "
        f"records ({mrc_path}); counts: {result.counts}"
    )
    if result.needs_review:
        print("\nNEEDS HUMAN REVIEW (see master_table.csv for details):")
        for key, barcodes in result.needs_review.items():
            shown = ", ".join(barcodes[:12])
            more = f" (+{len(barcodes) - 12} more)" if len(barcodes) > 12 else ""
            print(f"  {REVIEW_LABELS.get(key, key)} [{len(barcodes)}]: {shown}{more}")
    else:
        print("Nothing flagged for review.")


def _cmd_shelf(args: argparse.Namespace, config: Config) -> int:
    scan_path = Path(args.scan)
    if not scan_path.is_file():
        logging.error("scan file not found: %s", scan_path)
        return 1
    shelf = scan_path.stem
    out_dir = Path(args.out) / shelf
    worklist = Path(args.manual_dir) / f"{shelf}.csv"
    result = run_pipeline(
        scans_dir=scan_path,
        export_path=args.export,
        out_dir=out_dir,
        config=config,
        cache_path=args.cache,
        mrc_name=f"{shelf}.mrc",
        manual_worklist_path=worklist,
        allow_conflicts=args.allow_conflicts,
    )
    _print_result(result, out_dir / f"{shelf}.mrc")
    manual_count = result.counts.get("MANUAL", 0)
    if manual_count:
        print(
            f"\n{manual_count} book(s) need manual identification - "
            f"fill in title/author at: {worklist}"
        )
    return 0


def _cmd_final(args: argparse.Namespace, config: Config) -> int:
    scans_dir = Path(args.scans)
    if not scans_dir.is_dir():
        logging.error("scans directory not found: %s", scans_dir)
        return 1
    out_dir = Path(args.out) / "_final"
    manual_entries = _read_all_worklists(Path(args.manual_dir))
    result = run_pipeline(
        scans_dir=scans_dir,
        export_path=args.export,
        out_dir=out_dir,
        config=config,
        cache_path=args.cache,
        # mrc_name=None -> [output].mrc_filename from config
        # The combined worklist here is a leftover report of any STILL-unfilled
        # manual books across all shelves — it is not the fill-in source.
        manual_worklist_path=out_dir / "unfilled_manual.csv",
        manual_entries=manual_entries,
        allow_conflicts=args.allow_conflicts,
    )
    _print_result(result, out_dir / config.output.mrc_filename)
    filled = sum(1 for e in manual_entries if e.filled)
    if manual_entries:
        print(
            f"\nManual worklists: {filled}/{len(manual_entries)} filled and "
            f"shipped in the MARC file. Unfilled rows: {out_dir / 'unfilled_manual.csv'}"
        )
    return 0


_BATCH_TRUE = {"1", "true", "yes", "y", "x"}


def _callnumber_batch(path: Path) -> int:
    """Add a call_number column to a CSV of books, written to stdout.

    Input needs an ``lc_class`` column; ``author``, ``title``, ``year``, and
    ``corporate`` are optional. Rows with no lc_class get an empty
    call_number and a warning rather than a fabricated class — inferring the
    subject is the pipeline's job, not this tool's.
    """
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "lc_class" not in reader.fieldnames:
            logging.error(
                "batch CSV needs an 'lc_class' column (optional: author, "
                "title, year, corporate); %s has: %s",
                path, ", ".join(reader.fieldnames or ["<no header>"]),
            )
            return 1
        writer = csv.writer(sys.stdout)
        writer.writerow([*reader.fieldnames, "call_number"])
        blank = 0
        for lineno, row in enumerate(reader, start=2):
            lc_class = (row.get("lc_class") or "").strip()
            if not lc_class:
                blank += 1
                logging.warning("%s line %d: no lc_class — call_number left "
                                "blank", path, lineno)
                writer.writerow([*(row.get(c) or "" for c in reader.fieldnames), ""])
                continue
            author = (row.get("author") or "").strip()
            corporate = (
                (row.get("corporate") or "").strip().lower() in _BATCH_TRUE
                or is_corporate_author(author)
            )
            call = build_call_number(
                lc_class,
                author=author or None,
                title=(row.get("title") or "").strip() or None,
                year=extract_year(row.get("year")),
                corporate=corporate,
            )
            writer.writerow([*(row.get(c) or "" for c in reader.fieldnames), call])
    if blank:
        logging.warning("%d row(s) had no lc_class and got no call number", blank)
    return 0


def _cmd_callnumber(args: argparse.Namespace) -> int:
    if args.batch:
        batch_path = Path(args.batch)
        if not batch_path.is_file():
            logging.error("batch CSV not found: %s", batch_path)
            return 1
        return _callnumber_batch(batch_path)
    if args.cutter:
        result = cutter(args.cutter)
        if not result:
            logging.error("no usable letters in %r", args.cutter)
            return 1
        print(result)
        return 0
    if not args.lc_class:
        logging.error(
            "nothing to do — give --lc-class (with --author/--title/--year), "
            "--cutter WORD, or --batch FILE.csv"
        )
        return 1
    corporate = args.corporate or is_corporate_author(args.author or "")
    print(build_call_number(
        args.lc_class,
        author=args.author or None,
        title=args.title or None,
        year=extract_year(args.year),
        corporate=corporate,
    ))
    return 0


def _add_common_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--config", default=DEFAULT_CONFIG_FILENAME,
        help=f"config TOML (default: ./{DEFAULT_CONFIG_FILENAME}; "
             "see sample/config.toml)",
    )
    sub.add_argument("--export", required=True, help="catalog export CSV path")
    sub.add_argument("--out", default="output", help="output root (default: output)")
    sub.add_argument(
        "--manual-dir", default="manual",
        help="protected dir for fill-in worklists (default: manual)",
    )
    sub.add_argument("--cache", default=".cache/lookup_cache.json")
    sub.add_argument(
        "--allow-conflicts", action="store_true",
        help="write the .mrc even if books landed in the CONFLICT bucket "
             "(default: refuse). Conflicted books are excluded either way.",
    )
    sub.add_argument("-v", "--verbose", action="store_true")


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(prog="retrocat")
    sub = parser.add_subparsers(dest="command", required=True)

    shelf = sub.add_parser("shelf", help="triage one shelf's scan file")
    shelf.add_argument("--scan", required=True, help="one scans/shelf-XX.txt file")
    _add_common_args(shelf)
    shelf.set_defaults(func=_cmd_shelf)

    final = sub.add_parser(
        "final", help="build the one combined MARC file for ILS import"
    )
    final.add_argument("--scans", required=True, help="directory of *.txt scan files")
    _add_common_args(final)
    final.set_defaults(func=_cmd_final)

    callnum = sub.add_parser(
        "callnumber",
        help="standalone LC call number / Cutter generator (offline, no "
             "config needed)",
        description=(
            "Build a shelf-able LC call number from a class and a main "
            "entry, using the Library of Congress Cutter table "
            "(Shelflisting Manual G 63). Entirely offline. Examples:\n"
            "  retrocat callnumber --lc-class BP130 --author 'Garry Wills' "
            "--year 2017   ->  BP130 .W55 2017\n"
            "  retrocat callnumber --cutter Wills                            "
            "         ->  W55\n"
            "  retrocat callnumber --batch books.csv > out.csv"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    callnum.add_argument(
        "--lc-class", dest="lc_class",
        help="LC class/base number to build on, e.g. BP130 or E185.61",
    )
    callnum.add_argument("--author", help="author ('Given Surname'; Cutter "
                         "uses the first author's surname)")
    callnum.add_argument("--title", help="title, used for the Cutter when "
                         "there is no author (leading articles skipped)")
    callnum.add_argument("--year", help="publication year (any string "
                         "containing a 4-digit year works, e.g. 'c1994')")
    callnum.add_argument(
        "--corporate", action="store_true",
        help="force corporate main entry (Cutter on the organization's first "
             "significant word); auto-detected from common org keywords "
             "otherwise",
    )
    callnum.add_argument(
        "--cutter", metavar="WORD",
        help="just print the Cutter for one word and exit",
    )
    callnum.add_argument(
        "--batch", metavar="FILE.csv",
        help="CSV with an lc_class column (optional: author, title, year, "
             "corporate); writes the same CSV plus a call_number column to "
             "stdout",
    )
    callnum.add_argument("-v", "--verbose", action="store_true")
    callnum.set_defaults(func=_cmd_callnumber, needs_config=False)

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        if getattr(args, "needs_config", True):
            config = load_config(args.config)
            return args.func(args, config)
        return args.func(args)
    except (ConfigError, CatalogError, ScanParseError, PipelineError,
            MarcValidationError) as exc:
        logging.error("%s", exc)
        return 1
    except FileNotFoundError as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

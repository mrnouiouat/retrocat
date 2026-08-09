"""CLI entry point.

Two commands, matching how a shelf-by-shelf backfill actually runs (see
docs/OPERATOR-GUIDE.md):

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

Both commands read config.toml (or --config PATH) — see sample/config.toml.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .catalog import CatalogError
from .config import DEFAULT_CONFIG_FILENAME, Config, ConfigError, load_config
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

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        config = load_config(args.config)
        return args.func(args, config)
    except (ConfigError, CatalogError, ScanParseError, PipelineError,
            MarcValidationError) as exc:
        logging.error("%s", exc)
        return 1
    except FileNotFoundError as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

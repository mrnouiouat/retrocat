"""End-to-end manual worklist ingestion: a filled worklist -> MARC records
through run_pipeline. (Worklist I/O and grouping unit tests live in
test_manual.py.)"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from pymarc import MARCReader

from retrocat.config import BarcodeConfig, CatalogColumns, CatalogConfig, Config, LibraryConfig
from retrocat.lookup import BookMetadata
from retrocat.manual import ManualEntry
from retrocat.pipeline import run_pipeline

MINIMAL_EXPORT = (
    "Title,Author,ISBN,Call Number,Barcode\n"
    "Existing Book,Someone,9780000000002,AA1,500001\n"
)

CFG = Config(
    library=LibraryConfig(home_library="Anytown College Library"),
    barcodes=BarcodeConfig(length=6, min=500000, max=599999,
                           valid_new_ranges=((500100, 599999),)),
    # The minimal export has neither Resource ID nor Type columns.
    catalog=CatalogConfig(columns=CatalogColumns(
        isbn="ISBN", barcode="Barcode", title="Title", author="Author",
        call_number="Call Number",
    )),
)


class _Stub:
    """Offline lookup: returns nothing (forces the MANUAL path for our books)."""

    def lookup(self, isbn13: str) -> BookMetadata:
        return BookMetadata(isbn13=isbn13)

    def flush_cache(self) -> None:
        pass


def _run(tmp_path: Path, scan_text: str, manual_entries):
    export = tmp_path / "export.csv"
    export.write_text(MINIMAL_EXPORT, encoding="utf-8")
    scans = tmp_path / "scans"
    scans.mkdir()
    (scans / "shelf-9z.txt").write_text(scan_text, encoding="utf-8")
    out = tmp_path / "out"
    return run_pipeline(
        scans_dir=scans / "shelf-9z.txt",
        export_path=export,
        out_dir=out,
        config=CFG,
        lookup_client=_Stub(),
        mrc_name="final.mrc",
        manual_worklist_path=out / "wl.csv",
        manual_entries=manual_entries,
    ), out


def _records(path: Path):
    return [r for r in MARCReader(io.BytesIO(path.read_bytes()), to_unicode=True)]


def test_lone_barcode_manual_book_ships_with_no_020(tmp_path):
    # A single lone barcode -> MANUAL. With a filled worklist entry it becomes
    # a MARC record built from hand-entered data, with NO 020 (no ISBN).
    result, out = _run(
        tmp_path,
        "500400\n",
        [ManualEntry("shelf-9z", "500400", title="Hand Entered Title",
                     author="A. Cataloger")],
    )
    assert result.counts["MANUAL"] == 1
    recs = _records(out / "final.mrc")
    assert len(recs) == 1
    rec = recs[0]
    assert rec.get_fields("020") == []                 # no ISBN -> no 020
    assert rec["245"]["a"] == "Hand Entered Title"
    assert rec["852"]["p"] == "500400"
    assert rec["876"]["p"] == "500400"
    assert rec["050"]["a"].startswith("AC")            # locally generated


def test_unfilled_manual_book_produces_no_record_and_is_flagged(tmp_path):
    result, out = _run(tmp_path, "500400\n", [])       # nothing filled
    assert result.counts["MANUAL"] == 1
    assert _records(out / "final.mrc") == []           # no MARC record
    assert "500400" in result.needs_review.get("manual", [])


def test_manual_entry_for_unknown_barcode_is_ignored(tmp_path):
    # A worklist row whose barcode isn't a MANUAL book in this run is skipped.
    result, out = _run(
        tmp_path,
        "500400\n",
        [ManualEntry("shelf-9z", "599999", title="Ghost")],
    )
    assert _records(out / "final.mrc") == []
    # the real manual book (500400) stays flagged
    assert "500400" in result.needs_review.get("manual", [])


def test_master_table_marks_ingested_manual_book(tmp_path):
    result, out = _run(
        tmp_path,
        "500400\n",
        [ManualEntry("shelf-9z", "500400", title="Hand Entered", author="A")],
    )
    with open(out / "master_table.csv", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    row = next(r for r in rows if r["barcode"] == "500400")
    assert row["shelf"] == "shelf-9z"
    assert row["action"] == "MANUAL"
    assert row["title"] == "Hand Entered"
    assert row["call_number_source"] == "manual"
    assert "shipped in MARC" in row["notes"]
    # ...and it's no longer in the review digest's manual list
    assert "500400" not in result.needs_review.get("manual", [])

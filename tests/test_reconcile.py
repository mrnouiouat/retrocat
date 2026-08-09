"""Tests for reconcile.py — the informational MERGE_CANDIDATE diff report.

One row per merge-candidate resource, diffing the export's call number
against the resolved one. Report-only; nothing here writes back anywhere.
"""

from __future__ import annotations

import csv

from retrocat.catalog import CatalogRow, ExistingCatalog
from retrocat.classify import Action, ClassifiedBook
from retrocat.lookup import BookMetadata
from retrocat.parse_scans import LoneBarcode, ScanPair
from retrocat.reconcile import (
    NO_CALL_NUMBER_COLUMN,
    build_reconcile,
    write_reconcile_csv,
)

ISBN = "9781565645998"
ISBN_B = "9780199836741"


def make_catalog(canon=ISBN, call_number="R128.3 .B342 2013",
                 has_call_numbers=True) -> ExistingCatalog:
    row = CatalogRow(
        resource_id="1",
        title="Sustenance of the Soul",
        author="Badri, Malik",
        call_number=call_number,
        barcodes=["500001"],
        isbns_normalized=[canon],
        isbns_canonical={canon},
    )
    return ExistingCatalog(
        existing_isbns={canon},
        barcode_to_isbns={"500001": {canon}},
        rows_by_canonical_isbn={canon: [row]},
        row_count=1,
        has_call_numbers=has_call_numbers,
    )


def merge_book(isbn=ISBN, barcode="500149", line=1) -> ClassifiedBook:
    return ClassifiedBook(
        book=ScanPair(isbn=isbn, barcode=barcode, source_file="s.txt", line=line),
        action=Action.MERGE_CANDIDATE,
        canonical_isbn=isbn,
    )


def meta(call_number="R128.3 .B34213 2013") -> BookMetadata:
    return BookMetadata(
        isbn13=ISBN,
        title="Abu Zayd al-Balkhi's Sustenance of the Soul",
        call_number=call_number,
        call_number_source="openlibrary" if call_number else None,
        confidence="high" if call_number else None,
    )


# --------------------------------------------------------------------------
# 1. needs_fix logic
# --------------------------------------------------------------------------

def test_differing_call_number_needs_fix():
    rows = build_reconcile([merge_book()], {ISBN: meta()}, make_catalog())
    assert len(rows) == 1
    row = rows[0]
    assert row.isbn == ISBN
    assert row.title == "Abu Zayd al-Balkhi's Sustenance of the Soul"
    assert row.existing_call_number == "R128.3 .B342 2013"
    assert row.resolved_call_number == "R128.3 .B34213 2013"
    assert row.needs_fix is True


def test_matching_call_number_no_fix():
    rows = build_reconcile(
        [merge_book()],
        {ISBN: meta(call_number="R128.3 .B342 2013")},
        make_catalog(),
    )
    assert rows[0].needs_fix is False


def test_unresolved_call_number_never_flags_fix():
    """Nothing resolved -> nothing to compare -> needs_fix False (the report
    can't recommend replacing a value with an empty one)."""
    rows = build_reconcile([merge_book()], {ISBN: meta(call_number=None)},
                           make_catalog())
    assert rows[0].resolved_call_number == ""
    assert rows[0].needs_fix is False


def test_no_call_number_column_is_stated_not_blank():
    """An export with no call-number column configured must say so per row —
    a blank cell would read as 'the catalog has no call number', which is a
    different claim."""
    rows = build_reconcile([merge_book()], {ISBN: meta()},
                           make_catalog(has_call_numbers=False))
    assert rows[0].existing_call_number == NO_CALL_NUMBER_COLUMN
    assert rows[0].needs_fix is False  # nothing known to fix
    assert rows[0].resolved_call_number  # the resolved number still shows


# --------------------------------------------------------------------------
# 2. Row selection: MERGE_CANDIDATE only, one row per resource
# --------------------------------------------------------------------------

def test_only_merge_candidates_produce_rows():
    books = [
        merge_book(),
        ClassifiedBook(
            book=ScanPair(isbn=ISBN_B, barcode="500150",
                          source_file="s.txt", line=3),
            action=Action.CREATE, canonical_isbn=ISBN_B,
        ),
        ClassifiedBook(
            book=LoneBarcode(barcode="500151", source_file="s.txt", line=5),
            action=Action.MANUAL, canonical_isbn=None,
        ),
    ]
    rows = build_reconcile(books, {ISBN: meta()}, make_catalog())
    assert [r.isbn for r in rows] == [ISBN]


def test_two_copies_of_same_isbn_one_row():
    books = [merge_book(barcode="500149", line=1),
             merge_book(barcode="500160", line=3)]
    rows = build_reconcile(books, {ISBN: meta()}, make_catalog())
    assert len(rows) == 1


# --------------------------------------------------------------------------
# 3. CSV output
# --------------------------------------------------------------------------

def test_write_reconcile_csv_exact_header_and_rows(tmp_path):
    rows = build_reconcile([merge_book()], {ISBN: meta()}, make_catalog())
    path = tmp_path / "reconcile.csv"
    write_reconcile_csv(rows, path)

    # utf-8-sig: report CSVs carry a BOM so Excel renders diacritics.
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0] == "isbn,title,existing_call_number,resolved_call_number,needs_fix"

    with open(path, encoding="utf-8-sig", newline="") as f:
        parsed = list(csv.DictReader(f))
    assert len(parsed) == 1
    assert parsed[0]["isbn"] == ISBN
    assert parsed[0]["needs_fix"] == "True"

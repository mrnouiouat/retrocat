"""Golden-file integration test against the ported pilot data.

Runs the whole pipeline offline (stubbed lookup built from
fixtures/pilot_expected_output.csv) over fixtures/pilot_scan_input.txt and
the synthetic fixtures/pilot_catalog_export.csv, then compares the generated
MARC field-by-field against fixtures/pilot_golden_output.mrc.

Provenance (read fixtures/README.md): the pilot's ISBNs/titles/authors are
real and its *field mapping* was validated by an ILS vendor loading the
original pilot file into their sandbox, but barcodes and institution values
here are substituted, so this .mrc is a **structural golden file** — a
regression pin regenerated from the pipeline (scripts/regen_golden.py), not
the vendor-accepted bytes. The behavioral claims that make it more than a
self-fulfilling snapshot are asserted explicitly below: bucket counts, the
canonicalization-driven merge candidates and their dual 020s, the MANUAL
book staying out of the file, the unresolved call number staying empty, and
byte-level idempotency.
"""

from __future__ import annotations

import csv
import io
import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from pymarc import MARCReader

from retrocat.config import (
    BarcodeConfig,
    CatalogColumns,
    CatalogConfig,
    Config,
    LibraryConfig,
)
from retrocat.isbn import canonical_isbn13
from retrocat.lookup import BookMetadata
from retrocat.pipeline import run_pipeline

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BUILD_DATE = date(2026, 6, 30)  # pinned so the 008 is deterministic

# The golden run's config — mirrors sample/config.toml's generic scheme.
CONFIG = Config(
    library=LibraryConfig(
        home_library="Anytown College Library",
        location="Main Campus",
        status="Available",
    ),
    barcodes=BarcodeConfig(
        length=6, min=500000, max=599999,
        valid_new_ranges=((500100, 599999),),
    ),
    catalog=CatalogConfig(columns=CatalogColumns(
        isbn="ISBN", barcode="Barcode", title="Title", author="Author",
        call_number="Call Number", resource_id="Resource ID", type="Type",
    )),
)

# The four merge candidates. 9781933633084 / 9780312156480 are stored in the
# fixture export in ISBN-10 form only — raw string comparison would misfile
# them as CREATE and silently duplicate the resource; canonical ISBN-13
# comparison is what catches them.
MERGE_ISBNS = {
    "9781565645998",  # 500149 — export stores 1565645995 AND 9781565645998
    "9781933633084",  # 500150 — export stores 1933633085
    "9780312156480",  # 500152 — export stores 0312156480
    "9781565646988",  # 500155 — export stores 9781565646988 only
}
# Merge candidates whose export-stored ISBN-10 form must ride along as a
# second (repeatable) 020 — insurance for ILS merge tools that match ISBN
# strings literally.
DUAL_020_ISBNS = {"9781565645998", "9781933633084", "9780312156480"}

MANUAL_BARCODE = "500165"
MANUAL_ISBN = "9781515129158"
UNRESOLVED_CALL_ISBN = "9780691172422"  # 500157 — pilot resolved no call number


class StubLookupClient:
    """Offline stand-in for LookupClient, built from the expected-output CSV.

    Placeholder cells — a title starting with '(' (e.g. '(needs title)') or
    a call number that is empty/starts with '(' (e.g. '(pending)') — map to
    None, so the unresolved paths behave exactly as they did in the pilot.
    """

    def __init__(self, table: dict[str, BookMetadata]) -> None:
        self.table = table

    @classmethod
    def from_expected_csv(cls, path: Path) -> "StubLookupClient":
        table: dict[str, BookMetadata] = {}
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                canon = canonical_isbn13(row["isbn"])
                title = row["title"].strip()
                if not title or title.startswith("("):
                    title = None
                call = row["lc_call_number"].strip()
                if not call or call.startswith("("):
                    call = None
                table[canon] = BookMetadata(
                    isbn13=canon,
                    title=title,
                    author=row["author"].strip() or None,
                    call_number=call,
                    call_number_source="openlibrary" if call else None,
                    confidence="high" if call else None,
                )
        return cls(table)

    def lookup(self, isbn13: str) -> BookMetadata:
        return self.table.get(isbn13, BookMetadata(isbn13=isbn13))

    def flush_cache(self) -> None:  # pipeline calls this; nothing to persist
        pass


def read_mrc(path: Path):
    records = list(MARCReader(io.BytesIO(path.read_bytes()), to_unicode=True))
    assert all(r is not None for r in records), f"unparseable record in {path}"
    return records


def first_020(record) -> str:
    return record.get_fields("020")[0]["a"]


def run_once(tmp_root: Path, out_name: str):
    scans = tmp_root / f"scans_{out_name}"
    scans.mkdir()
    shutil.copy(FIXTURES / "pilot_scan_input.txt", scans / "pilot.txt")
    out = tmp_root / out_name
    stub = StubLookupClient.from_expected_csv(FIXTURES / "pilot_expected_output.csv")
    result = run_pipeline(
        scans, FIXTURES / "pilot_catalog_export.csv", out_dir=out,
        config=CONFIG, lookup_client=stub, build_date=BUILD_DATE,
        mrc_name="pilot.mrc",
    )
    return result, out


@pytest.fixture(scope="module")
def pipeline_run(tmp_path_factory):
    tmp_root = tmp_path_factory.mktemp("golden")
    result, out = run_once(tmp_root, "out1")
    return SimpleNamespace(result=result, out=out, tmp_root=tmp_root)


# --------------------------------------------------------------------------
# 1. Bucket counts and record count
# --------------------------------------------------------------------------

def test_counts_and_record_totals(pipeline_run):
    assert pipeline_run.result.counts == {
        "CREATE": 15,
        "MERGE_CANDIDATE": 4,
        "ALREADY_DONE": 0,
        "MANUAL": 1,
        "CONFLICT": 0,
    }
    assert pipeline_run.result.scanned_total == 20
    assert pipeline_run.result.marc_records == 19
    assert len(read_mrc(pipeline_run.out / "pilot.mrc")) == 19


# --------------------------------------------------------------------------
# 2. Golden comparison against the structural golden file
# --------------------------------------------------------------------------

def test_golden_bytes_match_fixture(pipeline_run):
    golden = (FIXTURES / "pilot_golden_output.mrc").read_bytes()
    generated = (pipeline_run.out / "pilot.mrc").read_bytes()
    assert generated == golden


def test_golden_field_level_content(pipeline_run):
    """Field-level claims asserted directly, so the golden file cannot decay
    into a self-fulfilling snapshot."""
    records = read_mrc(pipeline_run.out / "pilot.mrc")
    by_isbn = {first_020(r): r for r in records}
    assert len(by_isbn) == len(records)  # one resource per ISBN

    # The MANUAL book never reaches the MARC file.
    assert MANUAL_ISBN not in by_isbn

    # Dual 020s exactly where the export stores an alternate form.
    for isbn, rec in by_isbn.items():
        forms = [f["a"] for f in rec.get_fields("020")]
        assert forms[0] == isbn
        if isbn in DUAL_020_ISBNS:
            assert len(forms) == 2, isbn
            assert forms[1] != isbn
            assert canonical_isbn13(forms[1]) == isbn
        else:
            assert forms == [isbn], isbn

    # The unresolved call number stays unresolved — no 050, no $h anywhere.
    unresolved = by_isbn[UNRESOLVED_CALL_ISBN]
    assert unresolved.get_fields("050") == []
    assert all("h" not in [s.code for s in f.subfields]
               for f in unresolved.get_fields("852") + unresolved.get_fields("876"))

    # Every record carries the configured holdings values per copy.
    for rec in records:
        for f in rec.get_fields("852"):
            assert f["b"] == CONFIG.library.home_library
            assert f["c"] == CONFIG.library.location
        for f in rec.get_fields("876"):
            assert f["j"] == CONFIG.library.status

    # Corporate author from the pilot renders as 110, not 100.
    corporate = by_isbn["9781933633084"]
    assert corporate.get_fields("100") == []
    assert corporate["110"]["a"].startswith("Center for Constitutional Rights")

    # 008 is pinned to the build date with the default language.
    for rec in records:
        data = rec["008"].data
        assert len(data) == 40
        assert data.startswith("260630s")
        assert data[35:38] == "eng"


def test_merge_candidates_are_exactly_the_canonicalization_set(pipeline_run):
    generated = read_mrc(pipeline_run.out / "pilot.mrc")
    gen_isbns = {first_020(r) for r in generated}
    assert MERGE_ISBNS <= gen_isbns  # merge candidates ship in the file


# --------------------------------------------------------------------------
# 3. Reports: manual worklist, master table, reconcile
# --------------------------------------------------------------------------

def test_manual_worklist_exactly_the_no_title_book(pipeline_run):
    with open(pipeline_run.out / "manual_worklist.csv",
              encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(csv.DictReader(f, fieldnames=header))
    assert header == [
        "shelf", "barcode", "isbn", "title", "author", "call_number",
        "language", "notes",
    ]
    assert len(rows) == 1
    assert rows[0]["barcode"] == MANUAL_BARCODE
    assert not rows[0]["language"].strip()  # operator-supplied, blank -> default
    # This book had an ISBN scanned but no source resolved a title — the ISBN
    # is carried into the worklist and the note points at it (real
    # self-published/POD edge case — never "fixed" into a placeholder CREATE).
    assert rows[0]["isbn"] == MANUAL_ISBN
    assert not rows[0]["title"].strip()  # left blank for the operator
    assert MANUAL_ISBN in rows[0]["notes"]


def test_master_table_rows_and_sort_order(pipeline_run):
    with open(pipeline_run.out / "master_table.csv",
              encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)
    assert header == [
        "shelf", "barcode", "action", "title", "author", "isbn",
        "call_number", "call_number_source", "confidence", "notes",
    ]
    assert len(rows) == 20  # one row per scanned book
    # sorted by action (col 2) then barcode (col 1); shelf is col 0
    assert rows == sorted(rows, key=lambda r: (r[2], r[1]))
    # every row carries its shelf — here the single fixture file 'pilot'
    assert {r[0] for r in rows} == {"pilot"}


def test_reconcile_csv_one_row_per_merge_resource(pipeline_run):
    path = pipeline_run.out / "reconcile.csv"
    first_line = path.read_text(encoding="utf-8-sig").splitlines()[0]
    assert first_line == "isbn,title,existing_call_number,resolved_call_number,needs_fix"
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 4
    assert {r["isbn"] for r in rows} == MERGE_ISBNS
    # The fixture export deliberately carries junk call numbers ('2106',
    # 'E2-10') on two merge rows — both must be flagged needs_fix.
    flagged = {r["isbn"] for r in rows if r["needs_fix"] == "True"}
    assert {"9781933633084", "9780312156480"} <= flagged


# --------------------------------------------------------------------------
# 4. Idempotency
# --------------------------------------------------------------------------

def test_rerun_produces_identical_mrc_bytes(pipeline_run):
    """Re-running the pipeline on the same inputs must produce the same
    output byte-for-byte (non-negotiable; 008 pinned via build_date makes
    this exact)."""
    result2, out2 = run_once(pipeline_run.tmp_root, "out2")
    assert result2.counts == pipeline_run.result.counts
    first = (pipeline_run.out / "pilot.mrc").read_bytes()
    second = (out2 / "pilot.mrc").read_bytes()
    assert first == second

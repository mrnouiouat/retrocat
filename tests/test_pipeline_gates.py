"""Tests for pipeline.py's two write-blocking gates.

Both gates enforce the same non-negotiable — "if reconciliation fails, the
pipeline exits non-zero and writes no .mrc" — and cover the stale-file trap:
a blocked run must not leave a shippable-looking .mrc from an earlier clean
run sitting at the path the operator expects.

Hermetic: synthetic export + scan files in tmp_path, stubbed lookup, no HTTP.
"""

from __future__ import annotations

import csv
from datetime import date

import pytest

from retrocat.classify import Action
from retrocat.config import (
    BarcodeConfig,
    CatalogColumns,
    CatalogConfig,
    Config,
    LibraryConfig,
)
from retrocat.lookup import BookMetadata
from retrocat.pipeline import PipelineError, run_pipeline

BUILD_DATE = date(2026, 6, 30)

EXPORT_HEADER = [
    "Resource ID", "Title", "Author", "Publisher", "Published/Issued Date",
    "Type", "Home Library", "Call Number", "Barcode", "ISBN",
]

# Real, checksum-valid ISBNs (the parser validates every one).
ISBN_A = "9781565645998"   # also in the export below -> MERGE_CANDIDATE
ISBN_B = "9780312156480"
ISBN_C = "9781933633084"

# 500100-599999 is the valid new-sticker run in CFG; 500050 falls below it,
# which is how these tests produce a CONFLICT deterministically (classify.py).
GOOD_BARCODE = "500200"
GOOD_BARCODE_2 = "500201"
OUT_OF_RANGE_BARCODE = "500050"

CFG = Config(
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


class StubLookup:
    """Offline LookupClient stand-in: every ISBN resolves to a titled book."""

    def __init__(self, language: str | None = None) -> None:
        self.language = language

    def lookup(self, isbn13: str) -> BookMetadata:
        return BookMetadata(
            isbn13=isbn13,
            title=f"Book {isbn13[-4:]}",
            author="Some Author",
            call_number="BP100 .S66 2020",
            call_number_source="openlibrary",
            confidence="high",
            language=self.language,
        )

    def flush_cache(self) -> None:
        pass


def write_export(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(EXPORT_HEADER)
        for row in rows:
            writer.writerow(row)


def make_export(tmp_path):
    """One Book row carrying ISBN_A under an existing barcode, so a fresh scan
    of ISBN_A on a new barcode is a MERGE_CANDIDATE."""
    path = tmp_path / "export.csv"
    write_export(path, [[
        "1", "Existing Title", "Existing Author", "Pub", "2001",
        "Book", "Some Library", "BP1 .E1 2001", "500010", ISBN_A,
    ]])
    return path


def make_scan(tmp_path, pairs, name="shelf-t1.txt"):
    path = tmp_path / name
    lines = []
    for isbn, barcode in pairs:
        if isbn:
            lines.append(isbn)
        lines.append(barcode)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(tmp_path, scan, export, **kwargs):
    return run_pipeline(
        scans_dir=scan,
        export_path=export,
        out_dir=tmp_path / "out",
        config=kwargs.pop("config", CFG),
        lookup_client=kwargs.pop("lookup_client", StubLookup()),
        build_date=BUILD_DATE,
        mrc_name="test.mrc",
        manual_worklist_path=tmp_path / "manual" / "shelf-t1.csv",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Conflict gate
# ---------------------------------------------------------------------------

class TestConflictGate:
    def test_conflict_blocks_marc_write_by_default(self, tmp_path):
        scan = make_scan(tmp_path, [
            (ISBN_B, GOOD_BARCODE),
            (ISBN_C, OUT_OF_RANGE_BARCODE),  # outside valid range -> CONFLICT
        ])
        with pytest.raises(PipelineError, match="conflict gate FAILED"):
            run(tmp_path, scan, make_export(tmp_path))
        assert not (tmp_path / "out" / "test.mrc").exists()

    def test_blocked_run_still_writes_the_reports(self, tmp_path):
        # The operator needs the master table to see WHY it blocked — the gate
        # deliberately runs after the reports are written.
        scan = make_scan(tmp_path, [
            (ISBN_B, GOOD_BARCODE),
            (ISBN_C, OUT_OF_RANGE_BARCODE),
        ])
        with pytest.raises(PipelineError):
            run(tmp_path, scan, make_export(tmp_path))
        out = tmp_path / "out"
        assert (out / "master_table.csv").exists()
        assert (out / "reconcile.csv").exists()
        rows = list(csv.DictReader(
            (out / "master_table.csv").read_text(encoding="utf-8-sig").splitlines()
        ))
        conflicted = [r for r in rows if r["action"] == Action.CONFLICT.value]
        assert [r["barcode"] for r in conflicted] == [OUT_OF_RANGE_BARCODE]

    def test_conflict_note_carries_configured_ranges(self, tmp_path):
        # The range text in the note comes from THIS config, not a hardcoded
        # institutional range.
        scan = make_scan(tmp_path, [
            (ISBN_B, GOOD_BARCODE),
            (ISBN_C, OUT_OF_RANGE_BARCODE),
        ])
        with pytest.raises(PipelineError, match="500100"):
            run(tmp_path, scan, make_export(tmp_path))

    def test_allow_conflicts_ships_the_file(self, tmp_path):
        scan = make_scan(tmp_path, [
            (ISBN_B, GOOD_BARCODE),
            (ISBN_C, OUT_OF_RANGE_BARCODE),
        ])
        result = run(
            tmp_path, scan, make_export(tmp_path), allow_conflicts=True
        )
        assert (tmp_path / "out" / "test.mrc").exists()
        assert result.counts["CONFLICT"] == 1
        # The conflicted book is excluded from the MARC either way — only the
        # clean book is written.
        assert result.marc_records == 1

    def test_no_conflicts_writes_normally(self, tmp_path):
        scan = make_scan(tmp_path, [
            (ISBN_B, GOOD_BARCODE),
            (ISBN_C, GOOD_BARCODE_2),
        ])
        result = run(tmp_path, scan, make_export(tmp_path))
        assert (tmp_path / "out" / "test.mrc").exists()
        assert result.counts["CONFLICT"] == 0
        assert result.marc_records == 2

    def test_stale_marc_from_earlier_clean_run_is_removed(self, tmp_path):
        """The dangerous case: a good run, then a bad scan. Without removal the
        operator sees an error but finds yesterday's file exactly where the
        good one goes."""
        export = make_export(tmp_path)
        clean = make_scan(tmp_path, [(ISBN_B, GOOD_BARCODE)])
        run(tmp_path, clean, export)
        mrc = tmp_path / "out" / "test.mrc"
        assert mrc.exists()

        conflicted = make_scan(
            tmp_path,
            [(ISBN_B, GOOD_BARCODE), (ISBN_C, OUT_OF_RANGE_BARCODE)],
            name="shelf-t2.txt",
        )
        with pytest.raises(PipelineError):
            run(tmp_path, conflicted, export)
        assert not mrc.exists()

    def test_hand_entered_worklist_is_never_touched_by_a_blocked_run(self, tmp_path):
        # _discard_stale_marc only removes the .mrc under output/; manual/ holds
        # irreplaceable typed-in data and must survive.
        scan = make_scan(tmp_path, [
            (ISBN_B, GOOD_BARCODE),
            (ISBN_C, OUT_OF_RANGE_BARCODE),
        ])
        worklist = tmp_path / "manual" / "shelf-t1.csv"
        with pytest.raises(PipelineError):
            run(tmp_path, scan, make_export(tmp_path))
        assert worklist.exists()


# ---------------------------------------------------------------------------
# Reconciliation gate
# ---------------------------------------------------------------------------

class TestReconciliationGate:
    def test_gate_failure_raises_and_writes_no_marc(self, tmp_path, monkeypatch):
        # Force the invariant to break: drop a book from the classification so
        # bucketed != scanned. There is no input that does this legitimately —
        # that is the point of the gate, it catches a bug in our own code.
        import retrocat.pipeline as pipeline_mod

        real_classify = pipeline_mod.classify_books

        def lossy_classify(scanned, catalog, barcodes):
            result = real_classify(scanned, catalog, barcodes)
            result.books.pop()
            return result

        monkeypatch.setattr(pipeline_mod, "classify_books", lossy_classify)
        scan = make_scan(tmp_path, [
            (ISBN_B, GOOD_BARCODE),
            (ISBN_C, GOOD_BARCODE_2),
        ])
        with pytest.raises(PipelineError, match="reconciliation gate FAILED"):
            run(tmp_path, scan, make_export(tmp_path))
        assert not (tmp_path / "out" / "test.mrc").exists()

    def test_gate_failure_removes_a_stale_marc(self, tmp_path, monkeypatch):
        import retrocat.pipeline as pipeline_mod

        export = make_export(tmp_path)
        scan = make_scan(tmp_path, [
            (ISBN_B, GOOD_BARCODE),
            (ISBN_C, GOOD_BARCODE_2),
        ])
        run(tmp_path, scan, export)
        mrc = tmp_path / "out" / "test.mrc"
        assert mrc.exists()

        real_classify = pipeline_mod.classify_books

        def lossy_classify(scanned, catalog, barcodes):
            result = real_classify(scanned, catalog, barcodes)
            result.books.pop()
            return result

        monkeypatch.setattr(pipeline_mod, "classify_books", lossy_classify)
        with pytest.raises(PipelineError, match="reconciliation gate FAILED"):
            run(tmp_path, scan, export)
        assert not mrc.exists()


# ---------------------------------------------------------------------------
# 008 language end-to-end
# ---------------------------------------------------------------------------

class TestLanguageReachesTheMarcFile:
    def _read_008s(self, mrc_path):
        from pymarc import MARCReader

        with open(mrc_path, "rb") as f:
            return [r["008"].data for r in MARCReader(f, to_unicode=True)]

    def test_resolved_arabic_lands_in_008(self, tmp_path):
        scan = make_scan(tmp_path, [(ISBN_B, GOOD_BARCODE)])
        run(
            tmp_path, scan, make_export(tmp_path),
            lookup_client=StubLookup(language="ara"),
        )
        (data,) = self._read_008s(tmp_path / "out" / "test.mrc")
        assert data[35:38] == "ara"

    def test_unresolved_language_falls_back_to_config_default(self, tmp_path):
        scan = make_scan(tmp_path, [(ISBN_B, GOOD_BARCODE)])
        run(tmp_path, scan, make_export(tmp_path), lookup_client=StubLookup())
        (data,) = self._read_008s(tmp_path / "out" / "test.mrc")
        assert data[35:38] == "eng"
        assert len(data) == 40

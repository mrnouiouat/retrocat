"""Manual worklist round-trip: I/O, merge-preservation, and record building.

The end-to-end ingestion path (a filled worklist -> MARC records through the
whole pipeline) lives in test_pipeline_gates.py / the hermetic integration
test, which need pipeline.py.
"""

from __future__ import annotations

from retrocat.manual import (
    ManualEntry,
    build_manual_groups,
    manual_metadata,
    read_worklist,
    write_worklist,
)

DEFAULT_CLASS = "AC"  # mirrors the shipped class map's default_class


# --------------------------------------------------------------------------
# Worklist I/O + merge-preservation
# --------------------------------------------------------------------------

def test_write_then_read_roundtrip(tmp_path):
    path = tmp_path / "shelf-1a.csv"
    write_worklist(
        [
            ManualEntry("shelf-1a", "500300", isbn="9781565645998",
                        notes="no title resolved"),
            ManualEntry("shelf-1a", "500301", notes="no ISBN scanned"),
        ],
        path,
    )
    back = read_worklist(path)
    assert [e.barcode for e in back] == ["500300", "500301"]  # sorted by barcode
    assert back[0].isbn == "9781565645998"
    assert all(not e.filled for e in back)  # no titles yet


def test_missing_worklist_reads_empty(tmp_path):
    assert read_worklist(tmp_path / "nope.csv") == []


def test_rewrite_preserves_operator_entered_values(tmp_path):
    """The whole point of manual/ living outside output/: re-running a shelf
    must NOT clobber a title the operator already typed in."""
    path = tmp_path / "shelf-1a.csv"
    write_worklist([ManualEntry("shelf-1a", "500300", notes="hint")], path)

    # Operator opens the file and fills in title/author.
    rows = read_worklist(path)
    rows[0].title = "The Sealed Nectar"
    rows[0].author = "Al-Mubarakpuri"
    write_worklist(rows, path)

    # A later re-scan of the shelf regenerates the SAME barcode with blank
    # operator columns, the merge must keep the human's values.
    write_worklist([ManualEntry("shelf-1a", "500300", notes="hint")], path)
    after = read_worklist(path)
    assert after[0].title == "The Sealed Nectar"
    assert after[0].author == "Al-Mubarakpuri"
    assert after[0].filled


def test_rewrite_drops_barcodes_no_longer_manual(tmp_path):
    """A barcode that stops being manual (e.g. a fixed scan) is not re-emitted."""
    path = tmp_path / "shelf-1a.csv"
    write_worklist(
        [ManualEntry("shelf-1a", "500300"), ManualEntry("shelf-1a", "500301")],
        path,
    )
    write_worklist([ManualEntry("shelf-1a", "500301")], path)
    after = read_worklist(path)
    assert [e.barcode for e in after] == ["500301"]


# --------------------------------------------------------------------------
# manual_metadata: call-number generation
# --------------------------------------------------------------------------

def test_operator_call_number_used_verbatim():
    meta = manual_metadata(
        ManualEntry("s", "500300", isbn="9781565645998",
                    title="X", author="Garry Wills",
                    call_number="BP130 .W55 2017"),
        DEFAULT_CLASS,
    )
    assert meta.call_number == "BP130 .W55 2017"
    assert meta.call_number_source == "manual"
    assert meta.confidence == "low"
    assert meta.isbn13 == "9781565645998"  # canonicalized/carried through


def test_blank_call_number_is_generated_locally():
    # No call number given -> default class + Cutter from the surname.
    meta = manual_metadata(
        ManualEntry("s", "500301", title="Whatever", author="Garry Wills"),
        DEFAULT_CLASS,
    )
    assert meta.call_number.startswith("AC")
    assert ".W55" in meta.call_number  # cutter('Wills') == 'W55'
    assert meta.call_number_source == "manual"


def test_blank_call_number_picks_up_year_from_notes():
    meta = manual_metadata(
        ManualEntry("s", "500301", title="Whatever", author="Wills",
                    notes="printed 2017, no ISBN"),
        DEFAULT_CLASS,
    )
    assert meta.call_number.endswith("2017")


# --------------------------------------------------------------------------
# build_manual_groups: grouping + skipping unfilled
# --------------------------------------------------------------------------

def test_unfilled_entries_are_skipped():
    groups = build_manual_groups([ManualEntry("s", "500300")], DEFAULT_CLASS)
    assert groups == []


def test_no_isbn_book_is_its_own_single_copy_resource():
    groups = build_manual_groups(
        [ManualEntry("s", "500301", title="Lone")], DEFAULT_CLASS
    )
    assert len(groups) == 1
    assert groups[0].canonical_isbn == ""
    assert groups[0].barcodes == ["500301"]


def test_same_isbn_groups_into_one_resource_two_copies():
    isbn = "9781565645998"
    groups = build_manual_groups([
        ManualEntry("s", "500300", isbn=isbn, title="Dup", author="A"),
        ManualEntry("s", "500301", isbn=isbn, title="Dup", author="A"),
    ], DEFAULT_CLASS)
    assert len(groups) == 1
    assert groups[0].barcodes == ["500300", "500301"]


# --------------------------------------------------------------------------
# Operator-supplied language (MARC 008/35-37)
# --------------------------------------------------------------------------
# Books reaching the worklist skew non-English, they are exactly the ones the
# ISBN APIs do not index, so the operator can name the language at the shelf.

def test_worklist_round_trips_the_language_column(tmp_path):
    path = tmp_path / "shelf-x.csv"
    write_worklist([
        ManualEntry(shelf="shelf-x", barcode="500300", title="Kitab",
                    language="Arabic"),
    ], path)
    (entry,) = read_worklist(path)
    assert entry.language == "Arabic"


def test_operator_language_reaches_metadata_as_a_marc_code():
    entry = ManualEntry(
        shelf="shelf-x", barcode="500300", title="Kitab al-Tawhid",
        author="Ibn Abd al-Wahhab", language="Arabic",
    )
    assert manual_metadata(entry, DEFAULT_CLASS).language == "ara"


def test_blank_language_leaves_it_unset_for_the_default():
    entry = ManualEntry(shelf="shelf-x", barcode="500300", title="A Book")
    assert manual_metadata(entry, DEFAULT_CLASS).language is None


def test_language_is_preserved_across_a_rescan(tmp_path):
    # Same merge guarantee as title/author: a re-run must not clobber it.
    path = tmp_path / "shelf-x.csv"
    write_worklist([
        ManualEntry(shelf="shelf-x", barcode="500300", title="Kitab",
                    language="Arabic"),
    ], path)
    write_worklist([ManualEntry(shelf="shelf-x", barcode="500300")], path)
    (entry,) = read_worklist(path)
    assert entry.title == "Kitab"
    assert entry.language == "Arabic"

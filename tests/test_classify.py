"""Tests for classify.py — every bucket.

The catalog fixture deliberately stores its ISBN in the ISBN-10 form ONLY,
because real exports skew ISBN-10 while scanner reads are ISBN-13 EAN codes —
all bucket decisions must go through canonical ISBN-13 comparison.
"""

import pytest

from retrocat.catalog import load_catalog
from retrocat.classify import Action, classify_books
from retrocat.config import BarcodeConfig, CatalogColumns, CatalogConfig
from retrocat.parse_scans import LoneBarcode, ScanPair

# Catalog contents (synthetic export):
#   row 1: ISBN-10 form only ("1565645995" == ISBN-13 9781565645998),
#          barcode 500001
#   row 2: no ISBN at all, barcode 500002
KNOWN_ISBN10 = "1565645995"
KNOWN_ISBN13 = "9781565645998"       # canonical form of KNOWN_ISBN10
UNKNOWN_ISBN13 = "9781565646988"     # not in the catalog

CSV_TEXT = (
    '"Resource ID",Title,Author,Publisher,"Published/Issued Date",'
    'Type,"Home Library","Call Number",Barcode,ISBN\n'
    '1,Known Book,Author A,Pub,2000,Book,Anytown,"BP 130",500001,1565645995\n'
    '2,No ISBN Book,Author B,Pub,2001,Book,Anytown,"BP 131",500002,\n'
)

CATALOG_CFG = CatalogConfig(columns=CatalogColumns(
    isbn="ISBN", barcode="Barcode", title="Title", author="Author",
    call_number="Call Number", resource_id="Resource ID", type="Type",
))

# New stickers legitimately run 500100-599999; existing catalog barcodes
# (5000xx) sit below the free run, as in a real deployment.
BARCODES = BarcodeConfig(
    length=6, min=500000, max=599999,
    valid_new_ranges=((500100, 599999),),
)


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    path = tmp_path_factory.mktemp("catalog") / "export.csv"
    path.write_text(CSV_TEXT, encoding="utf-8")
    return load_catalog(path, CATALOG_CFG)


def pair(isbn, barcode, line=1):
    return ScanPair(isbn=isbn, barcode=barcode, source_file="scan.txt",
                    line=line)


def lone(barcode, line=1):
    return LoneBarcode(barcode=barcode, source_file="scan.txt", line=line)


def classify_one(book, catalog, barcodes=BARCODES):
    (classified,) = classify_books([book], catalog, barcodes).books
    return classified


class TestCreate:
    def test_new_barcode_unknown_isbn(self, catalog):
        c = classify_one(pair(UNKNOWN_ISBN13, "500100"), catalog)
        assert c.action == Action.CREATE
        assert c.canonical_isbn == UNKNOWN_ISBN13
        assert c.barcode == "500100"


class TestMergeCandidate:
    def test_new_barcode_known_isbn_same_form(self, catalog):
        c = classify_one(pair(KNOWN_ISBN10, "500150"), catalog)
        assert c.action == Action.MERGE_CANDIDATE

    def test_canonicalization_isbn13_scan_matches_isbn10_catalog(self, catalog):
        # The silent-duplication bug fix: the catalog CSV stores ONLY the
        # ISBN-10 form (1565645995), the scanner reads the ISBN-13 EAN
        # (9781565645998). Without canonical ISBN-13 comparison this book
        # would be misclassified as CREATE and silently duplicate the
        # existing resource. It must be MERGE_CANDIDATE.
        c = classify_one(pair(KNOWN_ISBN13, "500150"), catalog)
        assert c.action == Action.MERGE_CANDIDATE
        assert c.canonical_isbn == KNOWN_ISBN13


class TestAlreadyDone:
    def test_barcode_on_record_isbn_agrees_canonically(self, catalog):
        # Catalog stores the ISBN-10 form; the scan is the ISBN-13 of the
        # same book — canonically they agree, so this is ALREADY_DONE.
        c = classify_one(pair(KNOWN_ISBN13, "500001"), catalog)
        assert c.action == Action.ALREADY_DONE
        assert "agrees" in c.note

    def test_barcode_on_record_scanned_as_isbn10(self, catalog):
        c = classify_one(pair(KNOWN_ISBN10, "500001"), catalog)
        assert c.action == Action.ALREADY_DONE

    def test_barcode_on_record_but_record_has_no_isbn(self, catalog):
        c = classify_one(pair(UNKNOWN_ISBN13, "500002"), catalog)
        assert c.action == Action.ALREADY_DONE
        assert "no ISBN" in c.note


class TestConflict:
    def test_barcode_on_record_isbn_disagrees(self, catalog):
        c = classify_one(pair(UNKNOWN_ISBN13, "500001"), catalog)
        assert c.action == Action.CONFLICT
        # Both the on-record ISBN and the scanned ISBN must be in the note.
        assert KNOWN_ISBN13 in c.note
        assert UNKNOWN_ISBN13 in c.note

    def test_new_barcode_outside_valid_range(self, catalog):
        # 500050 is below the free run (500100+) and not in the catalog: a
        # fresh sticker cannot legitimately carry it.
        c = classify_one(pair(UNKNOWN_ISBN13, "500050"), catalog)
        assert c.action == Action.CONFLICT
        assert "500050" in c.note

    def test_range_note_built_from_config_not_hardcoded(self, catalog):
        # The message must describe THIS config's ranges.
        c = classify_one(pair(UNKNOWN_ISBN13, "500050"), catalog)
        assert "500100+" in c.note

    def test_lone_new_barcode_outside_valid_range(self, catalog):
        c = classify_one(lone("500050"), catalog)
        assert c.action == Action.CONFLICT


class TestRangeCheckDisabled:
    def test_no_ranges_means_no_range_conflicts(self, catalog):
        # A library with no sticker-number scheme: empty valid_new_ranges
        # disables the check entirely; nothing about the barcode number can
        # produce a CONFLICT (existing-barcode ISBN disagreement still can).
        no_ranges = BarcodeConfig(length=6, min=500000, max=599999)
        c = classify_one(pair(UNKNOWN_ISBN13, "500050"), catalog,
                         barcodes=no_ranges)
        assert c.action == Action.CREATE
        c = classify_one(lone("500050"), catalog, barcodes=no_ranges)
        assert c.action == Action.MANUAL


class TestLoneBarcodes:
    def test_lone_barcode_not_in_catalog_is_manual(self, catalog):
        c = classify_one(lone("500151"), catalog)
        assert c.action == Action.MANUAL
        assert "no ISBN scanned" in c.note
        assert c.canonical_isbn is None

    def test_lone_barcode_in_catalog_is_already_done(self, catalog):
        c = classify_one(lone("500001"), catalog)
        assert c.action == Action.ALREADY_DONE


class TestInvariants:
    def test_same_isbn_twice_both_classified(self, catalog):
        # Same ISBN scanned twice with two different barcodes: BOTH are
        # classified (both CREATE here); the multi-copy grouping into one
        # MARC resource happens later in marc_build, not in classify.
        books = [
            pair(UNKNOWN_ISBN13, "500152", line=1),
            pair(UNKNOWN_ISBN13, "500153", line=3),
        ]
        result = classify_books(books, catalog, BARCODES)
        assert [c.action for c in result.books] == [Action.CREATE,
                                                    Action.CREATE]
        assert {c.barcode for c in result.books} == {"500152", "500153"}

    def test_every_book_in_exactly_one_bucket(self, catalog):
        books = [
            pair(UNKNOWN_ISBN13, "500100"),   # CREATE
            pair(KNOWN_ISBN13, "500150"),     # MERGE_CANDIDATE
            pair(KNOWN_ISBN13, "500001"),     # ALREADY_DONE
            pair(UNKNOWN_ISBN13, "500001"),   # CONFLICT
            lone("500151"),                   # MANUAL
        ]
        result = classify_books(books, catalog, BARCODES)
        counts = result.counts()
        assert counts == {
            "CREATE": 1,
            "MERGE_CANDIDATE": 1,
            "ALREADY_DONE": 1,
            "CONFLICT": 1,
            "MANUAL": 1,
        }
        # Reconciliation invariant: buckets partition the scanned books.
        assert sum(counts.values()) == len(books) == len(result.books)
        for action in Action:
            for c in result.bucket(action):
                assert c.action == action

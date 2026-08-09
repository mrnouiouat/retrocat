"""Tests for marc_build.py — sandbox-validated field mapping + extensions.

Every structural claim here mirrors docs/DESIGN.md "MARC generation": field
set, indicators, subfield order, multi-copy grouping, dual 020s, round-trip
validation, and the never-fabricate-a-title rule.
"""

from __future__ import annotations

import io
from datetime import date

import pytest
from pymarc import Indicators, MARCReader

from retrocat.classify import Action, ClassifiedBook
from retrocat.config import LibraryConfig
from retrocat.lookup import BookMetadata
from retrocat.marc_build import (
    MarcValidationError,
    ResourceGroup,
    build_marc_file,
    build_record,
    group_books,
)
from retrocat.parse_scans import ScanPair

BUILD_DATE = date(2026, 6, 30)  # pinned: 008 must be deterministic
EXPECTED_008 = "260630s        xx            000 0 eng d"

LIB = LibraryConfig(
    home_library="Anytown College Library",
    location="Main Campus",
    status="Available",
    marc_language="eng",
)

ISBN = "9780199836741"
ISBN_MERGE = "9781565645998"
ISBN_MERGE_10 = "1565645995"


def make_meta(**overrides) -> BookMetadata:
    base = dict(
        isbn13=ISBN,
        title="Reading the Qur'an",
        subtitle="The Contemporary Relevance",
        author="Ziauddin Sardar",
        call_number="BP130.4 .S376 2011",
        call_number_source="openlibrary",
        confidence="high",
        lccn="2011012345",
    )
    base.update(overrides)
    return BookMetadata(**base)


def make_group(meta=None, barcodes=("500156",), extra=()) -> ResourceGroup:
    meta = meta or make_meta()
    return ResourceGroup(
        canonical_isbn=meta.isbn13,
        metadata=meta,
        barcodes=list(barcodes),
        extra_isbn_forms=list(extra),
    )


def subfield_pairs(field):
    return [(s.code, s.value) for s in field.subfields]


def roundtrip(record):
    reader = MARCReader(io.BytesIO(record.as_marc()), to_unicode=True)
    back = next(reader)
    assert back is not None
    return back


# --------------------------------------------------------------------------
# 1. Full-field check + round-trip
# --------------------------------------------------------------------------

def test_build_record_full_fields_and_roundtrip():
    record = build_record(make_group(), LIB, build_date=BUILD_DATE)

    assert [f.tag for f in record.fields] == [
        "008", "010", "020", "100", "245", "050", "852", "876",
    ]

    f008 = record["008"].data
    assert len(f008) == 40
    assert f008 == EXPECTED_008

    assert record["010"]["a"] == "2011012345"
    assert record["020"]["a"] == ISBN

    f100 = record["100"]
    assert f100.indicators == Indicators("1", " ")
    assert f100["a"] == "Ziauddin Sardar"

    f245 = record["245"]
    assert f245.indicators == Indicators("0", "0")
    # Subtitle is appended into $a with ': ', per the validated mapping.
    assert f245["a"] == "Reading the Qur'an: The Contemporary Relevance"

    f050 = record["050"]
    assert f050.indicators == Indicators(" ", "4")
    assert f050["a"] == "BP130.4 .S376 2011"

    assert subfield_pairs(record["852"]) == [
        ("b", LIB.home_library),
        ("c", LIB.location),
        ("h", "BP130.4 .S376 2011"),
        ("p", "500156"),
    ]
    assert subfield_pairs(record["876"]) == [
        ("p", "500156"),
        ("h", "BP130.4 .S376 2011"),
        ("j", LIB.status),
    ]

    # Round-trip through MARCReader: every field/subfield must survive, and
    # pymarc recomputes leader length/offsets on write.
    back = roundtrip(record)
    assert [str(f) for f in back.fields] == [str(f) for f in record.fields]
    ldr = str(back.leader)
    assert ldr[5:8] == "nam"
    assert ldr[8:12] == " a22"
    assert ldr[0:5].isdigit() and int(ldr[0:5]) > 0  # recomputed length
    assert ldr[12:17].isdigit()                      # recomputed base address
    assert ldr[17:] == " a 4500"


# --------------------------------------------------------------------------
# 2. Corporate author -> 110
# --------------------------------------------------------------------------

def test_corporate_author_uses_110_indicator_2():
    meta = make_meta(author="Center for Constitutional Rights (New York, N.Y.)")
    record = build_record(make_group(meta), LIB, build_date=BUILD_DATE)
    assert record.get_fields("100") == []
    f110 = record["110"]
    assert f110.indicators == Indicators("2", " ")
    assert f110["a"] == "Center for Constitutional Rights (New York, N.Y.)"


# --------------------------------------------------------------------------
# 3. No call number -> no 050, no $h
# --------------------------------------------------------------------------

def test_no_call_number_omits_050_and_h_subfields():
    meta = make_meta(call_number=None, call_number_source=None,
                     confidence=None, lccn=None)
    record = build_record(make_group(meta), LIB, build_date=BUILD_DATE)
    assert record.get_fields("050") == []
    assert record.get_fields("010") == []
    assert [s.code for s in record["852"].subfields] == ["b", "c", "p"]
    assert [s.code for s in record["876"].subfields] == ["p", "j"]


# --------------------------------------------------------------------------
# 4. Blank location/status config -> those subfields are omitted entirely
#    (the ILS then applies its own import defaults)
# --------------------------------------------------------------------------

def test_blank_location_and_status_omit_subfields():
    lib = LibraryConfig(home_library="Anytown College Library")
    record = build_record(make_group(), lib, build_date=BUILD_DATE)
    assert [s.code for s in record["852"].subfields] == ["b", "h", "p"]
    assert [s.code for s in record["876"].subfields] == ["p", "h"]


# --------------------------------------------------------------------------
# 5. Multi-copy grouping: one resource, two 852/876 pairs
# --------------------------------------------------------------------------

def test_same_isbn_two_barcodes_groups_to_one_resource():
    """Same ISBN scanned with two different barcodes -> ONE resource record
    with two 852/876 copy pairs, not two resources (confirmed with the ILS
    vendor as the cleaner path vs. their merge tool for same-file
    duplicates)."""
    meta = make_meta()
    books = [
        ClassifiedBook(
            book=ScanPair(isbn=ISBN, barcode="500156", source_file="s.txt", line=1),
            action=Action.CREATE, canonical_isbn=ISBN,
        ),
        ClassifiedBook(
            book=ScanPair(isbn=ISBN, barcode="500199", source_file="s.txt", line=5),
            action=Action.CREATE, canonical_isbn=ISBN,
        ),
    ]
    groups = group_books(books, {ISBN: meta})
    assert len(groups) == 1
    assert groups[0].barcodes == ["500156", "500199"]

    record = build_record(groups[0], LIB, build_date=BUILD_DATE)
    f852 = record.get_fields("852")
    f876 = record.get_fields("876")
    assert len(f852) == 2 and len(f876) == 2
    assert [f["p"] for f in f852] == ["500156", "500199"]
    assert [f["p"] for f in f876] == ["500156", "500199"]
    # Copy pairs are emitted interleaved: 852,876,852,876.
    assert [f.tag for f in record.fields][-4:] == ["852", "876", "852", "876"]


def test_group_books_skips_non_marc_buckets():
    books = [
        ClassifiedBook(
            book=ScanPair(isbn=ISBN, barcode="500156", source_file="s.txt", line=1),
            action=Action.ALREADY_DONE, canonical_isbn=ISBN,
        ),
    ]
    assert group_books(books, {ISBN: make_meta()}) == []


# --------------------------------------------------------------------------
# 6. Dual 020 for merge candidates
# --------------------------------------------------------------------------

def test_dual_020_extra_stored_forms_canonical_first():
    group = make_group(
        meta=make_meta(isbn13=ISBN_MERGE),
        barcodes=("500149",),
        extra=(ISBN_MERGE_10,),
    )
    record = build_record(group, LIB, build_date=BUILD_DATE)
    f020 = record.get_fields("020")
    assert [f["a"] for f in f020] == [ISBN_MERGE, ISBN_MERGE_10]


def test_group_books_attaches_stored_forms_only_for_merge_candidates():
    def book(action):
        return ClassifiedBook(
            book=ScanPair(isbn=ISBN_MERGE, barcode="500149",
                          source_file="s.txt", line=1),
            action=action, canonical_isbn=ISBN_MERGE,
        )

    stored = {ISBN_MERGE: {ISBN_MERGE, ISBN_MERGE_10}}
    merge_groups = group_books(
        [book(Action.MERGE_CANDIDATE)], {ISBN_MERGE: make_meta(isbn13=ISBN_MERGE)},
        stored,
    )
    assert merge_groups[0].extra_isbn_forms == [ISBN_MERGE_10]

    create_groups = group_books(
        [book(Action.CREATE)], {ISBN_MERGE: make_meta(isbn13=ISBN_MERGE)}, stored,
    )
    assert create_groups[0].extra_isbn_forms == []


# --------------------------------------------------------------------------
# 7. Untitled metadata is unbuildable
# --------------------------------------------------------------------------

def test_untitled_metadata_raises_never_fabricates():
    meta = make_meta(title=None, subtitle=None)
    with pytest.raises(MarcValidationError):
        build_record(make_group(meta), LIB, build_date=BUILD_DATE)


# --------------------------------------------------------------------------
# 8. build_marc_file writes a parseable .mrc
# --------------------------------------------------------------------------

def test_build_marc_file_roundtrips_from_disk(tmp_path):
    groups = [
        make_group(),
        make_group(meta=make_meta(isbn13=ISBN_MERGE, title="Second Book",
                                  subtitle=None, lccn=None),
                   barcodes=("500149",)),
    ]
    out = tmp_path / "out.mrc"
    records = build_marc_file(groups, out, LIB, build_date=BUILD_DATE)
    assert len(records) == 2

    reread = list(MARCReader(io.BytesIO(out.read_bytes()), to_unicode=True))
    assert len(reread) == 2
    for orig, back in zip(records, reread):
        assert back is not None
        assert [str(f) for f in back.fields] == [str(f) for f in orig.fields]


# --------------------------------------------------------------------------
# 008 language (MARC 008/35-37)
# --------------------------------------------------------------------------

def test_008_carries_resolved_language():
    from retrocat.marc_build import _field_008

    data = _field_008(BUILD_DATE, "ara", "eng")
    assert len(data) == 40
    assert data[35:38] == "ara"
    # Only the language positions move; the rest of the template is the
    # validated one.
    assert data[:35] == EXPECTED_008[:35]
    assert data[38:] == EXPECTED_008[38:]


def test_008_falls_back_to_configured_default():
    from retrocat.marc_build import _field_008

    assert _field_008(BUILD_DATE, None, "eng") == EXPECTED_008
    # The default comes from config, not a hardcoded constant — a library
    # whose collection default is Arabic stamps 'ara' on unresolved books.
    assert _field_008(BUILD_DATE, None, "ara")[35:38] == "ara"


def test_record_default_language_comes_from_library_config():
    lib = LibraryConfig(home_library="X", marc_language="ara")
    record = build_record(make_group(make_meta(language=None)), lib,
                          build_date=BUILD_DATE)
    assert record["008"].data[35:38] == "ara"


def test_008_rejects_a_non_three_character_language():
    # A malformed code would silently shift every position after 35 and corrupt
    # the fixed-length field — fail loud instead.
    from retrocat.marc_build import MarcValidationError, _field_008

    with pytest.raises(MarcValidationError, match="3-character MARC code"):
        _field_008(BUILD_DATE, "arabic", "eng")

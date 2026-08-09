"""Tests for isbn.py conversions and checksum validators.

The two conversion pairs in TestConversion are regression anchors verified
against a real catalog export during the original internal deployment, they
pin the 10->13 math to known-true values, not just to itself.
"""

import pytest

from retrocat.isbn import (
    canonical_isbn13,
    extract_isbns,
    is_valid_isbn10,
    is_valid_isbn13,
    isbn10_to_13,
    isbn13_to_10,
)

# ---------------------------------------------------------------------------
# ISBN 10 <-> 13 conversion
# ---------------------------------------------------------------------------

class TestConversion:
    def test_confirmed_pair_one(self):
        assert isbn10_to_13("1565645995") == "9781565645998"

    def test_confirmed_pair_two(self):
        assert isbn10_to_13("1565646983") == "9781565646988"

    def test_confirmed_pair_inverses(self):
        assert isbn13_to_10("9781565645998") == "1565645995"
        assert isbn13_to_10("9781565646988") == "1565646983"

    def test_canonical_passes_13_through_unchanged(self):
        assert canonical_isbn13("9781565645998") == "9781565645998"

    def test_canonical_converts_10_form(self):
        assert canonical_isbn13("1565645995") == "9781565645998"

    def test_canonical_normalizes_hyphens(self):
        assert canonical_isbn13("978-1-56564-599-8") == "9781565645998"

    def test_979_isbn13_has_no_10_form(self):
        assert isbn13_to_10("9798985782226") is None

    def test_canonical_rejects_non_isbn_length(self):
        with pytest.raises(ValueError):
            canonical_isbn13("12345")

    def test_misplaced_x_raises_cleanly_not_int_crash(self):
        # Export junk like '05218X9012' (X in a non-final position) used to
        # crash int() deep inside the check-digit math. It must raise a clean
        # ValueError so the catalog loader can log-and-skip it.
        with pytest.raises(ValueError, match="not numeric"):
            isbn10_to_13("05218X9012")

    def test_isbn13_to_10_junk_core_returns_none(self):
        assert isbn13_to_10("978123X567890") is None


# ---------------------------------------------------------------------------
# Checksum validators
# ---------------------------------------------------------------------------

class TestValidators:
    @pytest.mark.parametrize("isbn", ["1565645995", "1565646983",
                                      "097522980X", "086037307X",
                                      "097522980x"])
    def test_valid_isbn10(self, isbn):
        assert is_valid_isbn10(isbn)

    @pytest.mark.parametrize("isbn", [
        "1565645996",   # wrong check digit (correct is 5)
        "0975229809",   # wrong check digit (correct is X)
        "156564599",    # too short
        "15656459955",  # too long
        "X565645995",   # X not allowed in first nine
    ])
    def test_invalid_isbn10(self, isbn):
        assert not is_valid_isbn10(isbn)

    @pytest.mark.parametrize("isbn", ["9781565645998", "9781565646988"])
    def test_valid_isbn13(self, isbn):
        assert is_valid_isbn13(isbn)

    @pytest.mark.parametrize("isbn", [
        "9781565645999",  # wrong check digit (correct is 8)
        "1234567890128",  # EAN-valid but no 978/979 prefix
        "978156564599",   # too short
        "978156564599X",  # X illegal in ISBN-13
    ])
    def test_invalid_isbn13(self, isbn):
        assert not is_valid_isbn13(isbn)


# ---------------------------------------------------------------------------
# Extraction from junky multi-value export fields
# ---------------------------------------------------------------------------

class TestExtractIsbns:
    def test_multi_value_field(self):
        valid, rejected = extract_isbns("1565645995, 9781565645998")
        assert valid == ["1565645995", "9781565645998"]
        assert rejected == []

    def test_parenthetical_qualifier(self):
        valid, rejected = extract_isbns("086037307X (pbk. : v. 5)")
        assert valid == ["086037307X"]
        assert rejected == []

    def test_wrong_length_token_rejected(self):
        valid, rejected = extract_isbns("977-5224-9-8")
        assert valid == []
        assert rejected == ["977-5224-9-8"]

    def test_empty_and_none_safe(self):
        assert extract_isbns("") == ([], [])
        assert extract_isbns(None) == ([], [])

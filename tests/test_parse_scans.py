"""Tests for parse_scans.py, every state-machine transition.

The pairing state machine (docs/DESIGN.md "Scan files") has two deliberately
non-obvious cases that are contractual and must not be "fixed":
  * two barcodes in a row is NOT an error (first closes the open pair,
    second is a lone-barcode / no-ISBN book);
  * two ISBNs in a row IS an error (a barcode was never scanned).
Both are tested explicitly below.
"""

import pytest

from retrocat.config import BarcodeConfig
from retrocat.parse_scans import (
    LoneBarcode,
    ScanPair,
    ScanParseError,
    dedupe_scans,
    parse_scan_dir,
    parse_scan_file,
)

# All checksum-valid ISBNs (see tests/test_isbn.py for the validator tests).
ISBN13_A = "9781565645998"
ISBN13_B = "9781565646988"
ISBN10_A = "1565645995"        # ISBN-10 form of ISBN13_A
ISBN10_X = "097522980X"        # valid ISBN-10 with X check digit
ISBN10_X2 = "086037307X"       # valid ISBN-10 with X check digit
BC1 = "500100"
BC2 = "500101"
BC3 = "500102"

# Generic 6-digit scheme used throughout: bounds 500000-599999.
CFG = BarcodeConfig(length=6, min=500000, max=599999)


def write_scan(tmp_path, lines, name="scan.txt"):
    """Write a scan file from a list of line strings; return its Path."""
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# State machine transitions
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_two_pairs(self, tmp_path):
        # EXPECT_ISBN + ISBN -> EXPECT_BARCODE; EXPECT_BARCODE + barcode -> pair
        p = write_scan(tmp_path, [ISBN13_A, BC1, ISBN13_B, BC2])
        books = parse_scan_file(p, CFG)
        assert books == [
            ScanPair(isbn=ISBN13_A, barcode=BC1, source_file=str(p), line=1),
            ScanPair(isbn=ISBN13_B, barcode=BC2, source_file=str(p), line=3),
        ]

    def test_pair_line_is_isbn_line(self, tmp_path):
        p = write_scan(tmp_path, [ISBN13_A, BC1])
        (pair,) = parse_scan_file(p, CFG)
        assert pair.line == 1
        assert pair.source_file == str(p)


class TestTwoBarcodesInARow:
    def test_not_an_error_second_is_lone(self, tmp_path):
        # docs/DESIGN.md "Pairing validation": two barcodes with no ISBN
        # between them is NOT an error, the first barcode closes the previous
        # pair, the second is a lone-barcode (no-ISBN book). Contractual; do
        # not "fix" this into an error.
        p = write_scan(tmp_path, [ISBN13_A, BC1, BC2])
        books = parse_scan_file(p, CFG)
        assert books == [
            ScanPair(isbn=ISBN13_A, barcode=BC1, source_file=str(p), line=1),
            LoneBarcode(barcode=BC2, source_file=str(p), line=3),
        ]

    def test_parsing_continues_after_lone(self, tmp_path):
        p = write_scan(tmp_path, [ISBN13_A, BC1, BC2, ISBN13_B, BC3])
        books = parse_scan_file(p, CFG)
        assert [type(b) for b in books] == [ScanPair, LoneBarcode, ScanPair]
        assert books[2].isbn == ISBN13_B


class TestTwoIsbnsInARow:
    def test_two_different_isbns_is_error(self, tmp_path):
        # docs/DESIGN.md "Pairing validation": two ISBNs with no barcode
        # between them -> error, book N's barcode was never scanned.
        p = write_scan(tmp_path, [ISBN13_A, ISBN13_B, BC1])
        with pytest.raises(ScanParseError) as exc:
            parse_scan_file(p, CFG)
        msg = str(exc.value)
        assert "two different ISBNs" in msg
        assert "never scanned" in msg
        assert str(p) in msg
        assert ":2:" in msg  # line of the offending second ISBN
        assert ISBN13_A in msg and ISBN13_B in msg

    def test_two_identical_isbns_is_double_scan_error(self, tmp_path):
        # Same ISBN twice in a row gets the distinct double-scan message.
        p = write_scan(tmp_path, [ISBN13_A, ISBN13_A, BC1])
        with pytest.raises(ScanParseError) as exc:
            parse_scan_file(p, CFG)
        msg = str(exc.value)
        assert "same ISBN" in msg
        assert "twice" in msg
        assert str(p) in msg

    def test_trailing_unpaired_isbn_is_error(self, tmp_path):
        # EOF while EXPECT_BARCODE -> hard error (trailing single ISBN at end
        # of file with no barcode).
        p = write_scan(tmp_path, [ISBN13_A, BC1, ISBN13_B])
        with pytest.raises(ScanParseError) as exc:
            parse_scan_file(p, CFG)
        msg = str(exc.value)
        assert "unpaired ISBN" in msg
        assert ISBN13_B in msg
        assert str(p) in msg
        assert ":3:" in msg


class TestLoneBarcodeAtStart:
    def test_leading_barcode_is_lone(self, tmp_path):
        # EXPECT_ISBN + barcode -> LoneBarcode, stay in EXPECT_ISBN.
        p = write_scan(tmp_path, [BC1, ISBN13_A, BC2])
        books = parse_scan_file(p, CFG)
        assert books == [
            LoneBarcode(barcode=BC1, source_file=str(p), line=1),
            ScanPair(isbn=ISBN13_A, barcode=BC2, source_file=str(p), line=2),
        ]


class TestFileTolerance:
    def test_bom_crlf_and_blank_lines(self, tmp_path):
        p = tmp_path / "scan.txt"
        p.write_bytes(
            b"\xef\xbb\xbf" + ISBN13_A.encode() + b"\r\n"
            b"\r\n"
            b"   \r\n"
            + BC1.encode() + b"\r\n"
        )
        books = parse_scan_file(p, CFG)
        assert books == [
            ScanPair(isbn=ISBN13_A, barcode=BC1, source_file=str(p), line=1)
        ]


# ---------------------------------------------------------------------------
# Token classification / checksum validation
# ---------------------------------------------------------------------------

class TestIsbnValidation:
    def test_isbn13_bad_check_digit(self, tmp_path):
        p = write_scan(tmp_path, ["9781565645999", BC1])  # correct digit is 8
        with pytest.raises(ScanParseError) as exc:
            parse_scan_file(p, CFG)
        assert "check digit" in str(exc.value)

    def test_isbn13_without_978_979_prefix(self, tmp_path):
        # 1234567890128 is EAN-checksum-valid but is not a Bookland ISBN.
        p = write_scan(tmp_path, ["1234567890128", BC1])
        with pytest.raises(ScanParseError) as exc:
            parse_scan_file(p, CFG)
        assert "978/979" in str(exc.value)

    def test_isbn10_bad_check_digit(self, tmp_path):
        p = write_scan(tmp_path, ["1565645996", BC1])  # correct digit is 5
        with pytest.raises(ScanParseError) as exc:
            parse_scan_file(p, CFG)
        assert "check digit" in str(exc.value)

    @pytest.mark.parametrize("isbn10", [ISBN10_X, ISBN10_X2])
    def test_valid_isbn10_with_trailing_x_accepted(self, tmp_path, isbn10):
        p = write_scan(tmp_path, [isbn10, BC1])
        (pair,) = parse_scan_file(p, CFG)
        assert pair.isbn == isbn10
        assert pair.barcode == BC1

    def test_lowercase_x_normalized(self, tmp_path):
        p = write_scan(tmp_path, ["097522980x", BC1])
        (pair,) = parse_scan_file(p, CFG)
        assert pair.isbn == "097522980X"


class TestMalformedTokens:
    def test_six_digits_below_configured_min(self, tmp_path):
        p = write_scan(tmp_path, ["499999"])
        with pytest.raises(ScanParseError) as exc:
            parse_scan_file(p, CFG)
        msg = str(exc.value)
        assert "499999" in msg
        assert "minimum" in msg

    def test_six_digits_above_configured_max(self, tmp_path):
        p = write_scan(tmp_path, ["600000"])
        with pytest.raises(ScanParseError) as exc:
            parse_scan_file(p, CFG)
        assert "maximum" in str(exc.value)

    def test_no_bounds_accepts_any_six_digit_token(self, tmp_path):
        # A library with no barcode numbering scheme sets no min/max, any
        # 6-digit line is then a barcode.
        p = write_scan(tmp_path, ["000001"])
        (book,) = parse_scan_file(p, BarcodeConfig(length=6))
        assert book == LoneBarcode(barcode="000001", source_file=str(p), line=1)

    def test_configured_length_changes_barcode_shape(self, tmp_path):
        # An 8-digit library: 8-digit tokens are barcodes, 6-digit are junk.
        cfg8 = BarcodeConfig(length=8)
        p = write_scan(tmp_path, [ISBN13_A, "12345678"])
        (pair,) = parse_scan_file(p, cfg8)
        assert pair.barcode == "12345678"
        with pytest.raises(ScanParseError):
            parse_scan_file(write_scan(tmp_path, ["123456"], name="b.txt"), cfg8)

    def test_eight_digit_junk_is_error_with_location(self, tmp_path):
        p = write_scan(tmp_path, [ISBN13_A, BC1, "12345678"])
        with pytest.raises(ScanParseError) as exc:
            parse_scan_file(p, CFG)
        msg = str(exc.value)
        assert "malformed" in msg
        assert str(p) in msg
        assert ":3:" in msg

    def test_alpha_junk_line_is_error(self, tmp_path):
        # Code classification rule: anything that is not an ISBN or a barcode
        # is a malformed line -> hard error with file+line (fail loud, don't
        # guess). "hello" must not be silently skipped.
        p = write_scan(tmp_path, [ISBN13_A, BC1, "hello"])
        with pytest.raises(ScanParseError):
            parse_scan_file(p, CFG)


# ---------------------------------------------------------------------------
# Cross-file dedupe rules
# ---------------------------------------------------------------------------

class TestDedupeScans:
    def test_same_barcode_same_isbn_deduped(self):
        a = ScanPair(isbn=ISBN13_A, barcode=BC1, source_file="a.txt", line=1)
        b = ScanPair(isbn=ISBN13_A, barcode=BC1, source_file="b.txt", line=5)
        assert dedupe_scans([a, b]) == [a]

    def test_same_barcode_different_isbns_is_error(self):
        a = ScanPair(isbn=ISBN13_A, barcode=BC1, source_file="a.txt", line=1)
        b = ScanPair(isbn=ISBN13_B, barcode=BC1, source_file="b.txt", line=5)
        with pytest.raises(ScanParseError) as exc:
            dedupe_scans([a, b])
        msg = str(exc.value)
        assert BC1 in msg
        assert ISBN13_A in msg and ISBN13_B in msg

    def test_barcode_lone_in_one_file_paired_in_another_is_error(self):
        a = LoneBarcode(barcode=BC1, source_file="a.txt", line=1)
        b = ScanPair(isbn=ISBN13_A, barcode=BC1, source_file="b.txt", line=5)
        with pytest.raises(ScanParseError):
            dedupe_scans([a, b])
        # And the reverse order is the same inconsistency.
        with pytest.raises(ScanParseError):
            dedupe_scans([b, a])

    def test_identical_lone_barcode_twice_deduped(self):
        a = LoneBarcode(barcode=BC1, source_file="a.txt", line=1)
        b = LoneBarcode(barcode=BC1, source_file="b.txt", line=9)
        assert dedupe_scans([a, b]) == [a]


# ---------------------------------------------------------------------------
# Directory-level parsing
# ---------------------------------------------------------------------------

class TestParseScanDir:
    def test_reads_sorted_txt_files(self, tmp_path):
        # b.txt written first, a.txt second, output must be in sorted
        # filename order, not creation order.
        write_scan(tmp_path, [ISBN13_B, BC2], name="b.txt")
        write_scan(tmp_path, [ISBN13_A, BC1], name="a.txt")
        books = parse_scan_dir(tmp_path, CFG)
        assert [b.barcode for b in books] == [BC1, BC2]
        assert books[0].source_file.endswith("a.txt")

    def test_non_txt_files_ignored(self, tmp_path):
        write_scan(tmp_path, [ISBN13_A, BC1], name="a.txt")
        (tmp_path / "notes.md").write_text("not a scan\n", encoding="utf-8")
        books = parse_scan_dir(tmp_path, CFG)
        assert len(books) == 1

    def test_dedupes_across_files(self, tmp_path):
        write_scan(tmp_path, [ISBN13_A, BC1], name="a.txt")
        write_scan(tmp_path, [ISBN13_A, BC1], name="b.txt")
        books = parse_scan_dir(tmp_path, CFG)
        assert len(books) == 1

    def test_no_txt_files_raises(self, tmp_path):
        (tmp_path / "notes.md").write_text("nope\n", encoding="utf-8")
        with pytest.raises(FileNotFoundError):
            parse_scan_dir(tmp_path, CFG)

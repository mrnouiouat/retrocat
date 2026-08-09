"""Tests for config.py, loading, validation, and the barcode-range helpers."""

import pytest

from retrocat.config import (
    BarcodeConfig,
    CatalogColumns,
    CatalogConfig,
    Config,
    ConfigError,
    LibraryConfig,
    load_config,
    validate_config,
)

FULL_TOML = """
[library]
home_library = "Anytown College Library"
location = "Main Campus"
status = "Available"
marc_language = "eng"

[barcodes]
length = 6
min = 500000
max = 599999
valid_new_ranges = [[500100, 500999], [501003, 599999]]

[catalog]
book_type = "Book"

[catalog.columns]
isbn = "ISBN"
barcode = "Barcode"
title = "Title"
author = "Author"
call_number = "Call Number"
resource_id = "Resource ID"
type = "Type"

[output]
mrc_filename = "import.mrc"
"""


def write_config(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


class TestLoadConfig:
    def test_full_config_round_trips(self, tmp_path):
        cfg = load_config(write_config(tmp_path, FULL_TOML))
        assert cfg.library.home_library == "Anytown College Library"
        assert cfg.library.marc_language == "eng"
        assert cfg.barcodes.length == 6
        assert cfg.barcodes.valid_new_ranges == ((500100, 500999),
                                                 (501003, 599999))
        assert cfg.catalog.columns.isbn == "ISBN"
        assert cfg.catalog.columns.type == "Type"
        assert cfg.output.mrc_filename == "import.mrc"

    def test_minimal_config_gets_defaults(self, tmp_path):
        cfg = load_config(write_config(tmp_path, """
[library]
home_library = "Somewhere"
"""))
        assert cfg.barcodes.length == 6
        assert cfg.barcodes.min is None
        assert cfg.barcodes.valid_new_ranges == ()
        assert cfg.catalog.columns.isbn == "ISBN"
        assert cfg.catalog.columns.type == ""  # filter off by default
        assert cfg.output.mrc_filename == "catalog_import.mrc"

    def test_missing_file_is_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nope.toml")

    def test_invalid_toml_is_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="invalid TOML"):
            load_config(write_config(tmp_path, "[library\n"))

    def test_missing_home_library_fails_fast(self, tmp_path):
        # Fail at startup, not by emitting records with a placeholder library.
        with pytest.raises(ConfigError, match="home_library"):
            load_config(write_config(tmp_path, "[library]\nlocation = 'x'\n"))

    def test_unknown_key_is_error_not_silent_default(self, tmp_path):
        # A typo like 'home_libary' must not silently fall back to defaults.
        with pytest.raises(ConfigError, match="home_libary"):
            load_config(write_config(tmp_path, """
[library]
home_libary = "Typo Library"
"""))

    def test_unknown_section_is_error(self, tmp_path):
        with pytest.raises(ConfigError, match="barcode_scheme"):
            load_config(write_config(tmp_path, """
[library]
home_library = "X"
[barcode_scheme]
length = 6
"""))

    def test_barcode_length_colliding_with_isbn_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="collides"):
            load_config(write_config(tmp_path, """
[library]
home_library = "X"
[barcodes]
length = 13
"""))

    def test_bad_range_pair_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="valid_new_ranges"):
            load_config(write_config(tmp_path, """
[library]
home_library = "X"
[barcodes]
valid_new_ranges = [[100]]
"""))

    def test_inverted_range_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="lo 900 > hi 100"):
            load_config(write_config(tmp_path, """
[library]
home_library = "X"
[barcodes]
valid_new_ranges = [[900, 100]]
"""))

    def test_blank_isbn_column_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="isbn"):
            load_config(write_config(tmp_path, """
[library]
home_library = "X"
[catalog.columns]
isbn = ""
"""))

    def test_non_three_char_marc_language_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="marc_language"):
            load_config(write_config(tmp_path, """
[library]
home_library = "X"
marc_language = "en"
"""))

    def test_sample_config_is_valid(self):
        # The shipped sample must always load cleanly, it is the documented
        # starting point for every new library.
        from pathlib import Path
        sample = Path(__file__).resolve().parent.parent / "sample" / "config.toml"
        cfg = load_config(sample)
        assert cfg.library.home_library
        assert validate_config(cfg) == []


class TestBarcodeRangeHelpers:
    # Mirrors a real deployment's scheme: a free run, a gap of two occupied
    # out-of-sequence stickers, then the roll resumes.
    CFG = BarcodeConfig(
        length=6, min=500000, max=599999,
        valid_new_ranges=((500100, 500999), (501003, 599999)),
    )

    @pytest.mark.parametrize("barcode,expected", [
        ("500100", True),   # first free number
        ("500999", True),   # last of the contiguous free run
        ("501000", False),  # occupied out-of-sequence stickers
        ("501002", False),
        ("501003", True),   # sequence resumes after the gap
        ("500099", False),  # last already-used number before the free run
    ])
    def test_is_valid_new_barcode(self, barcode, expected):
        assert self.CFG.is_valid_new_barcode(barcode) is expected

    def test_empty_ranges_disables_check(self):
        cfg = BarcodeConfig(length=6)
        assert cfg.is_valid_new_barcode("000001")
        assert cfg.is_valid_new_barcode("999999")

    def test_describe_ranges_open_ended_tail(self):
        # A range reaching the configured max reads as 'lo+'.
        assert self.CFG.describe_ranges() == "500100-500999 or 501003+"

    def test_describe_ranges_closed_when_no_max(self):
        cfg = BarcodeConfig(length=6, valid_new_ranges=((100, 200),))
        assert cfg.describe_ranges() == "100-200"


class TestValidateConfig:
    def test_valid_default_plus_home_library(self):
        cfg = Config(library=LibraryConfig(home_library="X"))
        assert validate_config(cfg) == []

    def test_blank_barcode_column(self):
        cfg = Config(
            library=LibraryConfig(home_library="X"),
            catalog=CatalogConfig(columns=CatalogColumns(barcode="")),
        )
        problems = validate_config(cfg)
        assert any("barcode" in p for p in problems)

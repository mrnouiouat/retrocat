"""Tests for catalog.py, export loading, junk tolerance, header validation.

Uses small synthetic CSVs written to tmp_path, never a real export (keeps
the suite fast and hermetic).
"""

import pytest

from retrocat.catalog import CatalogError, load_catalog
from retrocat.config import CatalogColumns, CatalogConfig
from retrocat.isbn import canonical_isbn13

HEADER = ('"Resource ID",Title,Author,Publisher,"Published/Issued Date",'
          'Type,"Home Library","Call Number",Barcode,ISBN')

# Full mapping matching HEADER, type filter on.
CFG = CatalogConfig(
    columns=CatalogColumns(
        isbn="ISBN", barcode="Barcode", title="Title", author="Author",
        call_number="Call Number", resource_id="Resource ID", type="Type",
    ),
    book_type="Book",
)


def make_catalog(tmp_path, rows, cfg=CFG):
    """Write a synthetic export CSV and load it."""
    csv_path = tmp_path / "export.csv"
    csv_path.write_text(HEADER + "\n" + "\n".join(rows) + "\n",
                        encoding="utf-8")
    return load_catalog(csv_path, cfg)


class TestLoadCatalog:
    def test_isbn10_row_indexed_under_both_forms(self, tmp_path):
        cat = make_catalog(tmp_path, [
            '1,Some Title,Some Author,Pub,2000,Book,Anytown,"BP 130",500001,1565645995',
        ])
        # Both the normalized original AND the canonical ISBN-13 form must be
        # in existing_isbns; barcode maps to the canonical form.
        assert "1565645995" in cat.existing_isbns
        assert "9781565645998" in cat.existing_isbns
        assert cat.barcode_to_isbns["500001"] == {"9781565645998"}
        assert cat.isbn_known("9781565645998")

    def test_multi_value_isbn_and_barcode_fields(self, tmp_path):
        cat = make_catalog(tmp_path, [
            '2,Two Copies,Author,Pub,2001,Book,Anytown,"BP 1","733, 736",'
            '"1565645995, 9781565645998"',
        ])
        assert cat.barcode_to_isbns["733"] == {"9781565645998"}
        assert cat.barcode_to_isbns["736"] == {"9781565645998"}
        assert {"1565645995", "9781565645998"} <= cat.existing_isbns

    def test_invalid_length_isbn_skipped_row_kept(self, tmp_path):
        # Real junk observed in production exports: an ISSN-ish value.
        # Log-and-skip the value, keep the row, never raise.
        cat = make_catalog(tmp_path, [
            '3,Junk ISBN,Author,Pub,2002,Book,Anytown,"BP 2",500002,977-5224-9-8',
        ])
        assert cat.row_count == 1
        assert cat.skipped_rows == 0
        assert "977522498" not in cat.existing_isbns
        assert cat.existing_isbns == set()
        # Row (and its barcode) survives even though the ISBN value was junk.
        assert "500002" in cat.barcode_to_isbns

    def test_misplaced_x_isbn_value_skipped_loader_survives(self, tmp_path):
        # A 10-char token with 'X' mid-string passes extraction but cannot be
        # canonicalized, the loader must log-and-skip it, never crash, and
        # stored_forms must not see it either.
        cat = make_catalog(tmp_path, [
            '9,X Junk,Author,Pub,2002,Book,Anytown,"BP 9",500009,'
            '"05218X9012; 1565645995"',
        ])
        assert cat.row_count == 1
        assert "9781565645998" in cat.existing_isbns  # good value survives
        assert "05218X9012" not in cat.existing_isbns
        assert cat.stored_forms("9781565645998") == {"1565645995"}

    def test_call_number_shaped_barcode_skipped_row_kept(self, tmp_path):
        cat = make_catalog(tmp_path, [
            '4,Junk Barcode,Author,Pub,2003,Book,Anytown,"BP 3",'
            '"BP 130.45 .B57 2014",1565646983',
        ])
        assert "BP 130.45 .B57 2014" not in cat.barcode_to_isbns
        assert cat.barcode_to_isbns == {}
        # Row kept: its ISBN is still indexed.
        assert "9781565646988" in cat.existing_isbns

    def test_non_book_types_skipped_entirely(self, tmp_path):
        cat = make_catalog(tmp_path, [
            '5,A Collection,Author,Pub,2004,Collection,Anytown,"BP 4",500003,1565645995',
            '6,A Movie,Author,Pub,2005,Movie,Anytown,"BP 5",500004,1565646983',
            '7,A Book,Author,Pub,2006,Book,Anytown,"BP 6",500005,097522980X',
        ])
        assert cat.skipped_rows == 2
        assert "1565645995" not in cat.existing_isbns
        assert "500003" not in cat.barcode_to_isbns
        assert "500004" not in cat.barcode_to_isbns
        assert "097522980X" in cat.existing_isbns

    def test_blank_type_value_kept(self, tmp_path):
        # A blank Type cell is tolerated junk, not a non-book row.
        cat = make_catalog(tmp_path, [
            '10,Blank Type,Author,Pub,2007,,Anytown,"BP 10",500010,1565645995',
        ])
        assert cat.skipped_rows == 0
        assert "500010" in cat.barcode_to_isbns

    def test_type_filter_off_ingests_everything(self, tmp_path):
        # With no type column configured, every row is ingested, the export
        # is declared to contain only books.
        cfg = CatalogConfig(columns=CatalogColumns(
            isbn="ISBN", barcode="Barcode", title="Title", author="Author",
            call_number="Call Number",
        ))
        cat = make_catalog(tmp_path, [
            '5,A Collection,Author,Pub,2004,Collection,Anytown,"BP 4",500003,1565645995',
        ], cfg=cfg)
        assert cat.skipped_rows == 0
        assert "500003" in cat.barcode_to_isbns

    def test_isbn_with_parenthetical_qualifier(self, tmp_path):
        cat = make_catalog(tmp_path, [
            '8,Qualified,Author,Pub,2007,Book,Anytown,"BP 7",500006,'
            '"086037307X (pbk. : v. 5)"',
        ])
        assert "086037307X" in cat.existing_isbns
        assert cat.barcode_to_isbns["500006"] == {canonical_isbn13("086037307X")}

    def test_stored_forms_returns_original_forms(self, tmp_path):
        cat = make_catalog(tmp_path, [
            '9,Both Forms,Author,Pub,2008,Book,Anytown,"BP 8",500007,'
            '"1565645995, 9781565645998"',
        ])
        assert cat.stored_forms("9781565645998") == {"1565645995",
                                                     "9781565645998"}


class TestHeaderValidation:
    def test_missing_isbn_column_aborts_with_names(self, tmp_path):
        # The single most dangerous misconfiguration: a differently-named
        # ISBN column would classify every book as CREATE and silently
        # disable dedup. Must abort at load, naming what is missing.
        csv_path = tmp_path / "export.csv"
        csv_path.write_text(
            'Title,Author,Barcode,ISBN-13\nT,A,500001,9781565645998\n',
            encoding="utf-8",
        )
        with pytest.raises(CatalogError) as exc:
            load_catalog(csv_path, CFG)
        msg = str(exc.value)
        assert "'ISBN'" in msg          # what was configured...
        assert "ISBN-13" in msg         # ...and what the file actually has
        assert "dedup" in msg.lower()

    def test_all_missing_columns_listed_at_once(self, tmp_path):
        csv_path = tmp_path / "export.csv"
        csv_path.write_text('A,B\n1,2\n', encoding="utf-8")
        with pytest.raises(CatalogError) as exc:
            load_catalog(csv_path, CFG)
        msg = str(exc.value)
        for name in ("ISBN", "Barcode", "Title", "Author", "Call Number",
                     "Resource ID", "Type"):
            assert repr(name) in msg

    def test_type_column_configured_but_absent_aborts(self, tmp_path):
        # Without this check a missing Type column would silently ingest
        # every resource type (movies, equipment...) into the dedup index.
        csv_path = tmp_path / "export.csv"
        csv_path.write_text(
            'Title,Author,Barcode,ISBN\nT,A,500001,9781565645998\n',
            encoding="utf-8",
        )
        cfg = CatalogConfig(columns=CatalogColumns(type="Type"))
        with pytest.raises(CatalogError, match="'Type'"):
            load_catalog(csv_path, cfg)

    def test_unconfigured_optional_columns_not_required(self, tmp_path):
        # A minimal export with only the columns the minimal config maps.
        csv_path = tmp_path / "export.csv"
        csv_path.write_text(
            'Title,Author,"Call Number",Barcode,ISBN\n'
            'T,A,"BP 1",500001,9781565645998\n',
            encoding="utf-8",
        )
        cat = load_catalog(csv_path, CatalogConfig())
        assert cat.row_count == 1
        assert "9781565645998" in cat.existing_isbns

    def test_no_call_number_column_flags_catalog(self, tmp_path):
        csv_path = tmp_path / "export.csv"
        csv_path.write_text(
            'Title,Author,Barcode,ISBN\nT,A,500001,9781565645998\n',
            encoding="utf-8",
        )
        cfg = CatalogConfig(columns=CatalogColumns(call_number=""))
        cat = load_catalog(csv_path, cfg)
        assert cat.has_call_numbers is False
        assert cat.existing_call_numbers("9781565645998") == []

    def test_empty_file_has_no_header(self, tmp_path):
        csv_path = tmp_path / "export.csv"
        csv_path.write_text("", encoding="utf-8")
        with pytest.raises(CatalogError, match="no header"):
            load_catalog(csv_path, CFG)

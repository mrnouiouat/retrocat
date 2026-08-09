"""Load the catalog export CSV into dedup lookup structures.

The export is the dedup source of truth and is never written to. Every ISBN
is indexed under BOTH its original normalized form and its canonical ISBN-13
(see docs/DESIGN.md "ISBN canonicalization"). Junk rows/values (invalid-length
ISBNs, call-number-shaped barcode strings, non-book resource types) are
logged and skipped, never raised on.

**Header validation is a hard gate.** Every configured column name is checked
against the file's real header row before any row is read; a mismatch aborts
listing what is missing. Without this, a renamed ISBN column would make every
book classify as CREATE — the dedup that is the entire point of the tool would
silently stop working, and the reconciliation gate could not catch it because
the bucket counts still balance.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import CatalogConfig
from .isbn import canonical_isbn13, extract_isbns, is_valid_isbn10, is_valid_isbn13

logger = logging.getLogger(__name__)


class CatalogError(Exception):
    """Fatal problem with the export file itself (not a junk value)."""


@dataclass
class CatalogRow:
    resource_id: str
    title: str
    author: str
    call_number: str
    barcodes: list[str]
    isbns_normalized: list[str]   # original forms, normalized
    isbns_canonical: set[str]     # ISBN-13 forms


@dataclass
class ExistingCatalog:
    existing_isbns: set[str] = field(default_factory=set)  # canonical + original forms
    barcode_to_isbns: dict[str, set[str]] = field(default_factory=dict)  # -> canonical
    rows_by_canonical_isbn: dict[str, list[CatalogRow]] = field(default_factory=dict)
    row_count: int = 0
    skipped_rows: int = 0
    has_call_numbers: bool = True  # False when no call-number column is configured

    def isbn_known(self, canonical: str) -> bool:
        return canonical in self.existing_isbns

    def stored_forms(self, canonical: str) -> set[str]:
        """Original normalized ISBN forms the export stores for a canonical ISBN-13."""
        forms: set[str] = set()
        for row in self.rows_by_canonical_isbn.get(canonical, []):
            for norm in row.isbns_normalized:
                if canonical_isbn13(norm) == canonical:
                    forms.add(norm)
        return forms

    def existing_call_numbers(self, canonical: str) -> list[str]:
        return [
            row.call_number
            for row in self.rows_by_canonical_isbn.get(canonical, [])
            if row.call_number.strip()
        ]


def _split_barcodes(value: str, resource_id: str) -> list[str]:
    barcodes: list[str] = []
    for part in re.split(r"[;,]", value or ""):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            barcodes.append(part)
        else:
            # e.g. a call-number-shaped string in the Barcode column — real
            # junk observed in production exports; skip the value, keep the
            # row. DEBUG: this is expected, tolerated junk that repeats every
            # run — the load summary reports the aggregate; run -v to see
            # each value.
            logger.debug(
                "resource %s: skipping non-numeric barcode value %r",
                resource_id, part,
            )
    return barcodes


def _validate_headers(
    fieldnames: list[str] | None, cfg: CatalogConfig, path: Path
) -> None:
    """Abort unless every configured column name exists in the header row."""
    if not fieldnames:
        raise CatalogError(f"{path}: export file has no header row")
    cols = cfg.columns
    wanted = {
        "isbn": cols.isbn,
        "barcode": cols.barcode,
        "title": cols.title,
        "author": cols.author,
        "call_number": cols.call_number,
        "resource_id": cols.resource_id,
        "type": cols.type,
    }
    present = set(fieldnames)
    missing = [
        f"{setting} = {name!r}"
        for setting, name in wanted.items()
        if name and name not in present
    ]
    if missing:
        raise CatalogError(
            f"{path}: configured column(s) not found in export header: "
            + ", ".join(missing)
            + f". Header row has: {', '.join(fieldnames)}. Fix "
            "[catalog.columns] in your config — a silently-missing ISBN or "
            "barcode column would disable deduplication entirely."
        )


def load_catalog(path: str | Path, cfg: CatalogConfig) -> ExistingCatalog:
    path = Path(path)
    cols = cfg.columns
    catalog = ExistingCatalog(has_call_numbers=bool(cols.call_number))
    # Aggregate tolerated-junk counters. Per-item detail is logged at DEBUG
    # (it repeats identically every run — noise at INFO); the counts roll up
    # into the single summary line so the signal survives at normal verbosity.
    bad_isbn_len = 0
    checksum_warn = 0
    uncanonicalizable = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        _validate_headers(reader.fieldnames, cfg, path)
        for row in reader:
            catalog.row_count += 1
            resource_id = (
                (row.get(cols.resource_id) or "").strip()
                if cols.resource_id else ""
            )
            if cols.type:
                rtype = (row.get(cols.type) or "").strip()
                if rtype and rtype != cfg.book_type:
                    logger.debug(
                        "resource %s: skipping non-%s type %r",
                        resource_id, cfg.book_type, rtype,
                    )
                    catalog.skipped_rows += 1
                    continue

            isbns_norm, rejected = extract_isbns(row.get(cols.isbn) or "")
            for token in rejected:
                bad_isbn_len += 1
                logger.debug(
                    "resource %s: skipping invalid-length ISBN value %r",
                    resource_id, token,
                )
            canonical: set[str] = set()
            kept_norm: list[str] = []
            for norm in isbns_norm:
                if len(norm) == 10 and not is_valid_isbn10(norm):
                    checksum_warn += 1
                    logger.debug(
                        "resource %s: ISBN-10 %r fails checksum — canonicalizing anyway",
                        resource_id, norm,
                    )
                if len(norm) == 13 and not is_valid_isbn13(norm):
                    checksum_warn += 1
                    logger.debug(
                        "resource %s: ISBN-13 %r fails checksum — indexing anyway",
                        resource_id, norm,
                    )
                try:
                    canonical.add(canonical_isbn13(norm))
                except ValueError:
                    # Junk that survived extraction (e.g. an 'X' in a non-final
                    # position) — skip the value, keep the row. Never crash the
                    # loader on export junk.
                    uncanonicalizable += 1
                    logger.debug(
                        "resource %s: skipping uncanonicalizable ISBN value %r",
                        resource_id, norm,
                    )
                    continue
                kept_norm.append(norm)
            isbns_norm = kept_norm

            barcodes = _split_barcodes(row.get(cols.barcode) or "", resource_id)

            cat_row = CatalogRow(
                resource_id=resource_id,
                title=(row.get(cols.title) or "").strip() if cols.title else "",
                author=(row.get(cols.author) or "").strip() if cols.author else "",
                call_number=(
                    (row.get(cols.call_number) or "").strip()
                    if cols.call_number else ""
                ),
                barcodes=barcodes,
                isbns_normalized=isbns_norm,
                isbns_canonical=canonical,
            )

            catalog.existing_isbns.update(isbns_norm)
            catalog.existing_isbns.update(canonical)
            for canon in canonical:
                catalog.rows_by_canonical_isbn.setdefault(canon, []).append(cat_row)
            for barcode in barcodes:
                catalog.barcode_to_isbns.setdefault(barcode, set()).update(canonical)

    logger.info(
        "catalog loaded: %d rows (%d non-book skipped), %d ISBN forms, "
        "%d barcodes | tolerated junk: %d bad-length ISBNs, %d checksum "
        "warnings, %d uncanonicalizable ISBNs (run -v for per-row detail)",
        catalog.row_count, catalog.skipped_rows,
        len(catalog.existing_isbns), len(catalog.barcode_to_isbns),
        bad_isbn_len, checksum_warn, uncanonicalizable,
    )
    return catalog

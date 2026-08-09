"""Bucket every scanned book: CREATE / MERGE_CANDIDATE / ALREADY_DONE / MANUAL / CONFLICT.

All ISBN comparisons use the canonical ISBN-13 form. Every book lands in
exactly one bucket; pipeline.py enforces the reconciliation invariant over
the five buckets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from .catalog import ExistingCatalog
from .config import BarcodeConfig
from .parse_scans import LoneBarcode, ScannedBook, ScanPair

logger = logging.getLogger(__name__)


class Action(str, Enum):
    CREATE = "CREATE"
    MERGE_CANDIDATE = "MERGE_CANDIDATE"
    ALREADY_DONE = "ALREADY_DONE"
    MANUAL = "MANUAL"
    CONFLICT = "CONFLICT"


@dataclass
class ClassifiedBook:
    book: ScannedBook
    action: Action
    canonical_isbn: str | None
    note: str = ""

    @property
    def barcode(self) -> str:
        return self.book.barcode

    @property
    def scanned_isbn(self) -> str | None:
        return self.book.isbn if isinstance(self.book, ScanPair) else None


@dataclass
class Classification:
    books: list[ClassifiedBook] = field(default_factory=list)

    def bucket(self, action: Action) -> list[ClassifiedBook]:
        return [b for b in self.books if b.action == action]

    def counts(self) -> dict[str, int]:
        return {action.value: len(self.bucket(action)) for action in Action}


def _range_conflict_note(barcode: str, barcodes: BarcodeConfig) -> str:
    return (
        f"new barcode {barcode} outside the configured valid new-sticker "
        f"range ({barcodes.describe_ranges()}), investigate before import"
    )


def _classify_pair(
    pair: ScanPair, catalog: ExistingCatalog, barcodes: BarcodeConfig
) -> ClassifiedBook:
    canon = pair.canonical_isbn
    on_record = catalog.barcode_to_isbns.get(pair.barcode)

    if on_record is not None:
        # Barcode already in the export. ALREADY_DONE only if the scanned ISBN
        # agrees with the record, a mismatch means a mispaired scan or a
        # sticker on the wrong book.
        if canon in on_record:
            return ClassifiedBook(
                pair, Action.ALREADY_DONE, canon,
                "barcode already in export; ISBN agrees, verify only",
            )
        if not on_record:
            # Barcode is in the export but its record carries no usable ISBN
            # (common in messy exports), nothing to disagree with.
            return ClassifiedBook(
                pair, Action.ALREADY_DONE, canon,
                "barcode already in export; record has no ISBN on file, "
                "verify by title manually",
            )
        return ClassifiedBook(
            pair, Action.CONFLICT, canon,
            f"barcode {pair.barcode} is on record for ISBN(s) "
            f"{', '.join(sorted(on_record))} but was scanned with {canon}, "
            "mispaired scan or sticker on the wrong book",
        )

    # New barcode: collision-validation rule, a fresh sticker can only
    # legitimately carry a configured free-range number. With no configured
    # ranges the check is off and this never fires.
    if not barcodes.is_valid_new_barcode(pair.barcode):
        return ClassifiedBook(
            pair, Action.CONFLICT, canon, _range_conflict_note(pair.barcode, barcodes)
        )

    if catalog.isbn_known(canon):
        return ClassifiedBook(
            pair, Action.MERGE_CANDIDATE, canon,
            "ISBN already in export under a different barcode, rides along "
            "in MARC; the ILS-side merge tool consolidates post-import",
        )
    return ClassifiedBook(pair, Action.CREATE, canon, "")


def _classify_lone(
    lone: LoneBarcode, catalog: ExistingCatalog, barcodes: BarcodeConfig
) -> ClassifiedBook:
    if lone.barcode in catalog.barcode_to_isbns:
        return ClassifiedBook(
            lone, Action.ALREADY_DONE, None,
            "barcode already in export (no ISBN scanned), verify only",
        )
    if not barcodes.is_valid_new_barcode(lone.barcode):
        return ClassifiedBook(
            lone, Action.CONFLICT, None, _range_conflict_note(lone.barcode, barcodes)
        )
    return ClassifiedBook(
        lone, Action.MANUAL, None,
        "no ISBN scanned; look up manually by physical inspection",
    )


def classify_books(
    scanned: list[ScannedBook],
    catalog: ExistingCatalog,
    barcodes: BarcodeConfig,
) -> Classification:
    result = Classification()
    for book in scanned:
        if isinstance(book, ScanPair):
            result.books.append(_classify_pair(book, catalog, barcodes))
        else:
            result.books.append(_classify_lone(book, catalog, barcodes))
    logger.info("classification counts: %s", result.counts())
    return result

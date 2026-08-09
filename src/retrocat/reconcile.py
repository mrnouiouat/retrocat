"""Reconciliation report: MERGE_CANDIDATE call numbers vs the existing catalog.

Informational only, there is no write-back path to any ILS. Nothing here
modifies the export; the report just makes call-number cleanup easy to review
before (or after) import.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from .catalog import ExistingCatalog
from .classify import Action, ClassifiedBook
from .lookup import BookMetadata

logger = logging.getLogger(__name__)

# Rendered into the existing_call_number column when the export has no
# call-number column configured at all, an explicit statement beats a blank
# cell that reads as "the catalog has no call number for this book".
NO_CALL_NUMBER_COLUMN = "(export has no call-number column)"


@dataclass
class ReconcileRow:
    isbn: str
    title: str
    existing_call_number: str
    resolved_call_number: str
    needs_fix: bool


def build_reconcile(
    books: list[ClassifiedBook],
    metadata_by_isbn: dict[str, BookMetadata],
    catalog: ExistingCatalog,
) -> list[ReconcileRow]:
    if not catalog.has_call_numbers:
        logger.warning(
            "the catalog export has no call-number column configured; the "
            "reconcile report cannot diff existing call numbers and will say "
            "so per row"
        )
    rows: list[ReconcileRow] = []
    seen: set[str] = set()
    for book in books:
        if book.action != Action.MERGE_CANDIDATE:
            continue
        canon = book.canonical_isbn
        assert canon is not None
        if canon in seen:  # one row per resource, not per copy
            continue
        seen.add(canon)
        meta = metadata_by_isbn.get(canon)
        resolved = (meta.call_number if meta and meta.call_number else "") or ""
        if catalog.has_call_numbers:
            existing = "; ".join(catalog.existing_call_numbers(canon))
            needs_fix = bool(resolved) and existing.strip() != resolved.strip()
        else:
            # No column to diff against: state that explicitly instead of
            # emitting a blank, and never flag a "fix" of an unknown value.
            existing = NO_CALL_NUMBER_COLUMN
            needs_fix = False
        rows.append(
            ReconcileRow(
                isbn=canon,
                title=(meta.title if meta and meta.title else ""),
                existing_call_number=existing,
                resolved_call_number=resolved,
                needs_fix=needs_fix,
            )
        )
    return rows


def write_reconcile_csv(rows: list[ReconcileRow], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel renders diacritics in titles correctly.
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["isbn", "title", "existing_call_number",
             "resolved_call_number", "needs_fix"]
        )
        for row in rows:
            writer.writerow(
                [row.isbn, row.title, row.existing_call_number,
                 row.resolved_call_number, str(row.needs_fix)]
            )
    logger.info("wrote reconcile report (%d rows) to %s", len(rows), path)

"""Manual worklist: the round-trip for books whose ISBN resolved nothing online.

A book lands in the MANUAL bucket for one of two reasons (see classify.py):
  * a lone barcode, no ISBN was ever scanned; or
  * an ISBN was scanned but no source (Google/OpenLibrary/LoC) returned a title.

Either way the physical book still has a title/author a human can read off the
cover. Rather than set these aside for a big end-of-project reconstruction, the
pipeline emits a **per-shelf worklist** pre-filled with everything it already
knows (shelf, barcode, ISBN). The operator fills in title/author *while at that
shelf*, and the final build ingests the filled rows and mints MARC records for
them (tagged ``source="manual"``) straight into the combined file.

**Why the worklist does NOT live under output/.** output/ is defined as
regenerable, clearing it must never destroy real work. A filled worklist is
irreplaceable hand-entered data, so it lives in a top-level, version-trackable
``manual/`` directory, one CSV per shelf. Re-running a shelf *merges* into any
existing worklist (see ``write_worklist``) so a re-scan never clobbers rows
the operator already filled in.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from .isbn import canonical_isbn13
from .language import marc_language
from .lc_call import build_call_number, extract_year
from .lookup import BookMetadata, is_corporate_author
from .marc_build import ResourceGroup

logger = logging.getLogger(__name__)

WORKLIST_HEADER = [
    "shelf", "barcode", "isbn", "title", "author", "call_number", "language",
    "notes",
]
# Columns the operator fills by hand, merged/preserved across re-runs. The
# pipeline owns shelf/barcode/isbn; the human owns these.
_OPERATOR_COLUMNS = ("title", "author", "call_number", "language", "notes")


@dataclass
class ManualEntry:
    """One row of a manual worklist. ``isbn`` is the canonical ISBN-13 when one
    was scanned, else "" (a lone-barcode book has no ISBN at all)."""

    shelf: str
    barcode: str
    isbn: str = ""
    title: str = ""
    author: str = ""
    call_number: str = ""
    # Free-form: "Arabic", "ar", and "ara" all resolve (see language.py).
    # Blank falls back to the configured default language. Books that reach
    # the worklist skew non-English, they are exactly the ones the ISBN APIs
    # do not index.
    language: str = ""
    notes: str = ""

    @property
    def filled(self) -> bool:
        """A row is usable once a human has supplied a title, never fabricate."""
        return bool(self.title.strip())


def read_worklist(path: str | Path) -> list[ManualEntry]:
    """Read a worklist CSV. Missing file -> empty list (nothing filled yet)."""
    path = Path(path)
    if not path.is_file():
        return []
    entries: list[ManualEntry] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            entries.append(
                ManualEntry(
                    shelf=(row.get("shelf") or "").strip(),
                    barcode=(row.get("barcode") or "").strip(),
                    isbn=(row.get("isbn") or "").strip(),
                    title=(row.get("title") or "").strip(),
                    author=(row.get("author") or "").strip(),
                    call_number=(row.get("call_number") or "").strip(),
                    # .get(): worklists written before the language column
                    # existed still read cleanly.
                    language=(row.get("language") or "").strip(),
                    notes=(row.get("notes") or "").strip(),
                )
            )
    return entries


def write_worklist(entries: list[ManualEntry], path: str | Path) -> None:
    """Write a worklist, **preserving any hand-entered values already on disk**.

    The pipeline supplies fresh shelf/barcode/isbn for the current manual books;
    for any barcode that already has operator columns filled in the existing
    file, those values win. This makes re-running a shelf safe: an operator's
    typed-in titles survive a re-scan. Rows sorted by barcode for a stable diff.
    """
    path = Path(path)
    prior = {e.barcode: e for e in read_worklist(path)}
    merged: list[ManualEntry] = []
    for entry in entries:
        old = prior.get(entry.barcode)
        if old is not None:
            for col in _OPERATOR_COLUMNS:
                old_val = getattr(old, col)
                if old_val:
                    setattr(entry, col, old_val)
        merged.append(entry)
    merged.sort(key=lambda e: e.barcode)

    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel on Windows renders diacritics in typed-in titles.
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(WORKLIST_HEADER)
        for e in merged:
            writer.writerow(
                [e.shelf, e.barcode, e.isbn, e.title, e.author,
                 e.call_number, e.language, e.notes]
            )
    filled = sum(1 for e in merged if e.filled)
    logger.info(
        "wrote manual worklist (%d rows, %d filled) to %s",
        len(merged), filled, path,
    )


def manual_metadata(entry: ManualEntry, default_lc_class: str) -> BookMetadata:
    """Turn a filled worklist row into MARC-ready metadata.

    Uses the operator's call number verbatim when given; otherwise generates a
    shelf-able LC-shaped number locally (``default_lc_class`` + Cutter from the
    main entry + any year visible in the notes), never blank, never a network
    call. Always tagged source="manual", confidence="low": the title/author are
    exact (a human read them off the book) but the subject class is a default
    guess.
    """
    canon = canonical_isbn13(entry.isbn) if entry.isbn else ""
    call = entry.call_number.strip()
    if not call:
        call = build_call_number(
            default_lc_class,
            author=entry.author or None,
            title=entry.title or None,
            year=extract_year(entry.notes),
            corporate=is_corporate_author(entry.author or ""),
        )
    return BookMetadata(
        isbn13=canon,
        title=entry.title.strip(),
        author=entry.author.strip() or None,
        call_number=call,
        call_number_source="manual",
        confidence="low",
        # Unparseable or blank -> None -> marc_build stamps the configured
        # default language.
        language=marc_language(entry.language),
    )


def build_manual_groups(
    entries: list[ManualEntry], default_lc_class: str
) -> list[ResourceGroup]:
    """Group filled manual entries into MARC resources.

    Same rule as the main path: group by canonical ISBN-13 when present (two
    copies of one titled book -> one resource, two copies); a lone-barcode book
    with no ISBN is its own single-copy resource keyed by barcode. Unfilled
    rows are skipped, they stay in the MANUAL bucket for a human to finish.
    """
    groups: dict[str, ResourceGroup] = {}
    order: list[str] = []
    for entry in entries:
        if not entry.filled:
            continue
        meta = manual_metadata(entry, default_lc_class)
        key = meta.isbn13 or f"barcode:{entry.barcode}"
        if key not in groups:
            groups[key] = ResourceGroup(
                canonical_isbn=meta.isbn13, metadata=meta
            )
            order.append(key)
        groups[key].barcodes.append(entry.barcode)
    return [groups[k] for k in order]

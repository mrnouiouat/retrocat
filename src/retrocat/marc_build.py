"""Build validated MARC21 records from resolved book metadata.

The field mapping was validated end-to-end during the original deployment: an
ILS vendor's support team loaded a pilot file with this exact structure into
their sandbox and confirmed correct resources and copies (see
docs/VALIDATION.md). Do not redesign it casually — extend it.

All library-identity values (home library string, shelving location, item
status, default 008 language) come from ``LibraryConfig``; a blank location
or status simply omits that subfield so the target ILS applies its own
import defaults.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pymarc import Field, Indicators, MARCReader, Record, Subfield

from .classify import Action, ClassifiedBook
from .config import LibraryConfig
from .lookup import BookMetadata, is_corporate_author

logger = logging.getLogger(__name__)

LEADER = "00000nam a2200000 a 4500"


class MarcValidationError(Exception):
    """A generated record failed round-trip validation — a bug, not shippable."""


@dataclass
class ResourceGroup:
    """One MARC resource: a canonical ISBN, its metadata, and 1+ copy barcodes."""

    canonical_isbn: str
    metadata: BookMetadata
    barcodes: list[str] = field(default_factory=list)
    # Export-stored ISBN forms differing from the canonical scanned form —
    # each becomes an extra (repeatable) 020. Insurance in case the ILS-side
    # merge tool matches ISBNs literally rather than canonically; harmless
    # when the matching is already form-agnostic.
    extra_isbn_forms: list[str] = field(default_factory=list)


def group_books(
    books: list[ClassifiedBook],
    metadata_by_isbn: dict[str, BookMetadata],
    stored_forms_by_isbn: dict[str, set[str]] | None = None,
) -> list[ResourceGroup]:
    """Group CREATE/MERGE_CANDIDATE books by canonical ISBN-13, scan order.

    Same ISBN scanned with two barcodes -> ONE resource with two copies
    (confirmed with the ILS vendor as cleaner than relying on their merge
    tool for same-file duplicates).
    """
    stored_forms_by_isbn = stored_forms_by_isbn or {}
    groups: dict[str, ResourceGroup] = {}
    order: list[str] = []
    for book in books:
        if book.action not in (Action.CREATE, Action.MERGE_CANDIDATE):
            continue
        canon = book.canonical_isbn
        assert canon is not None  # CREATE/MERGE always carry an ISBN
        if canon not in groups:
            extra = sorted(
                form for form in stored_forms_by_isbn.get(canon, set())
                if form != canon
            )
            groups[canon] = ResourceGroup(
                canonical_isbn=canon,
                metadata=metadata_by_isbn[canon],
                extra_isbn_forms=extra if book.action == Action.MERGE_CANDIDATE else [],
            )
            order.append(canon)
        groups[canon].barcodes.append(book.barcode)
    return [groups[c] for c in order]


def _field_008(
    build_date: date, language: str | None, default_language: str
) -> str:
    """Build the sandbox-validated 40-char 008 template: date + language at
    35-37.

    ``language`` is a MARC code resolved per book from the lookup sources
    (see language.py); ``default_language`` (config ``[library].marc_language``)
    fills in when no source reported one, so the field is never blank.
    """
    lang = language or default_language
    if len(lang) != 3:
        raise MarcValidationError(
            f"008 language must be a 3-character MARC code, got {lang!r}"
        )
    stamp = build_date.strftime("%y%m%d")
    f = f"{stamp}s{' ' * 8}xx{' ' * 12}000 0 {lang} d"
    assert len(f) == 40
    return f


def build_record(
    group: ResourceGroup,
    library: LibraryConfig,
    build_date: date | None = None,
) -> Record:
    """Build one resource record. Field order matches the validated pilot:
    008, [010], 020(+), 100/110, 245, [050], then one 852/876 pair per copy.
    """
    meta = group.metadata
    if not meta.title:
        raise MarcValidationError(
            f"refusing to build a record without a title (ISBN {group.canonical_isbn}) "
            "— untitled books are MANUAL, never placeholder CREATEs"
        )
    record = Record(leader=LEADER, to_unicode=True, force_utf8=True)
    record.add_field(
        Field(tag="008",
              data=_field_008(build_date or date.today(), meta.language,
                              library.marc_language))
    )
    if meta.lccn:
        record.add_field(
            Field(tag="010", indicators=Indicators(" ", " "),
                  subfields=[Subfield("a", meta.lccn)])
        )
    # A manual no-ISBN book has an empty canonical_isbn — emit no 020 rather
    # than a blank subfield $a (an empty 020 is malformed, not "no ISBN").
    for isbn_form in [group.canonical_isbn, *group.extra_isbn_forms]:
        if not isbn_form:
            continue
        record.add_field(
            Field(tag="020", indicators=Indicators(" ", " "),
                  subfields=[Subfield("a", isbn_form)])
        )
    if meta.author:
        if is_corporate_author(meta.author):
            tag, ind = "110", Indicators("2", " ")
        else:
            tag, ind = "100", Indicators("1", " ")
        record.add_field(
            Field(tag=tag, indicators=ind, subfields=[Subfield("a", meta.author)])
        )
    # OpenLibrary sometimes returns a subtitle identical to the title
    # (observed live) — don't emit "Title: Title".
    if meta.subtitle and meta.subtitle.strip().lower() != meta.title.strip().lower():
        title = f"{meta.title}: {meta.subtitle}"
    else:
        title = meta.title
    record.add_field(
        Field(tag="245", indicators=Indicators("0", "0"),
              subfields=[Subfield("a", title)])
    )
    if meta.call_number:
        record.add_field(
            Field(tag="050", indicators=Indicators(" ", "4"),
                  subfields=[Subfield("a", meta.call_number)])
        )
    for barcode in group.barcodes:
        subs_852 = [Subfield("b", library.home_library)]
        if library.location:
            subs_852.append(Subfield("c", library.location))
        if meta.call_number:
            subs_852.append(Subfield("h", meta.call_number))
        subs_852.append(Subfield("p", barcode))
        record.add_field(
            Field(tag="852", indicators=Indicators(" ", " "), subfields=subs_852)
        )
        subs_876 = [Subfield("p", barcode)]
        if meta.call_number:
            subs_876.append(Subfield("h", meta.call_number))
        if library.status:
            subs_876.append(Subfield("j", library.status))
        record.add_field(
            Field(tag="876", indicators=Indicators(" ", " "), subfields=subs_876)
        )
    return record


def _roundtrip_validate(marc_bytes: bytes, records: list[Record]) -> None:
    """Every record must parse back out of the serialized file cleanly."""
    reread = list(MARCReader(io.BytesIO(marc_bytes), to_unicode=True))
    if len(reread) != len(records):
        raise MarcValidationError(
            f"round-trip record count mismatch: wrote {len(records)}, "
            f"read back {len(reread)}"
        )
    for i, (orig, back) in enumerate(zip(records, reread)):
        if back is None:
            raise MarcValidationError(f"record {i} failed to parse back")
        orig_fields = [str(f) for f in orig.fields]
        back_fields = [str(f) for f in back.fields]
        if orig_fields != back_fields:
            raise MarcValidationError(
                f"record {i} changed across round-trip:\n"
                f"  wrote: {orig_fields}\n  read:  {back_fields}"
            )


def build_marc_file(
    groups: list[ResourceGroup],
    out_path: str | Path,
    library: LibraryConfig,
    build_date: date | None = None,
) -> list[Record]:
    """Build, round-trip-validate, and write UTF-8 MARC21 (.mrc, never MARCXML)."""
    records = [
        build_record(g, library, build_date=build_date) for g in groups
    ]
    marc_bytes = b"".join(r.as_marc() for r in records)
    _roundtrip_validate(marc_bytes, records)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(marc_bytes)
    logger.info("wrote %d MARC records to %s", len(records), out_path)
    return records

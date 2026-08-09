"""Pipeline orchestration: parse -> catalog -> classify -> lookup -> reports -> MARC.

Hard rules enforced here (docs/DESIGN.md non-negotiables):
- Reconciliation gate: every scanned book in exactly one bucket, counts add
  up (including CONFLICT), BEFORE the MARC file is written. Gate failure ->
  PipelineError, no .mrc.
- Conflict gate: any book in the CONFLICT bucket blocks the MARC write unless
  ``allow_conflicts`` is set. Either gate failing also deletes a stale .mrc
  left by an earlier run, so a blocked run never leaves a shippable-looking
  file behind.
- All reports (master table, manual, reconcile) are written before the .mrc.
- Inputs (scan files, the catalog export) are never mutated.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .catalog import ExistingCatalog, load_catalog
from .classify import Action, Classification, ClassifiedBook, classify_books
from .config import Config
from .lookup import BookMetadata, ClassMap, LookupClient, load_class_map
from .manual import (
    ManualEntry,
    build_manual_groups,
    manual_metadata,
    write_worklist,
)
from .marc_build import build_marc_file, group_books
from .parse_scans import dedupe_scans, parse_scan_dir, parse_scan_file
from .reconcile import build_reconcile, write_reconcile_csv

logger = logging.getLogger(__name__)


def _shelf_of(book: ClassifiedBook) -> str:
    """Shelf label a scanned book belongs to = its scan file's stem
    (``scans/shelf-1a.txt`` -> ``shelf-1a``). This is how manual books are
    tied back to their shelf without any end-of-project reconstruction."""
    return Path(book.book.source_file).stem


def _parse_any(scans: str | Path, config: Config):
    """Parse either a whole scans directory or a single shelf file.

    A directory goes through parse_scan_dir (globs + dedupes). A single file is
    parsed and deduped on its own so per-shelf runs share the exact same
    dedupe semantics as the combined run.
    """
    path = Path(scans)
    if path.is_dir():
        return parse_scan_dir(path, config.barcodes)
    return dedupe_scans(parse_scan_file(path, config.barcodes))


class PipelineError(Exception):
    """Fatal pipeline failure — the run must exit non-zero with no .mrc."""


@dataclass
class PipelineResult:
    counts: dict[str, int] = field(default_factory=dict)
    scanned_total: int = 0
    marc_records: int = 0
    out_dir: Path | None = None
    # Review digest: bucket -> barcodes a human must look at. Built so the
    # operator reads this instead of scanning a huge CSV for problems.
    needs_review: dict[str, list[str]] = field(default_factory=dict)


def _lookup_metadata(
    classification: Classification, client: LookupClient
) -> dict[str, BookMetadata]:
    """Resolve metadata for every distinct CREATE/MERGE canonical ISBN."""
    isbns: list[str] = []
    for book in classification.books:
        if book.action in (Action.CREATE, Action.MERGE_CANDIDATE):
            assert book.canonical_isbn is not None
            if book.canonical_isbn not in isbns:
                isbns.append(book.canonical_isbn)
    metadata: dict[str, BookMetadata] = {}
    try:
        for i, isbn in enumerate(isbns, start=1):
            metadata[isbn] = client.lookup(isbn)
            if i % 50 == 0:
                logger.info("lookup progress: %d/%d", i, len(isbns))
    finally:
        # Flush even on a mid-run crash/interrupt — at multi-thousand-book
        # scale an unflushed tail is dozens of lookups re-fetched for nothing.
        client.flush_cache()
    return metadata


def _demote_untitled(
    classification: Classification, metadata: dict[str, BookMetadata]
) -> None:
    """No title from any source -> MANUAL, never a placeholder CREATE."""
    for book in classification.books:
        if book.action not in (Action.CREATE, Action.MERGE_CANDIDATE):
            continue
        meta = metadata.get(book.canonical_isbn or "")
        if meta is None or not meta.resolved:
            book.action = Action.MANUAL
            book.note = (
                f"no title resolved from any source for ISBN "
                f"{book.canonical_isbn} — look up manually by physical inspection"
            )


def _flag_call_number_quality(
    classification: Classification, metadata: dict[str, BookMetadata]
) -> None:
    """Append a cataloger-facing note to CREATE/MERGE books whose call number
    needs a human eye, so the weak ones are easy to spot in master_table.csv
    at scale:

    - blank call number (should be rare now every titled book gets one, but kept
      as a safety net) -> "assign manually";
    - source == 'default' -> the subject class is a pure last-resort default (no
      category/subject matched), the weakest guess -> "verify shelving".

    Category-inferred class_fallback rows are not noted here — they carry a
    plausible class and are already marked confidence=low for spot-check."""
    for book in classification.books:
        if book.action not in (Action.CREATE, Action.MERGE_CANDIDATE):
            continue
        meta = metadata.get(book.canonical_isbn or "")
        if not (meta and meta.resolved):
            continue
        flag = None
        if not meta.call_number:
            flag = "no call number resolved — assign manually"
        elif meta.call_number_source == "default":
            flag = "subject class defaulted (no category matched) — verify shelving"
        if flag:
            book.note = f"{book.note} | {flag}" if book.note else flag


def _build_review_digest(
    classification: Classification,
    metadata: dict[str, BookMetadata],
    resolved_manual: set[str] | None = None,
) -> dict[str, list[str]]:
    """Everything a human must look at, as bucket -> sorted barcodes.

    The point: at backfill scale nobody re-reads the whole master table; they
    read this digest and jump straight to the flagged rows. Manual books that
    have already been resolved via a filled worklist (``resolved_manual``) are
    NOT flagged — they are handled and ride in the MARC file.
    """
    resolved_manual = resolved_manual or set()
    digest: dict[str, list[str]] = {
        "conflicts": [], "manual": [],
        "blank_call_number": [], "defaulted": [], "low_confidence": [],
    }
    for book in classification.books:
        if book.action == Action.CONFLICT:
            digest["conflicts"].append(book.barcode)
        elif book.action == Action.MANUAL:
            if book.barcode not in resolved_manual:
                digest["manual"].append(book.barcode)
        elif book.action in (Action.CREATE, Action.MERGE_CANDIDATE):
            meta = metadata.get(book.canonical_isbn or "")
            if not (meta and meta.resolved):
                continue
            # Order matters: a 'default' source is also confidence=low, but it's
            # the weakest tier (pure default class) and gets its own bucket.
            if not meta.call_number:
                digest["blank_call_number"].append(book.barcode)
            elif meta.call_number_source == "default":
                digest["defaulted"].append(book.barcode)
            elif meta.confidence == "low":
                digest["low_confidence"].append(book.barcode)
    return {k: sorted(v) for k, v in digest.items() if v}


def _discard_stale_marc(mrc_path: Path) -> None:
    """Delete a leftover .mrc from an earlier run when this run is refusing to
    write one.

    Without this, a blocked run leaves a plausible-looking file sitting at the
    exact path the operator expects, with a timestamp from whenever the last
    clean run happened — and the failure mode is shipping it. output/ is
    regenerable by definition, so removing it costs nothing and closes the
    trap; the hand-entered manual/ worklists are never touched.
    """
    if mrc_path.exists():
        mrc_path.unlink()
        logger.warning(
            "removed stale MARC file %s — this run is not writing one", mrc_path
        )


def _reconciliation_gate(
    classification: Classification, scanned_total: int, mrc_path: Path
) -> None:
    counts = classification.counts()
    bucketed = sum(counts.values())
    if bucketed != scanned_total:
        _discard_stale_marc(mrc_path)
        raise PipelineError(
            f"reconciliation gate FAILED: {scanned_total} books scanned but "
            f"{bucketed} bucketed ({counts}) — refusing to write any MARC output"
        )
    logger.info(
        "reconciliation gate passed: %d scanned = %s", scanned_total, counts
    )


def _conflict_gate(
    conflicts: list[ClassifiedBook], mrc_path: Path, allow_conflicts: bool
) -> None:
    """Refuse to write MARC while any book is in the CONFLICT bucket.

    A CONFLICT means the scan disagrees with the catalog about which book a
    barcode belongs to — a mispaired scan, or a sticker physically applied to
    the wrong book. The book itself is already excluded from the MARC file, but
    an unresolved conflict means the *rest* of the file is built on a scan
    stream known to contain at least one bad pairing, and it should not reach a
    live import on a warning alone. ``allow_conflicts`` is the deliberate
    override for an operator who has reviewed them and accepts the risk.
    """
    if not conflicts:
        return
    if allow_conflicts:
        logger.warning(
            "%d CONFLICT book(s) present but --allow-conflicts was given — "
            "writing MARC anyway; conflicted books are still excluded",
            len(conflicts),
        )
        return
    _discard_stale_marc(mrc_path)
    shown = "; ".join(f"{b.barcode}: {b.note}" for b in conflicts[:5])
    more = f" (+{len(conflicts) - 5} more)" if len(conflicts) > 5 else ""
    raise PipelineError(
        f"conflict gate FAILED: {len(conflicts)} book(s) in the CONFLICT "
        f"bucket — refusing to write MARC. Resolve them (see master_table.csv) "
        f"or re-run with --allow-conflicts to ship without them. [{shown}{more}]"
    )


def _write_master_table(
    books: list[ClassifiedBook],
    metadata: dict[str, BookMetadata],
    path: Path,
    manual_by_barcode: dict[str, ManualEntry] | None = None,
    default_lc_class: str = "AC",
) -> None:
    """One row per scanned book, sorted by action then barcode for easy review.

    A first ``shelf`` column ties each book to its scan file — essential in the
    combined final table where books from every shelf are interleaved. Manual
    books resolved from a filled worklist (``manual_by_barcode``) render the
    hand-entered title/author/call number with source ``manual``.
    """
    manual_by_barcode = manual_by_barcode or {}
    rows = []
    for book in sorted(books, key=lambda b: (b.action.value, b.barcode)):
        entry = manual_by_barcode.get(book.barcode)
        if entry is not None and entry.filled:
            meta = manual_metadata(entry, default_lc_class)
            note = (f"{book.note} | entered manually — shipped in MARC"
                    if book.note else "entered manually — shipped in MARC")
        else:
            meta = metadata.get(book.canonical_isbn or "")
            note = book.note
        rows.append([
            _shelf_of(book),
            book.barcode,
            book.action.value,
            meta.title if meta and meta.title else "",
            meta.author if meta and meta.author else "",
            book.canonical_isbn or "",
            meta.call_number if meta and meta.call_number else "",
            meta.call_number_source if meta and meta.call_number_source else "",
            meta.confidence if meta and meta.confidence else "",
            note,
        ])
    # utf-8-sig: the BOM makes Excel on Windows render diacritics correctly —
    # this file exists to be eyeballed by a human, usually in Excel.
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "shelf", "barcode", "action", "title", "author", "isbn",
            "call_number", "call_number_source", "confidence", "notes",
        ])
        writer.writerows(rows)
    logger.info("wrote master table (%d rows) to %s", len(rows), path)


def _write_manual_worklist(
    books: list[ClassifiedBook], path: Path, resolved: set[str] = frozenset()
) -> None:
    """Emit the fill-in worklist for every MANUAL book (merge-preserving).

    Pre-fills what the pipeline knows — shelf, barcode, ISBN (blank for a
    lone-barcode book), and the classification hint in ``notes`` — leaving
    title/author for the operator to complete at the shelf. See manual.py for
    why this lives outside the regenerable output/ tree and how re-runs preserve
    already-entered rows.

    ``resolved`` barcodes (already handled via a filled worklist entry this
    run) are excluded — the final build's leftover report must list only the
    books that still need a human, not re-list handled ones as blank rows.
    """
    entries = [
        ManualEntry(
            shelf=_shelf_of(book),
            barcode=book.barcode,
            isbn=book.canonical_isbn or "",
            notes=book.note,
        )
        for book in books
        if book.action == Action.MANUAL and book.barcode not in resolved
    ]
    write_worklist(entries, path)


def run_pipeline(
    scans_dir: str | Path,
    export_path: str | Path,
    out_dir: str | Path,
    config: Config,
    cache_path: str | Path = ".cache/lookup_cache.json",
    lookup_client: LookupClient | None = None,
    build_date: date | None = None,
    mrc_name: str | None = None,
    manual_worklist_path: str | Path | None = None,
    manual_entries: list[ManualEntry] | None = None,
    allow_conflicts: bool = False,
) -> PipelineResult:
    """Run the pipeline over a scans directory OR a single shelf file.

    ``mrc_name`` names the .mrc inside ``out_dir`` (e.g. ``shelf-1a.mrc`` for a
    per-shelf triage run); None uses ``[output].mrc_filename`` from config.

    ``manual_worklist_path`` is where the fill-in worklist for this run's MANUAL
    books is written (defaults to ``out_dir/manual_worklist.csv``); it lives
    outside output/ in real use so hand-entered rows survive an output/ wipe.

    ``manual_entries`` are already-filled worklist rows (the final build passes
    the union of every shelf's worklist). Each whose barcode is a MANUAL book in
    this run is minted into the MARC file as a ``source="manual"`` record.

    ``allow_conflicts`` ships the MARC file even when books landed in the
    CONFLICT bucket. The default refuses — see ``_conflict_gate``.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mrc_path = out_dir / (mrc_name or config.output.mrc_filename)

    scanned = _parse_any(scans_dir, config)
    logger.info("parsed %d scanned books total (after dedupe)", len(scanned))

    catalog: ExistingCatalog = load_catalog(export_path, config.catalog)
    classification = classify_books(scanned, catalog, config.barcodes)

    class_map: ClassMap = load_class_map(
        config.lookup.class_map_file or None
    )
    client = lookup_client or LookupClient(
        cache_path=cache_path, class_map=class_map
    )
    metadata = _lookup_metadata(classification, client)
    _demote_untitled(classification, metadata)
    _flag_call_number_quality(classification, metadata)

    # Match filled manual entries to this run's MANUAL books (by barcode). An
    # entry with no matching MANUAL book here (stale worklist, or a book that
    # now classifies differently) is ignored rather than minting a phantom.
    manual_barcodes = {b.barcode for b in classification.bucket(Action.MANUAL)}
    manual_by_barcode: dict[str, ManualEntry] = {}
    for entry in manual_entries or []:
        if not entry.filled:
            continue
        if entry.barcode not in manual_barcodes:
            logger.warning(
                "manual worklist row for barcode %s has no matching MANUAL book "
                "in this run — skipping", entry.barcode,
            )
            continue
        manual_by_barcode[entry.barcode] = entry

    # Gates + all reports BEFORE the MARC file is touched.
    _reconciliation_gate(classification, len(scanned), mrc_path)
    _write_master_table(
        classification.books, metadata, out_dir / "master_table.csv",
        manual_by_barcode, class_map.default_class,
    )
    _write_manual_worklist(
        classification.books,
        Path(manual_worklist_path) if manual_worklist_path
        else out_dir / "manual_worklist.csv",
        resolved=set(manual_by_barcode),
    )
    reconcile_rows = build_reconcile(classification.books, metadata, catalog)
    write_reconcile_csv(reconcile_rows, out_dir / "reconcile.csv")

    conflicts = classification.bucket(Action.CONFLICT)
    for book in conflicts:
        logger.warning("CONFLICT %s: %s", book.barcode, book.note)
    # Deliberately after the reports: a blocked run must still leave the
    # operator the master table that explains WHY it blocked.
    _conflict_gate(conflicts, mrc_path, allow_conflicts)

    stored_forms = {
        b.canonical_isbn: catalog.stored_forms(b.canonical_isbn)
        for b in classification.books
        if b.action == Action.MERGE_CANDIDATE and b.canonical_isbn
    }
    groups = group_books(classification.books, metadata, stored_forms)
    # Manual records ride at the end of the file, in worklist order.
    manual_groups = build_manual_groups(
        list(manual_by_barcode.values()), class_map.default_class
    )
    if manual_groups:
        logger.info("adding %d manually-entered MARC record(s)", len(manual_groups))
    records = build_marc_file(
        groups + manual_groups,
        mrc_path,
        config.library,
        build_date=build_date,
    )

    result = PipelineResult(
        counts=classification.counts(),
        scanned_total=len(scanned),
        marc_records=len(records),
        out_dir=out_dir,
        needs_review=_build_review_digest(
            classification, metadata, set(manual_by_barcode)
        ),
    )
    logger.info(
        "pipeline complete: %d scanned -> %d MARC records; counts %s",
        result.scanned_total, result.marc_records, result.counts,
    )
    return result

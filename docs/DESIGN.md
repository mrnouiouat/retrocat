# Design: data contracts, classification rules, and why

The engineering reference for retrocat's behavior. These contracts came out of
a real backfill, with real scanner output, a real ILS export with real junk in
it, and a vendor sandbox validation round. The edge cases below all actually
happened, which is why the rules are as specific as they are. Change them
knowingly or not at all.

## Non-negotiables

- **No network calls in unit tests.** All HTTP is mocked and the suite runs
  offline in seconds.
- **Idempotent.** Re-running on the same inputs produces the same output. Scan
  files and the catalog export are never mutated.
- **Reports before MARC.** Every run writes its reconciliation reports before
  touching the `.mrc`. A failed gate exits non-zero, writes no `.mrc`, and
  removes any stale one.
- **Nothing gets fabricated.** If no source produces a title, the book goes to
  the manual worklist instead of into a placeholder MARC record.
- **No ILS write path.** The only write surface is the `.mrc` file a human
  imports. `reconcile.py` is report-only.

## Scan files (`parse_scans.py`)

One code per line, two lines per book: ISBN then its barcode. Whitespace-only
lines, CRLF, and a UTF-8 BOM are tolerated. Anything else unrecognizable is a
hard error with file and line.

**Token classification** (deterministic, config-driven):

- Length 10 or 13, digits, optional trailing `X` for ISBN-10 → **ISBN**,
  checksum-validated, and an ISBN-13 must carry a `978`/`979` prefix. Checksum
  failures are hard errors. They exist to catch hand-typed ISBNs, which is the
  one place transcription typos enter.
- Exactly `[barcodes].length` digits, within `min`/`max` when configured →
  **barcode**. A barcode length of 10 or 13 is rejected at config load, since
  it would make the stream ambiguous.
- Anything else is a hard error. Fail loud rather than guess.

**Pairing state machine**, with states `EXPECT_ISBN` and `EXPECT_BARCODE`:

| State | Token | Result |
|---|---|---|
| EXPECT_ISBN | ISBN | open a pair |
| EXPECT_ISBN | barcode | **lone barcode** (no-ISBN book), legal, goes to MANUAL |
| EXPECT_BARCODE | barcode | close the pair |
| EXPECT_BARCODE | ISBN | **hard error**. An identical ISBN gets a "double-scan, delete one line" message; a different ISBN gets "a barcode was never scanned" |
| EXPECT_BARCODE | EOF | hard error (trailing unpaired ISBN) |

The two asymmetric cases are contractual. Two barcodes in a row is *not* an
error: the first closes the open pair and the second is a book with no ISBN.
Two ISBNs in a row *is* an error. Both were confirmed against real scanning
behavior.

**Cross-file dedup:** the same barcode with the same ISBN seen twice is
deduped silently, since that's an idempotent re-scan. The same barcode with
two *different* ISBNs, or lone in one file and paired in another, is a hard
error, because it means a mispaired scan or a sticker that moved between
books.

## ISBN canonicalization (`isbn.py`)

The canonical form everywhere is **ISBN-13** (10 to 13: prepend `978`,
recompute the check digit). Catalog exports skew heavily toward ISBN-10 while
scanner reads are ISBN-13 EANs, so raw string comparison silently misses real
duplicates, and the reconciliation gate can't catch it because the bucket
totals still balance. This is the project's canonical example of a *silent*
failure class. Several other rules (header validation, dual 020s) exist
because they're the same class wearing a different costume.

Conversion is applied even when the ISBN-10 check digit is wrong, because the
recomputed EAN check digit rescues check-digit-only typos in the export.

## Catalog export (`catalog.py`)

The export is the dedup source of truth and is never written to. Column names
come from `[catalog.columns]` and are **validated against the file's real
header row before any row is read**. A misnamed ISBN column would classify
every book as CREATE and disable dedup with all totals balancing, so a missing
configured column aborts, listing what was expected alongside what the file
actually has. The same applies to the optional `type` column, where
configured-but-absent would silently ingest movies and equipment.

Every ISBN is indexed under **both** its original normalized form and its
canonical ISBN-13. `barcode_to_isbns` maps each export barcode to the
canonical ISBN or ISBNs on record for it. It's a dict rather than a bare set
because the ALREADY_DONE agreement check needs the mapping.

**Junk tolerance:** invalid-length ISBN values, call-number-shaped strings in
the barcode column, uncanonicalizable tokens (an `X` mid-string), and non-book
resource types are logged and skipped rather than raised on. The row survives
the loss of a junk value, so its barcode is still indexed.

## Classification (`classify.py`)

All comparisons are canonical. Exactly one bucket per book:

- Barcode on record and the scanned ISBN agrees → **ALREADY_DONE**
  (verify-only).
- Barcode on record and the record has no ISBN → **ALREADY_DONE** with a
  verify-by-title note. Common in messy exports.
- Barcode on record and the ISBN disagrees → **CONFLICT**, meaning a mispaired
  scan or a sticker on the wrong book. Never silently accepted, and kept out
  of the MARC.
- New barcode outside the configured valid-new-sticker ranges → **CONFLICT**,
  since a fresh sticker can't legitimately carry that number. With no ranges
  configured this check is off.
- ISBN known, barcode new → **MERGE_CANDIDATE**. Ships in the MARC as its own
  resource; the ILS-side merge tool consolidates by ISBN after import.
- ISBN unknown → **CREATE**.
- Lone barcode → **MANUAL**, and it never enters lookup.
- Same ISBN twice in a scan → both classified normally, then MARC building
  groups them into one resource with two copies.

The pipeline asserts `scanned = Σ buckets` (the reconciliation gate), and a
non-empty CONFLICT bucket blocks the MARC write (the conflict gate, with
`--allow-conflicts` to override). Both gates run *after* the reports are
written, and both remove a stale `.mrc`.

## Lookup (`lookup.py`, `language.py`, `lc_call.py`)

Three sources in order, Google Books then OpenLibrary then LoC SRU, with the
first hit winning **per field**, so a title can come from one source and a
call number from another.

- **Retry policy:** bounded exponential backoff on 429 and transient 5xx for
  Google and OpenLibrary, both of which really do 503 in bursts. An
  *unrecovered* transient error marks the lookup uncacheable, so a re-run
  re-fetches instead of permanently poisoning that book. LoC connection
  failures are handled separately: the source self-disables only after 3
  *consecutive* failures, since one blip shouldn't kill it for a whole run,
  and any success resets the counter. That endpoint is genuinely unreachable
  from some networks.
- **ISBN-10 retry:** when all sources miss on the 13-form, the full chain is
  retried once with the ISBN-10 form, because OpenLibrary and LoC often index
  pre-2007 titles only under ISBN-10.
- **OpenLibrary call-number trap:** `lc_classifications` can carry a blank
  entry ahead of the real one, so blanks are skipped and index 0 is never
  taken blindly.
- **Language (MARC 008/35-37):** resolved from LoC's own 008/041, which is
  already a MARC code, then Google's ISO 639-1, mapped explicitly to the ISO
  639-2/B variants MARC requires (`per` not `fas`, `ger` not `deu`). Getting
  that wrong is silent. With no signal it falls back to the config default. A
  non-3-character code is a hard error, since it would shift every position
  after 35 in the fixed-length field.
- **Class-level fallback:** when no source has a call number, an LC class is
  inferred from Google categories plus OpenLibrary subjects via an ordered
  keyword map, shipped as an overridable TOML data file where first match wins
  (so narrower keys precede words that contain them). A bare class isn't
  shelf-able, so `lc_call.py` enriches it offline with a Cutter number per LC
  Shelflisting Manual G 63 and the year, producing `BP130 .W55 2017` rather
  than `BP130`. When nothing maps, it uses the map's `default_class` (LC
  General Works) tagged `source=default`, the weakest tier. Every titled book
  leaves with a call number; only untitled books, which go to MANUAL, have
  none.
- **Confidence vocabulary:** `google | openlibrary | loc | class_fallback |
  default | manual`, with `confidence` of `high` (authoritative) or `low`
  (inferred) driving the review digest.
- **Caching:** raw source payloads are cached by canonical ISBN-13 in
  `.cache/`, deliberately outside `output/`. `output/` is regenerable and safe
  to delete; the cache isn't.

## MARC generation (`marc_build.py`)

The field mapping is sandbox-validated (see VALIDATION.md), so extend it
rather than redesigning it. Per resource: leader `00000nam a2200000 a 4500`
with pymarc recomputing lengths, `008` (build date plus resolved language),
`010` when an LCCN was found and never as an empty field, repeatable `020`
(no ISBN means no `020` rather than a blank one), `100`/`110` per the
corporate-author keyword heuristic, `245` (subtitle folded into `$a`, and a
subtitle identical to the title is dropped, which was observed live), `050`,
then one `852`/`876` pair per copy carrying the configured home
library/location/status. Blank config values omit the subfield so the ILS
applies its own defaults.

- **Multi-copy grouping:** books are grouped by canonical ISBN-13 *before*
  record building, giving one resource with one 852/876 pair per barcode. The
  ILS vendor confirmed this is cleaner than relying on their merge tool for
  same-file duplicates. It has never yet been observed in a live import (see
  VALIDATION.md).
- **Dual 020s:** a merge candidate whose export row stores a different ISBN
  form emits both forms, as insurance in case the ILS merge tool matches ISBN
  strings literally rather than canonically.
- **Round-trip validation:** the serialized file is read back with
  `MARCReader` and compared field by field before shipping. A record that
  doesn't survive the round trip is a bug rather than output.

## Manual worklist round-trip (`manual.py`)

Books the APIs can't identify still have a cover a human can read. Each shelf
run emits `manual/<shelf>.csv` pre-filled with what the pipeline knows (shelf,
barcode, ISBN, a hint) plus blank `title/author/call_number/language` columns.
Rules:

- `manual/` lives outside `output/`, because one is regenerable and the other
  is irreplaceable. Re-writing a worklist **merges**: operator-entered values
  for a barcode already on disk always win, and a barcode that's no longer
  MANUAL is dropped.
- The `final` run ingests every filled row into the combined MARC with
  `source=manual` and `confidence=low`. Grouping matches the main path, and a
  no-ISBN book emits no `020`. An operator-supplied call number is used
  verbatim; otherwise one is generated locally from the default class, a
  Cutter, and any year in the notes. Never blank, and never a network call.
- A filled row whose barcode isn't a MANUAL book in the current run is
  ignored rather than minted into a phantom record. Unfilled rows stay
  flagged and are listed in `output/_final/unfilled_manual.csv`.

## Reconciliation report (`reconcile.py`)

One row per merge-candidate resource: the export's existing call number
against the resolved one, with `needs_fix` set when they differ. Report-only.

The shipped MARC always carries the *resolved* call number. In the original
deployment the export's call numbers for re-scanned books were mostly junk,
with barcodes sitting in the call-number field, so incoming-wins was both
simpler and more correct than trying to detect which existing values were
worth keeping. When the export has no call-number column configured, the
report says so explicitly in each row instead of emitting blanks that would
read as "no call number on record."

# Design — data contracts, classification rules, and why

The engineering reference for retrocat's behavior. These contracts came out
of a real backfill (real scanner output, a real ILS export with real junk in
it, a vendor sandbox validation round); the edge cases below are all things
that actually happened, which is why the rules are specific. Change them
knowingly or not at all.

## Non-negotiables

- **No network calls in unit tests.** All HTTP mocked; the suite runs
  offline in seconds.
- **Idempotent.** Re-running on the same inputs produces the same output.
  Scan files and the catalog export are never mutated.
- **Reports before MARC.** Every run writes its reconciliation reports
  before touching the `.mrc`; a failed gate exits non-zero, writes no
  `.mrc`, and removes a stale one.
- **Never fabricate.** No title from any source → the book goes to the
  manual worklist; a placeholder MARC record is never created.
- **No ILS write path.** The only write surface is the `.mrc` file a human
  imports. `reconcile.py` is report-only.

## Scan files (`parse_scans.py`)

One code per line; two lines per book: ISBN then its barcode. Whitespace-only
lines, CRLF, and a UTF-8 BOM are tolerated; anything else unrecognizable is
a hard error with file + line.

**Token classification** (deterministic, config-driven):

- Length 10 or 13, digits (optional trailing `X` for ISBN-10) → **ISBN**,
  checksum-validated. ISBN-13 must carry a `978`/`979` prefix. Checksum
  failures are hard errors — they catch hand-typed ISBNs, the one place
  transcription typos enter.
- Exactly `[barcodes].length` digits (within `min`/`max` when configured)
  → **barcode**. A barcode length of 10 or 13 is rejected at config load —
  it would make the stream ambiguous.
- Anything else → hard error. Fail loud, don't guess.

**Pairing state machine** — states `EXPECT_ISBN` / `EXPECT_BARCODE`:

| State | Token | Result |
|---|---|---|
| EXPECT_ISBN | ISBN | open a pair |
| EXPECT_ISBN | barcode | **lone barcode** (no-ISBN book) — legal, → MANUAL |
| EXPECT_BARCODE | barcode | close the pair |
| EXPECT_BARCODE | ISBN | **hard error** — identical ISBN gets a "double-scan, delete one line" message; different ISBN gets "a barcode was never scanned" |
| EXPECT_BARCODE | EOF | hard error (trailing unpaired ISBN) |

The two asymmetric cases are contractual: two barcodes in a row is *not* an
error (first closes the open pair; second is a no-ISBN book), two ISBNs in
a row *is*. Both were confirmed against real scanning behavior.

**Cross-file dedup:** the same barcode + same ISBN seen twice → deduped
silently (idempotent re-scan). The same barcode with two *different* ISBNs
(or lone in one file, paired in another) → hard error: a mispaired scan or
a sticker moved between books.

## ISBN canonicalization (`isbn.py`)

Canonical form everywhere is **ISBN-13** (10→13: prepend `978`, recompute
the check digit). Catalog exports skew heavily ISBN-10 while scanner reads
are ISBN-13 EANs, so raw string comparison silently misses real duplicates —
and the reconciliation gate cannot catch it, because the bucket totals still
balance. This is the project's canonical example of a *silent* failure
class, and several other rules (header validation, dual 020s) exist because
they are the same class in a different costume.

Conversion is applied even when the ISBN-10 check digit is wrong — the
recomputed EAN check digit rescues check-digit-only typos in the export.

## Catalog export (`catalog.py`)

The export is the dedup source of truth and is never written to. Column
names come from `[catalog.columns]` and are **validated against the file's
real header row before any row is read** — a misnamed ISBN column would
classify every book as CREATE and disable dedup with all totals balancing,
so a missing configured column aborts, listing what was expected and what
the file actually has. The same applies to the optional `type` column
(configured-but-absent would silently ingest movies and equipment).

Every ISBN is indexed under **both** its original normalized form and its
canonical ISBN-13. `barcode_to_isbns` maps each export barcode to the
canonical ISBN(s) on record for it — a dict, not a bare set, because the
ALREADY_DONE agreement check needs the mapping.

**Junk tolerance:** invalid-length ISBN values, call-number-shaped strings
in the barcode column, uncanonicalizable tokens (an `X` mid-string), and
non-book resource types are logged and skipped — never raised on. The row
survives the loss of a junk value (its barcode is still indexed).

## Classification (`classify.py`)

All comparisons canonical. Exactly one bucket per book:

- Barcode on record + scanned ISBN agrees → **ALREADY_DONE** (verify-only).
- Barcode on record + record has no ISBN → **ALREADY_DONE** with a
  verify-by-title note (common in messy exports).
- Barcode on record + ISBN disagrees → **CONFLICT** (mispaired scan or a
  sticker on the wrong book). Never silently accepted; kept out of MARC.
- New barcode outside the configured valid-new-sticker ranges → **CONFLICT**
  (a fresh sticker can't legitimately carry that number). With no ranges
  configured this check is off.
- ISBN known, barcode new → **MERGE_CANDIDATE**. Ships in the MARC as its
  own resource; the ILS-side merge tool consolidates by ISBN after import.
- ISBN unknown → **CREATE**.
- Lone barcode → **MANUAL** (never enters lookup).
- Same ISBN twice in a scan → both classified normally; MARC building
  groups them into one resource with two copies.

The pipeline asserts `scanned = Σ buckets` (the reconciliation gate) and a
non-empty CONFLICT bucket blocks the MARC write (the conflict gate,
`--allow-conflicts` to override). Both gates run *after* the reports are
written and both remove a stale `.mrc`.

## Lookup (`lookup.py`, `language.py`, `lc_call.py`)

Three sources in order — Google Books, OpenLibrary, LoC SRU — first hit
wins **per field** (title can come from one source, call number another).

- **Retry policy:** bounded exponential backoff on 429 and transient 5xx
  for Google/OpenLibrary (both really do 503 in bursts). An *unrecovered*
  transient error marks the lookup uncacheable, so a re-run re-fetches
  instead of permanently poisoning that book. LoC connection failures are
  handled separately: the source self-disables only after 3 *consecutive*
  failures (one blip must not kill it for a whole run), and any success
  resets the counter. The endpoint is genuinely unreachable from some
  networks.
- **ISBN-10 retry:** when all sources miss the 13-form, the full chain is
  retried once with the ISBN-10 form — OpenLibrary and LoC often index
  pre-2007 titles only under ISBN-10.
- **OpenLibrary call-number trap:** `lc_classifications` can carry a blank
  entry ahead of the real one — blanks are skipped, never index 0 blindly.
- **Language (MARC 008/35-37):** resolved from LoC's own 008/041 (already a
  MARC code) then Google's ISO 639-1, mapped explicitly to the ISO 639-2/B
  variants MARC requires (`per` not `fas`, `ger` not `deu`) — getting that
  wrong is silent. No signal → the config default. A non-3-character code
  is a hard error, since it would shift every position after 35 in the
  fixed-length field.
- **Class-level fallback:** when no source has a call number, an LC class
  is inferred from Google categories + OpenLibrary subjects via an ordered
  keyword map (shipped as an overridable TOML data file; first match wins,
  so narrower keys precede words they contain). A bare class is not
  shelf-able, so `lc_call.py` enriches it — fully offline — with a Cutter
  number per LC Shelflisting Manual G 63 and the year: `BP130 .W55 2017`,
  not `BP130`. Nothing maps → the map's `default_class` (LC General Works),
  tagged `source=default`, the weakest tier. Every titled book leaves with
  a call number; only untitled books (→ MANUAL) have none.
- **Confidence vocabulary:** `google | openlibrary | loc | class_fallback |
  default | manual`, with `confidence` `high` (authoritative) or `low`
  (inferred/estimated) driving the review digest.
- **Caching:** raw source payloads cached by canonical ISBN-13 in
  `.cache/` — deliberately *not* under `output/`, which is regenerable and
  safe to delete; the cache is not.

## MARC generation (`marc_build.py`)

The field mapping is sandbox-validated (see VALIDATION.md) — extend, don't
redesign. Per resource: leader `00000nam a2200000 a 4500` (pymarc recomputes
lengths), `008` (build date + resolved language), `010` when an LCCN was
found (never an empty field), repeatable `020` (no ISBN → no 020, never a
blank one), `100`/`110` per the corporate-author keyword heuristic, `245`
(subtitle folded into `$a`; a subtitle identical to the title is dropped —
observed live), `050`, then one `852`/`876` pair per copy with the
configured home library/location/status (blank config values omit the
subfield so the ILS applies its defaults).

- **Multi-copy grouping**: books grouped by canonical ISBN-13 *before*
  record building — one resource, one 852/876 pair per barcode. Confirmed
  with the ILS vendor as cleaner than relying on their merge tool for
  same-file duplicates; never yet observed in a live import (see
  VALIDATION.md).
- **Dual 020s**: a merge candidate whose export row stores a different ISBN
  form emits both forms — insurance in case the ILS merge tool matches ISBN
  strings literally rather than canonically.
- **Round-trip validation**: the serialized file is read back with
  `MARCReader` and compared field-by-field before shipping. A record that
  doesn't survive the round trip is a bug, not output.

## Manual worklist round-trip (`manual.py`)

Books the APIs can't identify still have a cover a human can read. Each
shelf run emits `manual/<shelf>.csv` pre-filled with what the pipeline
knows (shelf, barcode, ISBN, a hint) and blank
`title/author/call_number/language` columns. Rules:

- `manual/` lives outside `output/` — regenerable vs. irreplaceable.
  Re-writing a worklist **merges**: operator-entered values for a barcode
  already on disk always win; a barcode no longer MANUAL is dropped.
- The `final` run ingests every filled row into the combined MARC
  (`source=manual`, `confidence=low`); grouping matches the main path; a
  no-ISBN book emits no `020`. An operator call number is used verbatim,
  else one is generated locally (default class + Cutter + any year in the
  notes) — never blank, never a network call.
- A filled row whose barcode is not a MANUAL book in the current run is
  ignored, not minted into a phantom record. Unfilled rows stay flagged and
  are listed in `output/_final/unfilled_manual.csv`.

## Reconciliation report (`reconcile.py`)

One row per merge-candidate resource: the export's existing call number vs.
the resolved one, `needs_fix` when they differ. Report-only. The shipped
MARC always carries the *resolved* call number — in the original deployment
the export's call numbers for re-scanned books were mostly junk (barcodes
sitting in the call-number field), so incoming-wins was both simpler and
more correct. When the export has no call-number column configured, the
report says so explicitly per row instead of emitting blanks that read as
"no call number on record".

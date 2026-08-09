# Operator guide — running a backfill with retrocat

This is the process document: how a person with a barcode scanner, a sticker
roll, and a few thousand uncataloged books actually runs the job. The README
covers installation and the sample demo; [DESIGN.md](DESIGN.md) covers why
the pipeline behaves the way it does.

## The shape of the whole thing

1. **Capture** each shelf into a plain-text scan file (two sweeps, below).
2. **Triage** each shelf: `retrocat shelf --scan scans/<shelf>.txt --export <export>.csv`.
   Review the reports; fill the manual worklist while you're at that shelf.
3. **Build once**: `retrocat final --scans scans/ --export <export>.csv`
   produces the single combined `.mrc` for your ILS.
4. **Sandbox-check** the file in your ILS before any live import (checklist
   below), then import.

Shelves are split into separate files **only to isolate problems** — a bad
scan session contaminates one file, not the whole corpus. The import file is
always built by the `final` run over all shelves together; per-shelf `.mrc`
files are triage artifacts, never concatenated.

## Capture — the two-sweep paired method

The method binds each barcode to its ISBN **physically**, so book order
never matters and a fumble can't silently propagate.

**Sweep 1 — label (fast, any order).** Walk the shelf. Book already has one
of your barcode stickers? Skip it. Otherwise apply the next sticker from the
roll (tape over it if your stickers scuff), and reshelve. A book with no
ISBN printed anywhere: set it aside — you'll scan its sticker alone in
sweep 2.

**Sweep 2 — scan pairs (ISBN, then barcode).** For each book, two scans:
the printed **ISBN** (13-digit EAN, or the older 10-digit form), then the
**barcode sticker** now on it. Two lines in the file, back to back.

- Already-stickered books: same pairing — scan ISBN, then the existing
  sticker. This is how the pipeline *verifies* previously processed books
  instead of skipping them blind.
- ISBN printed but won't scan: type it. Every ISBN is checksum-validated at
  parse time, so a typo is caught at its exact line, not imported.
- No ISBN at all: scan just the barcode. The lone barcode routes to the
  manual worklist with its number already known.

**File format:** plain text, one code per line, one file per shelf or
session (`shelf-a.txt`). A USB scanner types digits + Enter, so capture is
pure rhythm — no columns, no spreadsheet. The pipeline tells ISBNs from
barcodes by length (configured in `[barcodes]`), and validates the
ISBN/barcode alternation: two ISBNs in a row, a trailing unpaired ISBN, or
a checksum failure is flagged at that exact file and line.

**Why this is trustworthy:** the barcode is physically on the book when you
scan its ISBN, so the binding is *captured, not inferred*. There is no
sequence to keep in sync, so reshelving, reordering, or a dropped stack
cannot corrupt anything that already scanned.

## Triage — what to do with each shelf run

Each `shelf` run writes to `output/<shelf>/`:

- `master_table.csv` — one row per scanned book, sorted by action then
  barcode. This is the file you spot-check.
- `reconcile.csv` — for each merge candidate, your catalog's existing call
  number vs. the resolved one.
- `<shelf>.mrc` — a per-shelf MARC file for sandbox spot-checks only.

And one file *outside* `output/`: `manual/<shelf>.csv`, the **fill-in
worklist** for books the APIs couldn't identify (no ISBN, or an ISBN no
source recognizes — typically self-published, print-on-demand, or
non-Western-market books). Fill in `title`/`author` off the physical book
while you're standing at that shelf; optionally `call_number` (used
verbatim) and `language` ("Arabic", "ar", and "ara" all work). Leave
`call_number` blank to get a locally generated LC-shaped number.

`manual/` is deliberately outside `output/`: `output/` is regenerable and
safe to delete, while a filled worklist is irreplaceable hand-entered data.
Re-running a shelf **merges** — your typed-in rows survive a re-scan.

The console review digest lists exactly which barcodes need eyes:

- **CONFLICT** — the scan disagrees with the catalog about which book a
  barcode belongs to, or a fresh sticker carries an impossible number.
  Resolve these before shipping; the run refuses to write the `.mrc` while
  any exist (`--allow-conflicts` overrides after you've reviewed them).
- **MANUAL** — fill the worklist.
- **Estimated call numbers** (`confidence=low`) — spot-check a sample.
- **Defaulted class** — the weakest tier; verify shelving for these.

## The gates — "done" is defined

The pipeline enforces, in order, before any MARC file is written:

1. **Reconciliation** — every scanned book lands in exactly one bucket and
   the counts add up (`scanned = CREATE + MERGE_CANDIDATE + ALREADY_DONE +
   MANUAL + CONFLICT`). Failure aborts with no `.mrc`.
2. **Conflict gate** — a non-empty CONFLICT bucket blocks the write.
3. **Structural** — every record round-trips through a MARC reader.

Both blocking gates still write the reports first (a blocked run must
explain itself) and delete any stale `.mrc` from an earlier clean run, so
a failure never leaves yesterday's plausible-looking file where today's
good one should be.

## Sandbox checklist — before any live import

Load the `final` output into your ILS's sandbox/test environment and check:

- [ ] A sample of records: title, author, call number, barcode, location.
- [ ] **One multi-copy record** (same ISBN, two barcodes) imports as ONE
  resource with two attached copies — this path is structurally tested but
  not validated against your ILS until you check it.
- [ ] A merge candidate consolidates into its existing resource the way you
  expect, and note which ISBN form your ILS matched on (retrocat ships both
  forms as insurance).
- [ ] If your ILS merges resources: understand what happens to *existing*
  copies' call numbers. In the ILS this pipeline was built against, merging
  replaced resource-level fields with the incoming record's, but existing
  copies kept their old (often junk) copy-level call numbers — a
  post-import cleanup item no import file can fix.
- [ ] Location/status subfields (`852 $c` / `876 $j`) land where you want
  them — or blank them in config and rely on your ILS's import defaults.

## Practical notes

- **API caching**: every lookup response is cached in
  `.cache/lookup_cache.json` keyed by canonical ISBN-13. Re-runs and the
  `final` build cost no repeat API calls. Don't delete `.cache/` casually —
  at backfill scale it represents thousands of polite API calls.
- **Google Books key**: optional (`.env`), raises rate limits. The run
  degrades gracefully without it.
- **Everything is re-runnable.** Same scan files + same export = same
  output. Fixing a scan error and re-running is always safe; nothing
  mutates your scan files or your catalog export, and there is no
  write-back to your ILS of any kind.

# Operator guide: running a backfill with retrocat

This is the process document, meaning how a person with a barcode scanner, a
sticker roll, and a few thousand uncataloged books actually runs the job. The
README covers installation and the sample demo, and [DESIGN.md](DESIGN.md)
covers why the pipeline behaves the way it does.

## The shape of the whole thing

1. **Capture** each shelf into a plain-text scan file, using the two sweeps
   described below.
2. **Triage** each shelf with `retrocat shelf --scan scans/<shelf>.txt
   --export <export>.csv`. Review the reports and fill the manual worklist
   while you're still at that shelf.
3. **Build once.** `retrocat final --scans scans/ --export <export>.csv`
   produces the single combined `.mrc` for your ILS.
4. **Sandbox-check** the file in your ILS before any live import (checklist
   below), then import.

Shelves get split into separate files only so a problem stays isolated. A bad
scan session contaminates one file rather than the whole corpus. The import
file is always built by the `final` run over all shelves together; per-shelf
`.mrc` files are triage artifacts and should never be concatenated.

## Capture: the two-sweep paired method

The method binds each barcode to its ISBN **physically**, so book order never
matters and a fumble can't silently propagate.

**Sweep 1, label.** Fast, any order. Walk the shelf. If a book already has one
of your barcode stickers, skip it. Otherwise apply the next sticker from the
roll (tape over it if your stickers scuff) and reshelve. Set aside any book
with no ISBN printed anywhere; you'll scan its sticker alone in sweep 2.

**Sweep 2, scan pairs.** ISBN, then barcode. For each book, two scans: the
printed **ISBN**, either the 13-digit EAN or the older 10-digit form, then the
**barcode sticker** now on it. Two lines in the file, back to back.

- Already-stickered books get the same treatment: scan the ISBN, then the
  existing sticker. This is how the pipeline *verifies* previously processed
  books rather than skipping them blind.
- If an ISBN is printed but won't scan, type it. Every ISBN is
  checksum-validated at parse time, so a typo gets caught at its exact line
  rather than imported.
- If there's no ISBN at all, scan just the barcode. The lone barcode routes to
  the manual worklist with its number already known.

**File format:** plain text, one code per line, one file per shelf or session
(`shelf-a.txt`). A USB scanner types digits and Enter, so capture is pure
rhythm, with no columns and no spreadsheet. The pipeline tells ISBNs from
barcodes by length, configured in `[barcodes]`, and validates the
ISBN/barcode alternation. Two ISBNs in a row, a trailing unpaired ISBN, or a
checksum failure all get flagged at that exact file and line.

**Why this is trustworthy:** the barcode is physically on the book when you
scan its ISBN, so the binding is captured rather than inferred. There's no
sequence to keep in sync, which means reshelving, reordering, or a dropped
stack can't corrupt anything that already scanned.

## Triage: what to do with each shelf run

Each `shelf` run writes to `output/<shelf>/`:

- `master_table.csv`, one row per scanned book, sorted by action then barcode.
  This is the file you spot-check.
- `reconcile.csv`, showing your catalog's existing call number against the
  resolved one for each merge candidate.
- `<shelf>.mrc`, a per-shelf MARC file for sandbox spot-checks only.

And one file *outside* `output/`: `manual/<shelf>.csv`, the fill-in worklist
for books the APIs couldn't identify. Those are books with no ISBN, or with an
ISBN no source recognizes, typically self-published, print-on-demand, or
non-Western-market titles. Fill in `title` and `author` off the physical book
while you're standing at that shelf. Optionally fill `call_number`, which is
used verbatim, and `language`, where "Arabic", "ar", and "ara" all work.
Leaving `call_number` blank gets you a locally generated LC-shaped number.

`manual/` sits outside `output/` deliberately. `output/` is regenerable and
safe to delete, while a filled worklist is irreplaceable hand-entered data.
Re-running a shelf **merges**, so your typed-in rows survive a re-scan.

The console review digest lists exactly which barcodes need eyes:

- **CONFLICT.** The scan disagrees with the catalog about which book a barcode
  belongs to, or a fresh sticker carries an impossible number. Resolve these
  before shipping. The run refuses to write the `.mrc` while any exist, and
  `--allow-conflicts` overrides that once you've reviewed them.
- **MANUAL.** Fill the worklist.
- **Estimated call numbers** (`confidence=low`). Spot-check a sample.
- **Defaulted class.** The weakest tier, so verify shelving for these.

## The gates: "done" is defined

The pipeline enforces three things, in order, before any MARC file is written:

1. **Reconciliation.** Every scanned book lands in exactly one bucket and the
   counts add up (`scanned = CREATE + MERGE_CANDIDATE + ALREADY_DONE + MANUAL
   + CONFLICT`). Failure aborts with no `.mrc`.
2. **Conflict gate.** A non-empty CONFLICT bucket blocks the write.
3. **Structural.** Every record round-trips through a MARC reader.

Both blocking gates still write the reports first, since a blocked run has to
explain itself, and both delete any stale `.mrc` from an earlier clean run. A
failure never leaves yesterday's plausible-looking file sitting where today's
good one should be.

## Sandbox checklist, before any live import

Load the `final` output into your ILS's sandbox or test environment and check:

- [ ] A sample of records: title, author, call number, barcode, location.
- [ ] **One multi-copy record** (same ISBN, two barcodes) imports as ONE
  resource with two attached copies. This path is structurally tested but
  isn't validated against your ILS until you check it yourself.
- [ ] A merge candidate consolidates into its existing resource the way you
  expect. Note which ISBN form your ILS matched on, since retrocat ships both
  forms as insurance.
- [ ] If your ILS merges resources, find out what happens to *existing*
  copies' call numbers. In the ILS this pipeline was built against, merging
  replaced resource-level fields with the incoming record's, but existing
  copies kept their old and often junk copy-level call numbers. That's a
  post-import cleanup item no import file can fix.
- [ ] Location and status subfields (`852 $c` and `876 $j`) land where you
  want them. Alternatively, blank them in config and rely on your ILS's
  import defaults.

## Practical notes

- **Fixing call numbers by hand?** The offline generator is available
  standalone. `retrocat callnumber --lc-class BP130 --author "Garry Wills"
  --year 2017` prints `BP130 .W55 2017`, and `--batch books.csv` processes a
  whole spreadsheet. It's handy when reviewing `reconcile.csv` or assigning
  numbers to worklist books whose class you already know.
- **API caching.** Every lookup response is cached in
  `.cache/lookup_cache.json`, keyed by canonical ISBN-13, so re-runs and the
  `final` build cost no repeat API calls. Don't delete `.cache/` casually. At
  backfill scale it represents thousands of polite API calls.
- **Google Books key.** Optional, via `.env`, and it raises your rate limits.
  The run degrades gracefully without one.
- **Everything is re-runnable.** The same scan files plus the same export
  produce the same output. Fixing a scan error and re-running is always safe.
  Nothing mutates your scan files or your catalog export, and there's no
  write-back to your ILS of any kind.

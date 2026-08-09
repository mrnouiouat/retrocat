# retrocat

**Retrospective conversion cataloging for small libraries**: turns paired
barcode-scanner output (ISBN + item barcode, two lines per book) into a
validated **MARC21 `.mrc` file** ready for bulk import into a library ILS —
with deduplication against your existing catalog, reconciliation reports, and
a review digest that tells a human exactly which records need eyes.

Built for a real problem: The Islamic Seminary of America (Richardson, TX)
had 2,000+ physical titles to catalog, scoped as **4–6 months of manual data
entry**. This pipeline was built instead, and the seminary's president
approved open-sourcing it (institution named with permission). Small
religious and academic libraries hit this exact problem with no budget for
commercial cataloging software; retrocat is the generalized, configurable
version of the tool that job produced.

Pure Python 3.11+, two dependencies (`pymarc`, `requests`), 270+ fully
offline tests.

## Status, honestly

The MARC field mapping was **validated by the ILS vendor's support team
loading a pilot file into their sandbox** and confirming correct resources
and copies — title, author, call number, barcode, location, status
(details: [docs/VALIDATION.md](docs/VALIDATION.md)). The pipeline has
processed a real pilot and first shelf at the source library; the full
~2,000-book backfill is in progress there, not finished. Once a shelf is
physically scanned, the pipeline turns it into import-ready MARC in minutes —
the projected cost of the whole backfill is roughly the shelf-scanning time
plus a manual worklist for the handful of books no API can identify, instead
of months of per-book data entry.

## What it does

1. **Parses scan files** (`scans/*.txt`) with an explicit pairing state
   machine — lone barcodes (no-ISBN books) are routed to a manual bucket,
   mispaired scans fail loudly with file + line numbers, and every ISBN is
   checksum-validated to catch typing errors at the source.
2. **Dedupes against your existing catalog** (a CSV export from your ILS,
   column names mapped in config). All ISBN matching is canonical: ISBN-10s
   are converted to ISBN-13 before comparison, so a 13-digit EAN scan still
   matches a book the catalog lists under its 10-digit form. The loader
   tolerates real-world export junk (bad ISBN lengths, call-number-shaped
   barcodes, non-book rows) by logging and skipping — and **validates your
   configured column names against the file's real header row**, because a
   silently missing ISBN column would disable dedup entirely.
3. **Classifies every book** into exactly one bucket — `CREATE`,
   `MERGE_CANDIDATE`, `ALREADY_DONE`, `MANUAL`, or `CONFLICT` — and refuses
   to produce output if the bucket counts don't reconcile with the scan
   count. Unresolved conflicts **block the MARC write** (override:
   `--allow-conflicts`), and a blocked run deletes any stale `.mrc` from an
   earlier run so a plausible-looking wrong file is never left behind.
4. **Resolves metadata** (title, author, LC call number, LCCN, language) via
   Google Books, OpenLibrary, and Library of Congress SRU, with response
   caching, bounded exponential backoff on 429/5xx, an ISBN-10 retry for
   pre-2007 titles, and graceful per-source degradation — a flaky source
   never crashes the run, and an unrecovered transient error is never
   cached, so a re-run re-fetches it.
5. **Generates shelf-able LC call numbers locally** when no source has one:
   an inferred LC class (from Google Books categories and OpenLibrary
   subject headings) is enriched with a **Cutter number** built from the
   Library of Congress Cutter table (Shelflisting Manual G 63) plus the
   publication year — entirely offline, e.g. `BP130 .W55 2017`. Every call
   number is tagged with its source and a confidence level so estimates get
   spot-checked rather than trusted.
6. **Builds MARC21 records** with pymarc — one resource per distinct ISBN,
   one 852/876 copy pair per barcode (a book scanned on two shelves imports
   as one resource with two copies) — and round-trips every record through
   a MARC reader before writing.
7. **Prints a review digest** at the end of every run: exactly which
   barcodes need human eyes (conflicts, manual lookups, estimated call
   numbers), so nobody has to scan a 2,000-row spreadsheet for problems.

The call-number generator (step 5) is also exposed as a **standalone
offline tool** — `retrocat callnumber` — for catalogers who just want
properly shaped LC call numbers without running any pipeline (see below).

## The interesting engineering

- **The ISBN-10/13 canonicalization bug class.** Catalog exports skew
  ISBN-10; scanners read ISBN-13 EANs. Compare raw strings and a re-scanned
  book classifies as *new* — the duplicate ships, and no total or checksum
  ever disagrees, so reconciliation can't catch it. Every comparison in
  retrocat goes through canonical ISBN-13, and the catalog-export header
  validation exists because a misnamed ISBN column reproduces the same
  silent failure a different way.
- **An explicit pairing state machine** with two deliberately asymmetric
  rules: two barcodes in a row is *not* an error (the second is a no-ISBN
  book), while two ISBNs in a row *is* (a barcode was never scanned). Both
  came out of real scanning behavior, and both are pinned by tests that
  explain why they must not be "fixed."
- **Offline LC Cutter generation** per Shelflisting Manual G 63, verified
  against two independent transcriptions of the table, with the anchor case
  `cutter("Wills") == "W55"` reproducing a real Library of Congress call
  number end-to-end.
- **Confidence tagging over false confidence.** Authoritative call numbers
  are `high`; inferred ones are `low` and surfaced in the review digest;
  books whose subject couldn't even be inferred get a distinct weakest tier
  (`default`) — and a book with no resolvable title never gets a fabricated
  record at all; it goes to the manual worklist for a human.

## Requirements

- Python 3.11+
- `pymarc`, `requests` (installed automatically)
- Optional: a Google Books API key (`.env`, see below) — without one the
  pipeline still runs; anonymous Google quota just throttles the
  category-based call-number fallback.

## Setup

```bash
git clone https://github.com/thefirstsamurai/retrocat
cd retrocat
pip install -e .

cp .env.example .env                # optional: add GOOGLE_BOOKS_API_KEY
cp sample/config.toml config.toml   # then edit for your library
```

Everything library-specific lives in `config.toml`: your library's name and
holdings values, your barcode scheme (length, plausible range, valid
new-sticker ranges — or switch the range check off entirely), the column
names of your catalog export, and the output filename. The config is
validated loudly at startup; unknown keys and missing columns are hard
errors, not silent defaults.

## Run it on the sample data

`sample/` contains a synthetic 58-row catalog export and two shelf scan
files (real ISBNs — they resolve against the live APIs). The whole flow:

```bash
cd sample
retrocat shelf --scan scans/shelf-a.txt --export catalog_export.csv
```

Expected output (network results vary slightly):

```
INFO retrocat.catalog: catalog loaded: 58 rows (2 non-book skipped), 88 ISBN forms, 55 barcodes | ...
INFO retrocat.classify: classification counts: {'CREATE': 6, 'MERGE_CANDIDATE': 1, 'ALREADY_DONE': 1, 'MANUAL': 1, 'CONFLICT': 0}
INFO retrocat.pipeline: reconciliation gate passed: 9 scanned = {...}
INFO retrocat.marc_build: wrote 6 MARC records to output/shelf-a/shelf-a.mrc
OK: 9 scanned -> 6 MARC records (output/shelf-a/shelf-a.mrc); counts: {'CREATE': 6, 'MERGE_CANDIDATE': 1, 'ALREADY_DONE': 1, 'MANUAL': 1, 'CONFLICT': 0}

NEEDS HUMAN REVIEW (see master_table.csv for details):
  MANUAL - fill in title/author in manual/<shelf>.csv [1]: 500115

1 book(s) need manual identification - fill in title/author at: manual/shelf-a.csv
```

Open `manual/shelf-a.csv`, type a title for the lone-barcode book (that's
the human step between the two commands), then build the combined file:

```bash
retrocat final --scans scans --export catalog_export.csv
```

```
OK: 11 scanned -> 8 MARC records (output/_final/catalog_import.mrc); counts: {'CREATE': 8, 'MERGE_CANDIDATE': 1, 'ALREADY_DONE': 1, 'MANUAL': 1, 'CONFLICT': 0}
Nothing flagged for review.

Manual worklists: 1/1 filled and shipped in the MARC file.
```

`output/_final/catalog_import.mrc` is the file you'd hand to your ILS. The
**final file is always built by re-running over all shelves together**,
never by concatenating per-shelf `.mrc` files — that's what makes a book
scanned on two shelves import as one resource with two copies. (There's
also `sample/conflict-demo.txt` — run it as a shelf to watch the conflict
gate refuse to write.)

## Sample input and output

A scan file is just alternating ISBN/barcode lines, one code per line —
exactly what a USB barcode scanner types:

```
9781565645998
500101
9780199836741
500102
500115          <- lone barcode: a book with no ISBN, routed to MANUAL
```

`master_table.csv` (one row per scanned book, the thing you spot-check):

```
shelf    barcode  action           title                       call_number          call_number_source
shelf-a  500102   CREATE           Reading the Qur'an          BP130.4 .S376 2011   openlibrary
shelf-a  500104   CREATE           Hitler's American Model     KK4743 .W48 2018     loc
shelf-a  500115   MANUAL           Untitled Local Chapbook     AC .C38              manual
shelf-a  500101   MERGE_CANDIDATE  Abu Zayd al-Balkhi's ...    R128.3 .B34213 2013  openlibrary
```

And one decoded MARC record from a live run — a merge candidate, carrying
**both** ISBN forms because the catalog stored the ISBN-10 (insurance for
ILS merge tools that match ISBN strings literally):

```
=LDR  00458nam a2200121 a 4500
=008  260809s\\\\\\\\xx\\\\\\\\\\\\000\0\eng\d
=020  \\$a9781565645998
=020  \\$a1565645995
=100  1\$aMalik Badri
=245  00$aAbu Zayd al-Balkhi's Sustenance of the Soul: The Cognitive Behavior Therapy of A Ninth Century Physician
=050  \4$aR128.3 .B34213 2013
=852  \\$bAnytown College Library$cMain Campus$hR128.3 .B34213 2013$p500101
=876  \\$p500101$hR128.3 .B34213 2013$jAvailable
```

## Standalone tool: LC call numbers without the pipeline

The offline call-number generator is useful on its own — any cataloger who
knows a book's LC class but needs a properly shaped, non-colliding shelf
number can use it directly, no config and no network:

```bash
# Full call number: class + Cutter (Shelflisting Manual G 63) + year
$ retrocat callnumber --lc-class BP130 --author "Garry Wills" --year 2017
BP130 .W55 2017

# Just the Cutter for a word
$ retrocat callnumber --cutter Wills
W55

# A whole spreadsheet at once: reads a CSV with an lc_class column
# (optional author/title/year/corporate), writes it back with a
# call_number column appended
$ retrocat callnumber --batch books.csv > books_with_callnumbers.csv
```

It follows LC main-entry practice: personal authors Cutter on the surname,
corporate bodies on the organization's first significant word (auto-detected
from common org keywords, or forced with `--corporate`), no-author books on
the first significant title word with leading articles skipped. The same
code is importable as a library: `from retrocat.lc_call import cutter,
build_call_number`.

## Adapting it to your library

The short version — five steps from clone to your first shelf:

1. `pip install -e .` and copy `sample/config.toml` next to your data.
2. Export your catalog from your ILS as CSV and put your export's exact
   column headers in `[catalog.columns]` (retrocat aborts with a clear list
   if they don't match the file).
3. Describe your barcode scheme in `[barcodes]` — or set
   `valid_new_ranges = []` if you don't have one.
4. Scan one shelf (two lines per book: ISBN, then barcode — see
   [docs/OPERATOR-GUIDE.md](docs/OPERATOR-GUIDE.md) for the two-sweep
   method) and run `retrocat shelf` on it.
5. Read `output/<shelf>/master_table.csv`, fill `manual/<shelf>.csv`, and
   when all shelves are done, `retrocat final` builds the one file to
   sandbox-test in your ILS.

The details:

- **Config first.** `[library]` (name, location, status, default MARC
  language), `[barcodes]` (your scheme; empty `valid_new_ranges` disables
  the new-sticker range check), `[catalog.columns]` (map to your export's
  exact headers — validated at load), `[output]`. See the comments in
  `sample/config.toml`.
- **Your collection's subjects.** The class-fallback map
  (`src/retrocat/data/lc_class_map.toml`) that turns vendor subject
  categories into LC classes ships tuned to a religious-studies collection
  as a worked example. Copy it, edit it, and point
  `[lookup].class_map_file` at your copy.
- **The one genuinely ILS-specific surface** is the `852`/`876` holdings
  pair in `marc_build.py` (which subfields carry location/status varies by
  ILS). This mapping is sandbox-validated against exactly one ILS — verify
  it against your importer's documentation, or blank `location`/`status`
  in config to let your ILS apply its own import defaults (the
  pilot-validated path).
- **Tests**: `python -m pytest` — fully offline, includes a golden-file
  integration test over real pilot data and a hermetic end-to-end run of
  the documented two-command flow.

## Limitations, honestly

- **Not fully unattended.** A human fills the manual worklist between the
  two commands — that's by design (those books are unidentifiable online),
  but it is a human step.
- **Multi-copy grouping is structurally tested but has never been confirmed
  in a live import** — the pilot contained no multi-copy book. Confirm one
  in your ILS's sandbox before a full import.
- **Class-fallback call numbers are estimates.** The LC class is inferred
  from vendor subject categories tuned to a religious-studies collection;
  they ship `confidence=low` specifically so you spot-check them.
- **LoC SRU is unreachable from some networks** and self-disables after
  three consecutive connection failures; the run continues on the other
  sources.
- **Validated against exactly one ILS.** See
  [docs/VALIDATION.md](docs/VALIDATION.md) for precisely what was and
  wasn't proven.

## Project docs

- [docs/DESIGN.md](docs/DESIGN.md) — data contracts, classification rules,
  and the edge-case reasoning behind them
- [docs/OPERATOR-GUIDE.md](docs/OPERATOR-GUIDE.md) — the shelf-by-shelf
  capture process for actually running a backfill
- [docs/VALIDATION.md](docs/VALIDATION.md) — what has been validated, and how
- [DECISIONS.md](DECISIONS.md) — design decisions made while generalizing
  this from the original internal tool

## License

MIT.

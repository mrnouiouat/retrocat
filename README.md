# retrocat

[![tests](https://github.com/mrnouiouat/retrocat/actions/workflows/tests.yml/badge.svg)](https://github.com/mrnouiouat/retrocat/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/retrocat)](https://pypi.org/project/retrocat/)
[![Python](https://img.shields.io/pypi/pyversions/retrocat)](https://pypi.org/project/retrocat/)

retrocat turns barcode-scanner output into MARC21 records you can bulk-import
into a library ILS. You scan a shelf (ISBN, then item barcode, two lines per
book), run two commands, and get a `.mrc` file plus a report telling you which
records a human needs to look at.

I wrote it for The Islamic Seminary of America in Richardson, TX. They had
2,000+ books that had never been cataloged and a plan to type them all in by
hand over the next four to six months. Their president approved open-sourcing
the tool afterward, so retrocat is that same pipeline with the
institution-specific parts pulled out into a config file. If you run a small
religious or academic library with no budget for commercial cataloging
software, this is aimed at you.

Python 3.11+, two dependencies (`pymarc` and `requests`), and 285 tests that
run offline in under two seconds.

## What's actually been proven

I'd rather you know the boundaries up front.

The MARC field mapping is **sandbox-validated**. The ILS vendor's support team
loaded a 17-record pilot file into their sandbox and confirmed it produced
correct resources and copies, down to call number, barcode, location, and
status. The specifics are in [docs/VALIDATION.md](docs/VALIDATION.md).

The seminary's backfill itself is underway, not finished. The pilot and the
first shelf are done. What I can tell you about speed is that once a shelf is
physically scanned, turning it into import-ready MARC takes minutes, so the
real cost of the project is shelf-scanning time plus a short worklist of books
no API can identify.

retrocat, the generalized version, has been tested against that deployment. I
set it up from scratch following the "Adapting it to your library" steps below
and ran it on the seminary's actual data: the real shelf scan, the real
2,100-row ILS export, the operator's real hand-filled worklist. It classified
every book identically to the internal tool and produced 30 of 32 identical
call numbers. The two that differed were books whose subject class had to be
guessed because Google Books happened to be down during the run, and both were
flagged by retrocat's own review digest. That's written up in
[docs/VALIDATION.md](docs/VALIDATION.md) under "Reproduction check".

## What it does

1. **Parses scan files** (`scans/*.txt`) with an explicit pairing state
   machine. Lone barcodes (books with no ISBN) go to a manual bucket,
   mispaired scans fail with a file and line number, and every ISBN gets
   checksum-validated on the way in.
2. **Dedupes against your existing catalog**, a CSV export from your ILS with
   the column names mapped in config. All ISBN matching is canonical: ISBN-10s
   become ISBN-13 before anything is compared. The loader skips real-world
   export junk (bad ISBN lengths, call numbers sitting in the barcode column,
   non-book rows) with a log line, and it checks your configured column names
   against the file's actual header before it reads a single row.
3. **Classifies every book** into exactly one of `CREATE`, `MERGE_CANDIDATE`,
   `ALREADY_DONE`, `MANUAL`, or `CONFLICT`, and won't write output unless the
   bucket counts add back up to the scan count. Conflicts block the MARC write
   entirely (`--allow-conflicts` once you've reviewed them), and a blocked run
   deletes any `.mrc` left behind by an earlier one.
4. **Resolves metadata** (title, author, LC call number, LCCN, language) from
   Google Books, OpenLibrary, and the Library of Congress SRU endpoint, with
   response caching and bounded backoff on 429s and 5xxs. Sources fail
   independently, and a transient error is never cached, so a re-run picks up
   whatever a flaky API dropped.
5. **Generates a shelf-able LC call number locally** when no source has one.
   It infers the LC class from vendor subject categories, builds a Cutter
   number from the Library of Congress Cutter table (Shelflisting Manual
   G 63), and appends the year, giving you something like `BP130 .W55 2017`
   with no network call. Every call number carries its source and a
   confidence level.
6. **Builds the MARC21 records** with pymarc: one resource per distinct ISBN,
   one 852/876 pair per barcode, so a book that turns up on two shelves
   imports as one resource with two copies. Each record is round-tripped
   through a MARC reader before it's written.
7. **Prints a review digest** naming the exact barcodes that need human
   attention, so you're not reading a 2,000-row spreadsheet hunting for
   problems.

Step 5 also ships as a standalone command, `retrocat callnumber`, if all you
want is LC call numbers and none of the pipeline.

## The interesting engineering

The bug this whole project is shaped around is ISBN canonicalization. Catalog
exports skew heavily toward ISBN-10; barcode scanners read ISBN-13 EANs.
Compare the raw strings and a book you already own classifies as new, the
duplicate gets imported, and nothing catches it, because the counts still
balance and every checksum is valid. When I fixed this in the original tool,
two books on the pilot shelf quietly moved from CREATE to MERGE_CANDIDATE, which
is to say two duplicates I had been about to ship. Every comparison in
retrocat goes through canonical ISBN-13. The catalog header validation is
there for the same reason: if your export calls the column `isbn13` and your
config says `ISBN`, dedup silently does nothing and the run still looks
perfect.

The scan parser is a state machine with two rules that look inconsistent and
aren't. Two barcodes in a row is fine, because the second one is a book with
no ISBN to scan. Two ISBNs in a row is an error, because a barcode never got
scanned. Both rules came out of watching how scanning actually goes, and both
are pinned by tests with comments explaining why they shouldn't be "fixed".

Cutter generation runs entirely offline, following Shelflisting Manual G 63. I
checked the table against two independent transcriptions, and the anchor test
is `cutter("Wills") == "W55"`, which reproduces a real Library of Congress
call number end to end.

Call numbers are tagged by confidence rather than presented as equally
trustworthy. Numbers from LoC or OpenLibrary are `high`. Ones built from an
inferred subject class are `low` and show up in the review digest. Books whose
subject couldn't be inferred at all get their own weakest tier. And when no
source can produce a title, retrocat doesn't write a record with a placeholder
in it. The book goes on the worklist for a person to identify.

## Requirements

- Python 3.11+
- `pymarc` and `requests`, installed automatically
- Optionally a Google Books API key (see below). Without one the pipeline
  still runs; anonymous Google quota just throttles the category lookups that
  feed the call-number fallback.

## Setup

```bash
pip install retrocat
```

That gets you the `retrocat` command. You also need a `config.toml`, and the
easiest way to get a commented one to edit is to grab the template from this
repo:

```bash
curl -O https://raw.githubusercontent.com/mrnouiouat/retrocat/main/sample/config.toml
```

Optionally put a `GOOGLE_BOOKS_API_KEY` in a `.env` file next to it, which
raises your rate limits.

If you'd rather work from a clone, which also gets you the sample data used
below:

```bash
git clone https://github.com/mrnouiouat/retrocat
cd retrocat
pip install -e .
```

Everything library-specific lives in `config.toml`: your library's name and
holdings values, your barcode scheme (length, plausible range, valid
new-sticker ranges, or no scheme at all), the column names of your catalog
export, and the output filename. It's validated loudly at startup. Unknown
keys and missing columns are hard errors rather than silent defaults.

## Run it on the sample data

`sample/` has a synthetic 58-row catalog export and two shelf scan files. The
ISBNs are real, so they resolve against the live APIs. The whole flow:

```bash
cd sample
retrocat shelf --scan scans/shelf-a.txt --export catalog_export.csv
```

Expected output, give or take whatever the APIs are doing today:

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

Open `manual/shelf-a.csv` and type a title for the lone-barcode book. That's
the human step between the two commands. Then build the combined file:

```bash
retrocat final --scans scans --export catalog_export.csv
```

```
OK: 11 scanned -> 8 MARC records (output/_final/catalog_import.mrc); counts: {'CREATE': 8, 'MERGE_CANDIDATE': 1, 'ALREADY_DONE': 1, 'MANUAL': 1, 'CONFLICT': 0}
Nothing flagged for review.

Manual worklists: 1/1 filled and shipped in the MARC file.
```

`output/_final/catalog_import.mrc` is the file you'd hand to your ILS. The
final file is always built by re-running over all shelves together, never by
concatenating per-shelf `.mrc` files, and that's what makes a book scanned on
two shelves import as one resource with two copies. There's also
`sample/conflict-demo.txt` if you want to watch the conflict gate refuse to
write.

## Sample input and output

A scan file is alternating ISBN/barcode lines, one code per line, which is
exactly what a USB barcode scanner types for you:

```
9781565645998
500101
9780199836741
500102
500115          <- lone barcode: a book with no ISBN, routed to MANUAL
```

`master_table.csv` is the thing you spot-check, one row per scanned book:

```
shelf    barcode  action           title                       call_number          call_number_source
shelf-a  500102   CREATE           Reading the Qur'an          BP130.4 .S376 2011   openlibrary
shelf-a  500104   CREATE           Hitler's American Model     KK4743 .W48 2018     loc
shelf-a  500115   MANUAL           Untitled Local Chapbook     AC .C38              manual
shelf-a  500101   MERGE_CANDIDATE  Abu Zayd al-Balkhi's ...    R128.3 .B34213 2013  openlibrary
```

And here's a decoded MARC record from a live run. It's a merge candidate, so
it carries both ISBN forms, insurance against an ILS merge tool that matches
ISBN strings literally:

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

The offline call-number generator is useful on its own. If you know a book's
LC class and just need a properly shaped, non-colliding shelf number, you can
call it directly with no config and no network:

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
from common org keywords, or forced with `--corporate`), and books with no
author on the first significant title word, skipping leading articles. The
same code imports as a library: `from retrocat.lc_call import cutter,
build_call_number`.

## Adapting it to your library

Five steps from clone to your first shelf:

1. `pip install retrocat` and put a copy of `sample/config.toml` next to your
   data.
2. Export your catalog from your ILS as CSV and put your export's exact column
   headers in `[catalog.columns]`. retrocat aborts with a clear list if they
   don't match the file.
3. Describe your barcode scheme in `[barcodes]`, or set
   `valid_new_ranges = []` if you don't have one.
4. Scan one shelf, two lines per book, ISBN then barcode. See
   [docs/OPERATOR-GUIDE.md](docs/OPERATOR-GUIDE.md) for the two-sweep method
   that keeps that from being tedious. Then run `retrocat shelf` on it.
5. Read `output/<shelf>/master_table.csv`, fill in `manual/<shelf>.csv`, and
   once every shelf is done, `retrocat final` builds the one file to
   sandbox-test in your ILS.

A few things worth knowing before you start:

- **Config first.** `[library]` (name, location, status, default MARC
  language), `[barcodes]` (your scheme; an empty `valid_new_ranges` turns the
  new-sticker range check off), `[catalog.columns]` (your export's exact
  headers, validated at load), `[output]`. The comments in
  `sample/config.toml` walk through each one.
- **Your collection's subjects.** The class-fallback map at
  `src/retrocat/data/lc_class_map.toml` turns vendor subject categories into
  LC classes, and the shipped version is tuned to a religious-studies
  collection as a worked example. Copy it, edit it, and point
  `[lookup].class_map_file` at your copy.
- **The one genuinely ILS-specific surface** is the `852`/`876` holdings pair
  in `marc_build.py`, since which subfields carry location and status varies
  by system. It's sandbox-validated against exactly one ILS, so check it
  against your importer's documentation. You can also blank `location` and
  `status` in config and let your ILS apply its own import defaults, which is
  the path the pilot actually validated.
- **Tests.** `python -m pytest`. Fully offline, including a golden-file
  integration test over real pilot data and a hermetic end-to-end run of the
  two-command flow above.

## Where it falls short

- **It isn't fully unattended.** Somebody has to fill in the manual worklist
  between the two commands. Those are books no API can identify, so there's no
  way around it, but it is a human step.
- **Multi-copy grouping has never been through a live import.** The structure
  is unit-tested and the pilot happened to contain no multi-copy book. Confirm
  one in your ILS's sandbox before you run a full import.
- **Class-fallback call numbers are guesses at the subject level.** The Cutter
  and year are exact transforms of real metadata; the LC class is inferred
  from vendor categories, and Google Books will occasionally insist that a
  book on Islamic theology is Juvenile Fiction. They ship `confidence=low`
  precisely so you spot-check them.
- **LoC SRU is unreachable from some networks.** It self-disables after three
  consecutive connection failures and the run carries on with the other two
  sources.
- **One ILS.** Every sandbox claim here involves a single vendor's importer.
  [docs/VALIDATION.md](docs/VALIDATION.md) is specific about what that does
  and doesn't cover.

## Project docs

- [docs/DESIGN.md](docs/DESIGN.md): data contracts, classification rules, and
  the edge-case reasoning behind them
- [docs/OPERATOR-GUIDE.md](docs/OPERATOR-GUIDE.md): the shelf-by-shelf capture
  process for actually running a backfill
- [docs/VALIDATION.md](docs/VALIDATION.md): what's been validated, and how
- [DECISIONS.md](DECISIONS.md): the design decisions behind the generalized
  version, and why each one went the way it did
- [CHANGELOG.md](CHANGELOG.md): what changed between releases

## License

MIT.

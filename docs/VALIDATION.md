# Validation status: what has actually been proven, and how

An honest inventory of what backs each claim this project makes. "Validated"
here means someone loaded real output into a real system and checked the
result, not "the tests pass."

## Sandbox-validated: the MARC field mapping

A 17-record pilot file built with exactly the field structure `marc_build.py`
emits (leader, `008`, `020`, `100`/`110`, `245`, `050`, and one `852`/`876`
pair per copy) was loaded by an ILS vendor's support team into their sandbox
environment. They confirmed the import produced correct results at both
levels:

- **Resource level:** title and author landed correctly.
- **Copy level:** call number, barcode, home library/location, and status
  landed correctly.

The pilot came from a real 20-book shelf at the small academic library where
this pipeline was first deployed, named in the README with permission. The
byte-for-byte vendor-accepted file remains in that library's internal
repository. What ships here is a structural golden file regenerated from the
pipeline with substituted institution values; `tests/fixtures/README.md` gives
the exact provenance.

## Reproduction check: retrocat against the original internal tool (2026-08-09)

As an adopter dry-run, retrocat was pointed at the source library's *real*
first-shelf data: a 64-line scan file, a roughly 2,100-row ILS export, and the
operator's actual hand-filled worklist. It ran from a fresh directory with a
fresh `config.toml` copied from `sample/config.toml` and edited in four places
(library identity, barcode ranges, column names, output filename), with a cold
lookup cache, live APIs, and Google Books unavailable. Compared row by row
against the original internal pipeline on the same data:

- **Bucket counts and per-barcode classifications were identical**, across 32
  books: 31 CREATE, 1 MANUAL. Record counts were identical too, at 32, all
  round-tripping.
- **30 of 32 call numbers were identical.** The two divergences were the two
  books whose subject class had to be defaulted without Google's category
  data, which are exactly the books the review digest flags as "verify
  shelving" in both runs.
- Remaining diffs, meaning title casing on 18 records and one `008` language
  code, all traced back to source availability. With Google down,
  OpenLibrary's sentence-case titles win the per-field priority, and a
  language signal only Google carried fell back to the configured default.
  Same books, same structure, no classification drift.

One caveat surfaced that's worth stating plainly: **the `008` language signal
often comes only from Google Books**, since LoC misses many small-press
titles. With Google unavailable, non-English books get stamped with the
configured default language. The manual worklist's `language` column is the
hand-fix for books you know are non-English, and a spot-check of `008`s is
worthwhile if Google was down during your run.

## Structurally tested but NOT sandbox-validated

- **Multi-copy grouping**, where the same ISBN on two barcodes produces one
  resource record with two `852`/`876` pairs. The pilot contained no
  multi-copy book, so this path has never been through a live import. The
  structure is unit-tested. Confirm that one multi-copy record imports as one
  resource with two attached copies in *your* ILS's sandbox before a full
  live import.
- **Dual `020` merge insurance**, where a merge candidate emits both the
  scanned ISBN-13 and the export's stored ISBN-10. It's harmless if your ILS
  matches ISBNs canonically, and the second form only matters if it matches
  literally. Untested against any live merge tool.
- **The post-pilot `852 $c` and `876 $j` location/status subfields.** The
  pilot relied on the ILS's import defaults and the explicit subfields were
  added afterward. Verify your ILS reads them, or blank them in config and use
  your ILS's defaults, which is the pilot-validated path.

## Validated against exactly one ILS

Every sandbox claim above involves one vendor's import tool. The `852`/`876`
holdings subfields are the single most ILS-specific surface in MARC holdings
data, since different systems expect different subfield codes for location and
status. Treat `marc_build.py`'s holdings block as a template to verify against
your own importer's documentation rather than as portable truth.

## What deliberately has no validation claim

- No claim that any full production backfill has completed with this tool.
- Class-fallback and default-class call numbers are `confidence=low`
  estimates. The LC subject class is inferred from vendor categories, and only
  the Cutter and year are exact transforms of real metadata. They exist to be
  spot-checked.

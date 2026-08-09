# Validation status — what has actually been proven, and how

Honest inventory of what backs each claim this project makes. "Validated"
here means *someone loaded real output into a real system and checked the
result*, not "the tests pass."

## Sandbox-validated: the MARC field mapping

A 17-record pilot file built with exactly the field structure `marc_build.py`
emits — leader, `008`, `020`, `100`/`110`, `245`, `050`, and one `852`/`876`
pair per copy — was loaded by an ILS vendor's support team into their sandbox
environment. They confirmed the import produced correct results at both
levels:

- **Resource level:** title and author landed correctly.
- **Copy level:** call number, barcode, home library/location, and status
  landed correctly.

The pilot came from a real 20-book shelf at the small academic library where
this pipeline was first deployed (named in the README with permission). The
byte-for-byte vendor-accepted file remains in that library's internal
repository; this repo carries a structural golden file regenerated from the
pipeline with substituted institution values — see `tests/fixtures/README.md`
for the exact provenance.

## Structurally tested but NOT sandbox-validated

- **Multi-copy grouping** (same ISBN on two barcodes → one resource record
  with two `852`/`876` pairs). The pilot contained no multi-copy book, so
  this path has never been through a live import. The structure is
  unit-tested; confirm one multi-copy record imports as one resource with
  two attached copies in *your* ILS's sandbox before a full live import.
- **Dual `020` merge insurance** (a merge candidate emits both the scanned
  ISBN-13 and the export's stored ISBN-10). Harmless if your ILS matches
  ISBNs canonically; the second form only matters if it matches literally.
  Untested against any live merge tool.
- **The post-pilot `852 $c` / `876 $j` location/status subfields.** The
  pilot relied on the ILS's import defaults; the explicit subfields were
  added afterward. Verify your ILS reads them (or blank them in config and
  use your ILS's defaults, which is the pilot-validated path).

## Validated against exactly one ILS

Every sandbox claim above involves one vendor's import tool. The `852`/`876`
holdings subfields are the single most ILS-specific surface in MARC
holdings data — different systems expect different subfield codes for
location and status. Treat `marc_build.py`'s holdings block as a template to
verify against your own importer's documentation, not as portable truth.

## What deliberately has no validation claim

- No claim that any full production backfill has completed with this tool.
- Class-fallback and default-class call numbers are `confidence=low`
  *estimates* (the LC subject class is inferred from vendor categories; only
  the Cutter and year are exact transforms of real metadata). They exist to
  be spot-checked, not trusted.

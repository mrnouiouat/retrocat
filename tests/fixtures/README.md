# Fixtures — ported pilot data, honestly labeled

These fixtures descend from a real 20-book pilot run at the library where
this pipeline was first deployed. **What is real:** the ISBNs, titles,
authors, and resolved call numbers — public bibliographic facts, kept
deliberately so the lookup chain demonstrably works against live APIs.
**What is substituted:** barcodes (renumbered into the generic `5001xx`
scheme), the institution's name and holdings values, and the catalog export
(a 4-row synthetic file shaped like the real one).

**Provenance of the MARC mapping.** The original pilot file built with this
exact field structure was loaded by the ILS vendor's support team into their
sandbox and confirmed correct at the copy level (call number, barcode,
location) and the resource level (title, author). `pilot_golden_output.mrc`
here is NOT that vendor-accepted file — after renumbering it cannot be. It
is a **structural golden file**: regenerated from the pipeline itself
(`python scripts/regen_golden.py`) with pinned inputs, pinning the validated
*field mapping* byte-for-byte against regressions. The behavioral claims the
mapping rests on are asserted explicitly in `test_integration_golden.py`,
so the golden file cannot silently decay into a self-fulfilling snapshot.

## Files

- `pilot_scan_input.txt` — 20 books in the paired scan format (ISBN line,
  then barcode line). Barcodes 500148–500167.
- `pilot_expected_output.csv` — what the pipeline should classify each book
  as (CREATE / MERGE_CANDIDATE / MANUAL) and what title/author/call number
  the lookup chain resolved during the real pilot. Doubles as the offline
  lookup stub's data source.
- `pilot_catalog_export.csv` — synthetic 4-row catalog export holding the
  four merge-candidate ISBNs, three of them stored in ISBN-10 form (that is
  the point — see below).
- `pilot_golden_output.mrc` — the structural golden file, 19 records.

## Known cases baked in (intentional — do not "fix" them)

- Barcodes **500149, 500150, 500152, 500155** → MERGE_CANDIDATE. The export
  stores 500150's and 500152's ISBNs **only in ISBN-10 form** (`1933633085`,
  `0312156480`); a raw string comparison misses them and silently duplicates
  the resource, which is precisely the bug canonical ISBN-13 comparison
  exists to kill. The original pilot, which predated canonicalization,
  misclassified these two as CREATE.
- Barcode **500165** → MANUAL. ISBN 9781515129158 never resolved a title
  from any source — a real self-published/print-on-demand edge case, not a
  bug. It must never become a placeholder CREATE.
- Barcode **500157** → CREATE with **no call number**. Google Books,
  OpenLibrary and LoC all came up empty during the pilot, and the target
  ILS autofilled it on import. The record ships with no `050` and no `$h`
  — do not fabricate one. (In the live pipeline the class-fallback would
  now generate an estimate; the stub reproduces the pilot's misses.)
- Two merge rows in `pilot_catalog_export.csv` carry junk call numbers
  (`2106`, `E2-10` — shapes really observed in a production export), so the
  reconcile report must flag them `needs_fix`.

## Caveat

The pilot was captured before the paired-scan format was finalized, so the
barcode-per-book is a sequential assignment, not a live physical scan. It
is valid for testing parsing, classification, and MARC building; it is not
proof that a real shelf scan pairs identically.

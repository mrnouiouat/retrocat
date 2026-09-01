# Validation status

This document separates what retrocat has done in a real library from what an
adopter still needs to verify in a new system. Here, "validated" means that
the pipeline produced MARC output, that output was imported into an ILS, and
the resulting catalog was checked. It does not just mean that the tests pass.

## Completed production deployment

retrocat was used end to end to complete a clean **2,227-book catalog import**
for the nonprofit seminary where the project began.

The production workflow included:

- Capturing the physical collection through paired ISBN and item-barcode scans
- Reconciling those scans against the library's existing ILS export
- Separating new resources, merge candidates, completed items, manual records,
  and conflicts
- Resolving bibliographic metadata through the configured public sources
- Reusing, correcting, or generating LC call numbers as the data required
- Completing the manual worklists for books the public sources could not
  identify
- Building the combined MARC21 file and importing it into the production ILS

This was the full backfill, not a sample run. It brought the physical shelves
and the online catalog back into sync after students had been unable to rely on
the catalog to tell them whether the library owned a title, where it was, or
whether a copy was available.

## The data was not clean before the import

The source catalog was useful, but it could not be treated as a pristine source
of truth. The real collection included uncataloged books, missing or incorrect
LC call numbers, mismatched barcodes, inconsistent ISBN forms, and records that
did not reliably describe the copies on the shelves.

Those conditions shaped the pipeline's safety checks. ISBN comparison is
canonical, configured CSV headers are validated before any rows are read,
barcode disagreements become conflicts, and each scan must land in exactly one
classification bucket. Records that cannot be completed safely go to a manual
worklist instead of receiving placeholder metadata.

The completed deployment demonstrates that this reconciliation model can be
used at full-collection scale, including the human review step between shelf
triage and the final build.

## What the completed import establishes

For the ILS used by the source library, retrocat successfully produced and
imported the resource- and copy-level data needed for the completed catalog.
That includes bibliographic records, call numbers, item barcodes, and holdings
information used to reconnect physical copies with the online catalog.

It also establishes that the operational workflow is practical: shelves can be
processed independently, worklist edits survive reruns, and the final command
can rebuild one combined import across the complete set of scan files.

## Automated verification

The repository has 285 offline tests, including parser and classification edge
cases, ISBN normalization, catalog-header validation, lookup failure handling,
manual-worklist preservation, call-number generation, MARC round trips, a
golden-file integration test, and the two-command sample workflow.

CI runs the suite on Python 3.11, 3.12, and 3.13. HTTP behavior is mocked, so a
test cannot pass or fail because a public metadata service happens to be down.

The golden fixtures preserve data from the project's earlier validation work.
They are regression fixtures, not the basis of the production-completion claim
above.

## What a new library still needs to verify

The completed deployment involved one ILS. MARC21 is portable, but import rules
for holdings, merging, location, and status are not identical across vendors.
Every adopter should load a representative final file into the target system's
sandbox and check:

- `852` and `876` holdings fields, including location and status
- ISBN merge behavior, including ISBN-10 and ISBN-13 forms
- One title with multiple physical barcodes
- At least one manually completed record
- The treatment of existing copy-level call numbers during a resource merge

Low-confidence call numbers also remain estimates. The Cutter and year are
deterministic transforms of the available metadata, but a subject class inferred
from vendor categories needs a librarian's spot-check.

Language needs similar care when metadata coverage is incomplete. If no source
reports a language, retrocat uses the configured default; the manual worklist's
`language` column can override it for a known title.

The production import proves that retrocat completed the job it was built for.
It does not remove the need to verify another ILS's import behavior before
using it on that system.

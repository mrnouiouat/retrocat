# Design decisions

retrocat is a clean-room generalization of an internal tool I built for one
library's retrospective conversion. Every institution-specific fact in that
tool had to become either a config value, a shipped-and-overridable data file,
or nothing at all. This is the record of the calls I made doing that, and why,
so nobody (including me, later) has to re-derive them from the diff.

Organized by area rather than chronologically. Everything here landed
2026-08-08 and 2026-08-09.

## Scope and packaging

**Version starts at 0.1.0.** The internal tool called itself 1.0.0, but that
number described *its* maturity against one library's data. The generalized
package hasn't earned it yet, and I'd rather under-claim in metadata that
people read as a promise.

**`retrocat/data/*.toml` ships as package data.** The class-fallback subject
map is a data file rather than a Python constant, so a library can override it
without touching source. That decision shaped `pyproject.toml` before the map
itself existed.

**The LICENSE copyright holder is a GitHub handle rather than a legal name.**
That's a deliberate choice, not an oversight. What actually mattered was that
the holder match the repo owner: for a while the LICENSE said `thefirstsamurai`
while the account had been renamed to `mrnouiouat`, so a reader opening the
LICENSE saw a copyright holder who wasn't the person publishing the code. On a
project whose pitch is provenance, that reads badly. The handle is now
consistent across the LICENSE, the clone URL, and the git remote.

## Configuration

The theme across all of these: misconfiguration has to be loud. The failure
mode this project exists to prevent is a run that *looks* clean and quietly
does the wrong thing.

**Unknown config keys are hard errors, not warnings.** A typo like
`home_libary` reverting to a default is the same class of bug as a renamed
export column silently disabling dedup. Neither one shows up in any count.

**Catalog column names are validated against the real header row** before a
single row is read, and a mismatch aborts with both the configured names and
the actual header printed side by side. If your export calls the column
`isbn13` and your config says `ISBN`, dedup matches nothing, every book
classifies as new, and the totals still balance perfectly. There is no way to
catch that downstream, so it gets caught at load.

**`[catalog.columns].isbn` and `.barcode` are required and non-empty.**
`title`, `author`, and `call_number` fall back to conventional names.
`resource_id` and `type` default to empty, meaning "my export doesn't have
this column." An empty `call_number` sets `ExistingCatalog.has_call_numbers =
False`, and `reconcile.py` then writes an explicit "(export has no
call-number column)" in every row rather than a blank cell that would read as
"this book has no call number on record."

**A barcode length of 10 or 13 is rejected at config load.** Those lengths
make a scan line ambiguous against ISBN classification, and the parser's whole
contract is that a token's type is decidable from the token. Any other length
is fine.

**`valid_new_ranges = []` disables the new-sticker range check entirely.** The
internal tool hardcoded one library's sticker-roll constants. Plenty of small
libraries have no barcode scheme at all, so the check had to be switchable off
rather than merely configurable. `[barcodes].min`/`max` are separately
optional; absent means any all-digit token of the configured length counts.

**`marc_language` is length-checked twice**, at config load and again at MARC
build time. A wrong-length language code shifts every position after 35 in the
fixed-length `008` field and corrupts it silently, so both ends stay paranoid
about it.

## Classification and parsing

**I kept a branch the original spec called unreachable.** The spec said
"barcode on record but the record has no ISBN → ALREADY_DONE" could never
fire. Against the actual code it fires readily: `catalog.py` maps a barcode to
an *empty* ISBN set whenever an export row has a barcode but no usable ISBN,
which is common in messy exports, and `classify._classify_pair` then takes the
`if not on_record:` branch. There's a test proving it
(`test_barcode_on_record_but_record_has_no_isbn`). When the code and the spec
disagree about what's reachable, the code wins and the spec gets amended.

**Conflict-range text comes from config.** The internal tool's conflict note
quoted its own hardcoded sticker ranges. `BarcodeConfig.describe_ranges` now
generates that sentence from whatever the adopting library configured, tested
in `test_conflict_note_carries_configured_ranges`.

## Lookup and the class map

**The class-fallback map is `src/retrocat/data/lc_class_map.toml`**, loaded
with `tomllib`, which preserves document order. Order is semantic here: first
keyword found wins, so narrower keys have to be written before words that
contain them. The file also carries `default_class`, the last-resort LC class
that used to be a `DEFAULT_LC_CLASS` constant, which puts the entire "where
does an unclassifiable book go" policy in one overridable file.
`[lookup].class_map_file` points at your copy.

The shipped map stays tuned to a religious-studies collection rather than being
neutered into something generic. A worked example that visibly encodes one
collection's subject profile teaches an adopter what to do with theirs; an
empty template teaches nothing.

**`class_fallback` is module-level**, taking the class map as a parameter, so
tests and the standalone tool can call it without constructing a lookup client.

**`language.py` no longer owns a default language.** `DEFAULT_LANGUAGE` was
deleted and `marc_build._field_008` takes the default from
`[library].marc_language`. A default language is a property of a library's
collection, not of a code module.

## MARC build

**I dropped the `include_location_status` boolean.** The `852 $c` and `876 $j`
subfields are now emitted if and only if the corresponding config values are
non-empty, so "blank config means let the ILS apply its own defaults" replaces
a flag that existed only to reproduce the pilot's exact shape. One rule, no
mode.

## Sample data and fixtures

**`sample/catalog_export.csv` mixes real bibliographic facts with synthetic
everything-else.** Titles, authors, and ISBNs are real books, which matters
because the sample scans have to resolve against live APIs for the demo to be
honest. Resource IDs, barcodes (500xxx), the library name, and the junk-row
values are invented. 33 of 58 ISBN cells are stored in ISBN-10 form, so
canonicalization is demonstrated by the sample rather than merely asserted in
prose. All the junk cases an adopter will actually hit are present: an
invalid-length ISBN, a call-number-shaped value in the barcode cell, a
multi-value `;` ISBN cell, a barcode-shaped call number, a row with no ISBN,
and two non-book rows.

**The two sample shelves cover the interesting paths** between them:
ALREADY_DONE via ISBN-10-vs-EAN canonical agreement, a MERGE_CANDIDATE with
dual `020`s, plain CREATEs, the same ISBN twice on one shelf, the same ISBN
across two shelves, and a lone barcode.

**The CONFLICT case lives in its own `sample/conflict-demo.txt`.** A conflict
inside `shelf-a` would block that shelf's `.mrc` by design, which is correct
behavior and a terrible first-run experience.

**The golden `.mrc` is regenerated from the pipeline and compared
byte-for-byte.** The internal repo compared its golden file with a list of
tolerated deltas, but only because its reference file predated a set of rule
amendments. Starting clean, there's no reason to carry that tolerance, and
byte equality is both simpler and stricter. Field-level behavioral assertions
run alongside it so the golden file can't decay into a snapshot that merely
agrees with whatever the code currently does. `scripts/regen_golden.py`
regenerates it deliberately.

The golden file keeps the pilot's real ISBNs, titles, and authors, with
barcodes renumbered to 500148–500167 and the catalog dependency replaced by a
4-row synthetic export holding exactly the merge-candidate ISBNs, two of them
in ISBN-10-only form. That reproduces the canonicalization reclassification
that motivated the whole design.

**The vendor-accepted bytes are not in this repo.** What's here is a
structurally identical file regenerated from the pipeline.
`tests/fixtures/README.md` and `docs/VALIDATION.md` both say so plainly rather
than letting a reader assume the golden file is the artifact the vendor
loaded.

## A bug I found while porting

**`unfilled_manual.csv` listed books that weren't unfilled.** The internal tool
wrote every book in the MANUAL bucket to the final run's leftover report,
including ones already resolved from a filled worklist, which showed up as
blank rows. That contradicts its own spec ("unfilled rows are listed in
unfilled_manual.csv") and it makes the report actively misleading at the exact
moment an operator is using it to decide whether they're done.
`_write_manual_worklist` now takes a set of resolved barcodes and skips them.
Shelf runs are unaffected, since they pass no filled entries. Fixed here and
backported to the internal tool.

## The standalone call-number tool

**`retrocat callnumber` is a subcommand, not a second console script.** As a
subcommand it's discoverable from `retrocat --help` and there's exactly one
entry point to document. It sets `needs_config=False`, so unlike the pipeline
commands it needs no `config.toml`, no network, and no data files.

Three modes: single-shot (`--lc-class/--author/--title/--year/--corporate`),
`--cutter WORD`, and `--batch FILE.csv`, which appends a `call_number` column
and writes to stdout.

**Batch rows with no `lc_class` get a blank call number and a warning.**
Inferring a subject class is the pipeline's job, because the pipeline has
vendor category signals to work from. The standalone tool has nothing to infer
from, so it declines rather than guessing.

`lc_call.py` itself is untouched by all of this. The tool is a CLI wrapper, so
pipeline and standalone share one implementation of the Cutter algorithm.

## Packaging and releases

**Distribution is via PyPI, with the clone as the secondary path.** The stated
audience is small-library staff with no budget for cataloging software, and
for that reader `git clone && pip install -e .` is a real barrier rather than
a formality. `pip install retrocat` is the version of this project they can
actually try.

**A changelog and a design log are different documents.** For one release the
PyPI `Changelog` URL pointed at `DECISIONS.md`, which is this file: reasoning
about why the pipeline works the way it does, with no notion of what changed
between versions. Someone clicking "Changelog" wants a release history. They
are now separate URLs, with `CHANGELOG.md` as the changelog and this file
listed as design notes. PyPI metadata is immutable per version, so correcting
a mislabeled URL costs a release; 0.1.1 exists mostly for that.

**Releases publish from CI via Trusted Publishing (OIDC), not from a laptop.**
0.1.0 went up with a long-lived API token through twine, which means the token
existed on a developer machine and in that machine's shell history. Trusted
Publishing removes it: PyPI is configured to trust this repository and the
`release.yml` workflow, and GitHub mints a short-lived identity token per run.
A release is now `git tag && git push`, and uploads carry sigstore
attestations as a side effect.

**The release workflow verifies the packaged data file before publishing.**
`data/lc_class_map.toml` is loaded through `importlib.resources`, so if it
ever falls out of a built artifact, every fresh install breaks at runtime
while a source checkout keeps working perfectly. That's a bug you cannot
notice locally, and PyPI versions can't be replaced once uploaded, so the
workflow asserts the file is present in both the wheel and the sdist and
smoke-tests the built wheel in a clean virtualenv with no source tree on the
path.

## Verification

**The README's demo output is from a real run**, not composed. I ran the CLI
over `sample/` from a scratch directory, shelf through worklist through final,
and quoted what came out. Google Books was unavailable during that run and
OpenLibrary and LoC resolved every book anyway, including one call number the
other two lacked, which is an unplanned live demonstration of the per-source
degradation. I omitted Google's failure warnings from the quoted output
because they came from a stale environment artifact on my machine rather than
anything a stranger would see.

**The adopter dry-run used the source library's real data.** Fresh directory,
real 64-line shelf scan and real 2,100-row export copied in (never into this
repo), `config.toml` copied from the sample and edited in four places, cold
cache, live APIs. Full flow, including the operator's real hand-entered
worklist title surviving the merge. Then diffed row by row against the
internal pipeline on the same data: identical classifications, 30 of 32
identical call numbers, every divergence traceable to Google being down.
Details in `docs/VALIDATION.md` under "Reproduction check."

That run surfaced a real caveat worth repeating here: **the `008` language
signal often comes only from Google Books**, since LoC misses many small-press
titles. A Google-down run stamps the configured default language on
non-English books. The worklist's `language` column is the hand-fix, and it's
why that column exists.

## Open items

- Multi-copy grouping is structurally tested but has never been through a live
  import, because the pilot contained no multi-copy book. Someone needs to
  confirm one in an ILS sandbox before a full production import.
- The dual-`020` merge insurance is untested against any live merge tool. It's
  harmless if your ILS matches ISBNs canonically and only matters if it
  matches them literally.
- The class-fallback map is tuned to a religious-studies collection. It works
  as a documented worked example, but retrocat has never been run against a
  collection with a genuinely different subject profile, so how much editing
  that actually takes for an adopter is unmeasured.

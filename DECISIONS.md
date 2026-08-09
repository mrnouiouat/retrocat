# DECISIONS.md — design calls made while building retrocat

Running log of design decisions and deviations from `RETROCAT-HANDOFF.md`
(which lives in the source repo, not here). Newest entries at the bottom of
each phase section. Kept per the handoff's Authority section: this is the
record the owner reads instead of re-deriving the reasoning.

## Phase 1 — skeleton (2026-08-08)

- **Cloned the existing GitHub repo** rather than `git init`, per the amended
  handoff. Branch `main`; the placeholder 35-byte `README.md` stays until
  Phase 8 overwrites it.
- **LICENSE copyright holder is the GitHub handle `thefirstsamurai`**, not a
  legal name — no confirmed real name was available to the build session, and
  guessing one from an email address seemed worse than the handle. Swap in a
  real name whenever; one-line change.
- `.gitignore` carried over from the source repo verbatim (the handoff calls
  it correct). `output/` and `.cache/` stay ignored for the same reasons as
  the source repo: regenerable vs. precious-but-local.
- `pyproject.toml` includes a `[tool.setuptools.package-data]` entry for
  `retrocat/data/*.toml` ahead of Phase 5, where the class-fallback subject
  mapping becomes a shipped, overridable data file.
- Version starts at `0.1.0` (the source repo says `1.0.0`, but that number
  described the *internal* tool's maturity; the generalized package has not
  yet earned it).

## Phase 2 — isbn.py and lc_call.py (2026-08-08)

- Ported verbatim except import paths and the `isbn.py` docstring, which
  referenced the internal spec file and the internal export's ISBN-10 ratio;
  now phrased generically (the engineering claim is unchanged).
- **ISBN tests got their own `tests/test_isbn.py`.** In the source repo they
  live inside `test_catalog.py`; splitting them means Phase 2 lands with a
  green suite before `catalog.py` exists. Added a small `extract_isbns` test
  class (the function was only covered indirectly before). The catalog-loading
  tests move over with `catalog.py` in Phase 4.
- One fixture string in `test_lc_call.py` replaced: a real organization name
  used to exercise leading-article skipping became "The Riverside Historical
  Society" — it was test data, not a bibliographic fact, so the keep-real-data
  exception did not apply. Real corporate *authors* (e.g. El-Falah Foundation)
  stay, per the locked fixtures decision.
- 56 tests passing at this checkpoint.

## Phases 3+4 — config.py, parse_scans.py, catalog.py, classify.py (2026-08-08)

### config.py design calls

- **Unknown config keys are hard errors**, not warnings. A typo like
  `home_libary` silently reverting to a default is the same failure class as
  a renamed export column silently disabling dedup — the project's whole
  posture is that misconfiguration must be loud.
- **Barcode length 10 or 13 is rejected at load** — it would make scan lines
  ambiguous against ISBN classification. Any other length works.
- `[barcodes].min`/`max` are optional (absent = any all-digit token of the
  configured length is a barcode). `valid_new_ranges` is a list of `[lo, hi]`
  pairs; **empty list = collision-range check disabled entirely**, per the
  handoff's requirement that a library with no barcode scheme can switch it
  off. The check lives on `BarcodeConfig.is_valid_new_barcode`, replacing the
  source repo's `barcode_in_valid_new_range` + hardcoded sticker-roll
  constants (not ported, as instructed).
- `[catalog.columns].isbn` and `.barcode` are required non-empty; `title`,
  `author`, `call_number` default to their conventional names; `resource_id`
  and `type` default to `""` (= my export doesn't have this). An empty
  `call_number` sets `ExistingCatalog.has_call_numbers = False`, which
  `reconcile.py` will surface explicitly in Phase 5 instead of emitting
  silent blanks.
- `marc_language` is length-checked at config load (3 chars) *and* will be
  re-checked at MARC build time — a wrong-length code corrupts the fixed-
  length 008 field silently, so both ends stay paranoid.
- `--config` (Phase 6) will default to `./config.toml`; the missing-file
  error points at `sample/config.toml`.

### Port deviations

- **Kept the "barcode on record but record has no ISBN → ALREADY_DONE"
  branch that the handoff calls unreachable.** Against the current code it
  IS reachable: `catalog.py` maps a barcode to an *empty* ISBN set whenever
  an export row has a barcode but no usable ISBN (common in messy exports),
  and `classify._classify_pair` then takes the `if not on_record:` branch.
  There is a test proving it (`test_barcode_on_record_but_record_has_no_isbn`).
  Flagging per the handoff's "the code wins" rule rather than silently
  deviating; nothing was dropped.
- **Catalog header validation added** as specified: every configured column
  name is checked against the real header row before any row is read;
  missing columns abort listing both the configured names and the actual
  header. Covers the `Type` filter the same way (configured-but-absent
  aborts; unconfigured = filter off, documented as "my export contains only
  books").
- Ported tests replace the source repo's real sticker-range values with a
  generic 5xxxxx scheme (institution range facts are institution data, even
  in test constants). The classify tests now also cover: range note built
  from config, range check disabled, blank type value kept, type filter off.
- `test_config.py` loads `sample/config.toml` in the suite, so the shipped
  sample can never drift into invalidity.
- 140 tests passing at this checkpoint.

## Phase 5 — lookup, language, marc_build, reconcile, manual (2026-08-08)

- **The class-fallback map ships as `src/retrocat/data/lc_class_map.toml`**
  (packaged via `[tool.setuptools.package-data]`), loaded with `tomllib`,
  which preserves document order — order is semantic (first keyword found
  wins; narrower keys are written before words they contain). The file also
  carries `default_class` (the last-resort LC class, formerly the
  `DEFAULT_LC_CLASS` constant), so the whole "where does an unclassifiable
  book go" policy lives in one overridable file. Override hook:
  `[lookup].class_map_file` in config.toml. The shipped map keeps the
  religious-studies Tier 1 as a documented worked example, per the handoff.
- `_class_fallback` became the module-level `class_fallback(class_map, ...)`
  so tests and future tools can use it without a client.
- **`language.py` no longer owns a default language.** `DEFAULT_LANGUAGE`
  was deleted; `marc_build._field_008` takes the default from
  `[library].marc_language` per the backport instruction. Both config load
  and MARC build validate the 3-character shape.
- **`marc_build` dropped the `include_location_status` flag.** Location and
  status subfields (852 $c / 876 $j) are emitted iff the config values are
  non-empty — "blank config = let the ILS default" replaces a boolean that
  existed to reproduce the pilot's shape. The golden fixture regeneration in
  Phase 7 will use blank location/status for the pilot-shaped records.
- `reconcile.py`: when the export has no call-number column, every row's
  `existing_call_number` reads `"(export has no call-number column)"` and
  `needs_fix` stays False — an explicit statement instead of a blank that
  would read as "this book has no call number on record". A warning is
  logged once per run.
- `manual.py` takes `default_lc_class` as an explicit parameter (the
  pipeline passes the loaded class map's default) rather than importing a
  constant.
- Vendor/institution names stripped from comments while keeping the
  technical reasoning (dual-020 insurance, sandbox validation, LoC
  unreachability) exactly as the handoff prescribed.
- `test_manual.py`'s end-to-end section (which drives `run_pipeline`) is
  deferred to Phase 6 with the pipeline itself; everything else ported.
- 243 tests passing at this checkpoint.

## Phase 6 — pipeline.py and __main__.py (2026-08-08)

- Straight port as amended: `_conflict_gate`, `--allow-conflicts`,
  stale-`.mrc` removal, and both gate test classes carried over intact. The
  only behavior change is the prescribed one — conflict range text comes
  from config (`BarcodeConfig.describe_ranges`), tested in
  `test_conflict_note_carries_configured_ranges`.
- `run_pipeline` takes a required `config: Config` and threads it everywhere;
  `mrc_name=None` now means "use `[output].mrc_filename`". The class map is
  loaded once per run (respecting `[lookup].class_map_file`) and feeds both
  the lookup client and the manual-entry default class.
- CLI: `--config` on both subcommands, default `./config.toml`; `ConfigError`
  and the new `CatalogError` (header validation) exit 1 with a clean message
  like the other expected failures. The `final` subcommand's output filename
  comes from config instead of a hardcoded institution name.
- The source repo's `test_manual.py` end-to-end section became
  `tests/test_pipeline_manual.py` (it drives `run_pipeline`, so it belongs
  with the pipeline tests, and keeps `test_manual.py` import-light).
- 258 tests passing at this checkpoint.

## Phase 7 — sample data, golden fixture, hermetic e2e (2026-08-09)

- **`sample/catalog_export.csv`** (58 rows): title/author/ISBN trios are real
  bibliographic facts drawn from the source library's export (allowed per
  the locked fixtures decision); resource IDs, barcodes (5000xx), home
  library ("Anytown College Library"), and junk-row values are synthetic.
  33/58 ISBN cells stored in ISBN-10 form so canonicalization is
  demonstrated, not claimed. All the specified junk cases are present:
  invalid-length ISBN, call-number-shaped barcode cell, multi-value `;`
  ISBN cell, a barcode-like call number, a no-ISBN row, and two non-Book
  rows. Generator script kept in the session scratchpad only (one-shot,
  reads the private export — must not enter this repo).
- **`sample/scans/`** (shelf-a, shelf-b) exercises ALREADY_DONE (ISBN-10 vs
  EAN canonical agreement), MERGE_CANDIDATE with dual 020, CREATE,
  same-ISBN-twice multi-copy, cross-shelf multi-copy (9780199836741 on both
  shelves), and a lone barcode. The CONFLICT case lives in a separate
  `sample/conflict-demo.txt` — a conflict inside shelf-a would block its
  `.mrc` by design and break the 60-second happy-path demo.
- **Golden fixture**: pilot ISBNs/titles/authors kept; barcodes renumbered
  500148–500167; the export dependency replaced with a 4-row synthetic
  `pilot_catalog_export.csv` holding exactly the merge-candidate ISBNs
  (two in ISBN-10-only form, reproducing the canonicalization
  reclassification the source repo documents as deltas). The `.mrc` is
  regenerated from the pipeline (`scripts/regen_golden.py`) and compared
  **byte-for-byte** — simpler and stricter than the source repo's
  delta-tolerant comparison, which existed only because its reference file
  predated the amended rules. Field-level behavioral assertions ride
  alongside so the golden file can't decay into a self-fulfilling snapshot.
  `tests/fixtures/README.md` rewritten honestly (structural golden file,
  not the vendor-accepted bytes); `docs/VALIDATION.md` records what was
  sandbox-validated without names/URLs/dates.
- **Deviation (bug fix): `unfilled_manual.csv` now lists only genuinely
  unfilled books.** The source repo writes every MANUAL-bucket book to the
  final run's leftover report, including ones already resolved from a
  filled worklist (as blank rows) — contradicting its own spec ("Unfilled
  rows ... are listed in unfilled_manual.csv"). `_write_manual_worklist`
  takes a `resolved` barcode set and skips them. Shelf runs are unaffected
  (they pass no filled entries). Worth backporting to the source repo.
- CLI tests (`test_cli_sample.py`) drive the real `main(argv)` over a copy
  of `sample/` with `pipeline.LookupClient` monkeypatched — the documented
  two-command flow (shelf → fill worklist → final), the conflict-gate exit
  code, `--allow-conflicts`, config errors, and the header-validation abort.
- 272 tests passing at this checkpoint.

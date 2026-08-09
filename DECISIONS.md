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

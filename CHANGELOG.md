# Changelog

Notable changes per release. Format follows [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html).

Design reasoning lives in [DECISIONS.md](DECISIONS.md), which explains *why*
the pipeline works the way it does. This file only records what changed
between releases.

## [0.1.3] - 2026-08-09

### Changed

- Removed em dashes from every message the tool writes, including the seeded
  `notes` text in `manual/<shelf>.csv` and `master_table.csv`. Those files are
  opened in Excel by operators, and the note now reads "no ISBN scanned; look
  up manually by physical inspection". A few validation errors that a comma
  would have turned into run-on sentences were repunctuated rather than
  mechanically converted. No behavior changed; the strings are wording only.
- The README demo GIF was re-recorded against this build so the worklist it
  shows matches what the tool now writes.

## [0.1.2] - 2026-08-09

### Fixed

- Every documentation link in the README was repo-relative. That works on
  GitHub, but the README is also the PyPI long description, where a relative
  target resolves against `pypi.org/project/retrocat/` and 404s. Anyone
  arriving from `pip install retrocat` found all nine pointers dead, including
  all four references to `docs/VALIDATION.md`, which is where the evidence for
  the README's claims actually lives. All documentation links are now absolute
  URLs.

## [0.1.1] - 2026-08-09

Packaging and metadata only. No change to pipeline behavior, and the 285
tests are unchanged.

### Added

- A real `CHANGELOG.md`, which is what the `Changelog` project URL should
  have pointed at all along.
- Tag-triggered publishing via GitHub Actions using PyPI Trusted Publishing
  (OIDC). Releases no longer require a long-lived API token on a developer
  machine. The workflow also verifies that the packaged
  `data/lc_class_map.toml` is present in both the wheel and the sdist before
  it publishes, since a missing data file would break every fresh install
  while working fine from a source checkout.

### Fixed

- The `Changelog` project URL on PyPI pointed at `DECISIONS.md`, which is a
  design log rather than a release history. It now points at this file, and
  `DECISIONS.md` is listed separately under "Design notes."

## [0.1.0] - 2026-08-09

First public release.

### Added

- Scan file parsing with an explicit pairing state machine, checksum-validated
  ISBNs, and hard errors that name the file and line.
- Deduplication against an existing catalog export, with every ISBN comparison
  performed on canonical ISBN-13 so a 13-digit EAN scan matches a catalog row
  stored in 10-digit form.
- Validation of configured column names against the export's real header row.
- Classification into `CREATE`, `MERGE_CANDIDATE`, `ALREADY_DONE`, `MANUAL`,
  and `CONFLICT`, with a reconciliation gate and a conflict gate that block
  the MARC write.
- Metadata resolution from Google Books, OpenLibrary, and Library of Congress
  SRU, with response caching, bounded backoff, an ISBN-10 retry, and
  independent per-source failure.
- Offline LC call number generation using the Cutter table from Shelflisting
  Manual G 63, with source and confidence tagging on every call number.
- MARC21 output via pymarc, one resource per distinct ISBN with one 852/876
  pair per copy, round-trip validated before writing.
- A manual worklist round-trip that preserves hand-entered rows across
  re-scans.
- `retrocat callnumber`, a standalone offline call-number and Cutter
  generator that needs no config and no network.
- Configuration through `config.toml`, covering library identity, barcode
  scheme, catalog column mapping, and output filename.

[0.1.3]: https://github.com/mrnouiouat/retrocat/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/mrnouiouat/retrocat/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/mrnouiouat/retrocat/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/mrnouiouat/retrocat/releases/tag/v0.1.0

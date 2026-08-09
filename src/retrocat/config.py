"""Configuration loading: one TOML file describes the library, its barcode
scheme, the catalog export's column names, and output naming.

Design rules:

* **Fail fast at load time.** A missing `home_library`, a barcode length that
  collides with ISBN classification, or an unknown key (probable typo) all
  abort before any file is read. A config typo that silently reverts to a
  default is the same failure class as a renamed export column silently
  disabling dedup — the whole point of this layer is that misconfiguration
  is loud.
* **The barcode range check is optional.** A library with no sticker-number
  scheme leaves `valid_new_ranges` empty and the new-barcode collision check
  switches off entirely; nothing hard-codes any particular range.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_CONFIG_FILENAME = "config.toml"


class ConfigError(Exception):
    """Bad or missing configuration — always fatal at startup."""


@dataclass(frozen=True)
class LibraryConfig:
    """Values stamped into every MARC record for this library."""

    home_library: str = ""  # required; validated non-empty in load_config
    location: str = ""
    status: str = ""
    marc_language: str = "eng"  # MARC 008/35-37 default when no source knows


@dataclass(frozen=True)
class BarcodeConfig:
    """The library's item-barcode scheme, used by the scan-token classifier
    and the new-sticker collision check."""

    length: int = 6
    min: int | None = None  # inclusive; None = any all-digit token of `length`
    max: int | None = None
    # Ranges a NEW sticker may legitimately carry, as (lo, hi) inclusive
    # pairs. Empty = the collision-range check is disabled entirely.
    valid_new_ranges: tuple[tuple[int, int], ...] = ()

    def is_valid_new_barcode(self, barcode: str) -> bool:
        """True if a barcode is a legitimate number for a NEW sticker.

        With no configured ranges the check is off and everything passes —
        collision with *existing* catalog barcodes is checked separately in
        classify.py and does not depend on this.
        """
        if not self.valid_new_ranges:
            return True
        n = int(barcode)
        return any(lo <= n <= hi for lo, hi in self.valid_new_ranges)

    def describe_ranges(self) -> str:
        """Human-readable range list for conflict messages, e.g.
        '500000-500999 or 501003+'."""
        parts = []
        for lo, hi in self.valid_new_ranges:
            if self.max is not None and hi >= self.max:
                parts.append(f"{lo}+")
            else:
                parts.append(f"{lo}-{hi}")
        return " or ".join(parts)


@dataclass(frozen=True)
class CatalogColumns:
    """Column-name mapping for the catalog export. An empty string marks a
    column the export simply does not have (allowed only where noted);
    configured names are validated against the real header row at load time.
    """

    isbn: str = "ISBN"          # required non-empty
    barcode: str = "Barcode"    # required non-empty
    title: str = "Title"
    author: str = "Author"
    call_number: str = "Call Number"  # may be "" — reconcile reports that fact
    resource_id: str = ""       # optional, used in log/report context only
    type: str = ""              # optional; "" disables the resource-type filter


@dataclass(frozen=True)
class CatalogConfig:
    columns: CatalogColumns = field(default_factory=CatalogColumns)
    # Rows whose type column differs from this are skipped (only when a type
    # column is configured). Blank type values are kept.
    book_type: str = "Book"


@dataclass(frozen=True)
class LookupConfig:
    # Path to a custom LC class-fallback map (TOML, same shape as the shipped
    # retrocat/data/lc_class_map.toml). Empty = use the shipped map.
    class_map_file: str = ""


@dataclass(frozen=True)
class OutputConfig:
    mrc_filename: str = "catalog_import.mrc"


@dataclass(frozen=True)
class Config:
    library: LibraryConfig = field(default_factory=LibraryConfig)
    barcodes: BarcodeConfig = field(default_factory=BarcodeConfig)
    catalog: CatalogConfig = field(default_factory=CatalogConfig)
    lookup: LookupConfig = field(default_factory=LookupConfig)
    output: OutputConfig = field(default_factory=OutputConfig)


def _check_unknown_keys(
    given: dict, allowed: set[str], where: str, problems: list[str]
) -> None:
    for key in given:
        if key not in allowed:
            problems.append(f"unknown key {key!r} in [{where}] — typo?")


def _field_names(cls) -> set[str]:
    return {f.name for f in fields(cls)}


def load_config(path: str | Path) -> Config:
    """Load and validate a config TOML. Raises ConfigError on any problem."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path} — copy sample/config.toml next to "
            "your data and edit it, then pass --config if it is not at ./"
            f"{DEFAULT_CONFIG_FILENAME}"
        )
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

    problems: list[str] = []
    _check_unknown_keys(
        raw, {"library", "barcodes", "catalog", "lookup", "output"},
        "top level", problems,
    )

    lib_raw = raw.get("library", {})
    _check_unknown_keys(lib_raw, _field_names(LibraryConfig), "library", problems)
    library = LibraryConfig(**{
        k: v for k, v in lib_raw.items() if k in _field_names(LibraryConfig)
    })

    bar_raw = dict(raw.get("barcodes", {}))
    _check_unknown_keys(bar_raw, _field_names(BarcodeConfig), "barcodes", problems)
    ranges_raw = bar_raw.pop("valid_new_ranges", [])
    ranges: list[tuple[int, int]] = []
    for i, pair in enumerate(ranges_raw):
        if (not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(v, int) for v in pair)):
            problems.append(
                f"[barcodes].valid_new_ranges[{i}] must be a two-integer "
                f"array [lo, hi], got {pair!r}"
            )
            continue
        lo, hi = pair
        if lo > hi:
            problems.append(
                f"[barcodes].valid_new_ranges[{i}]: lo {lo} > hi {hi}"
            )
            continue
        ranges.append((lo, hi))
    barcodes = BarcodeConfig(
        **{k: v for k, v in bar_raw.items() if k in _field_names(BarcodeConfig)},
        valid_new_ranges=tuple(ranges),
    )

    cat_raw = dict(raw.get("catalog", {}))
    _check_unknown_keys(cat_raw, {"columns", "book_type"}, "catalog", problems)
    col_raw = cat_raw.get("columns", {})
    _check_unknown_keys(col_raw, _field_names(CatalogColumns), "catalog.columns",
                        problems)
    columns = CatalogColumns(**{
        k: v for k, v in col_raw.items() if k in _field_names(CatalogColumns)
    })
    catalog = CatalogConfig(
        columns=columns, book_type=cat_raw.get("book_type", "Book")
    )

    look_raw = raw.get("lookup", {})
    _check_unknown_keys(look_raw, _field_names(LookupConfig), "lookup", problems)
    lookup = LookupConfig(**{
        k: v for k, v in look_raw.items() if k in _field_names(LookupConfig)
    })

    out_raw = raw.get("output", {})
    _check_unknown_keys(out_raw, _field_names(OutputConfig), "output", problems)
    output = OutputConfig(**{
        k: v for k, v in out_raw.items() if k in _field_names(OutputConfig)
    })

    config = Config(library=library, barcodes=barcodes, catalog=catalog,
                    lookup=lookup, output=output)
    problems.extend(validate_config(config))
    if problems:
        raise ConfigError(
            f"{path}: {len(problems)} problem(s):\n  - " + "\n  - ".join(problems)
        )
    return config


def validate_config(config: Config) -> list[str]:
    """Semantic validation, shared by load_config and tests."""
    problems: list[str] = []

    if not config.library.home_library.strip():
        problems.append(
            "[library].home_library is required — records must carry your "
            "library's real name, not a placeholder"
        )
    if len(config.library.marc_language) != 3:
        problems.append(
            f"[library].marc_language must be a 3-character MARC language "
            f"code (e.g. 'eng'), got {config.library.marc_language!r}"
        )

    b = config.barcodes
    if b.length in (10, 13):
        problems.append(
            f"[barcodes].length = {b.length} collides with ISBN token "
            "classification (ISBNs are 10 or 13 digits) — scan lines would "
            "be ambiguous"
        )
    elif b.length < 1:
        problems.append(f"[barcodes].length must be positive, got {b.length}")
    if b.min is not None and b.max is not None and b.min > b.max:
        problems.append(f"[barcodes].min {b.min} > max {b.max}")

    cols = config.catalog.columns
    if not cols.isbn.strip():
        problems.append(
            "[catalog.columns].isbn is required — dedup against the export "
            "is impossible without it"
        )
    if not cols.barcode.strip():
        problems.append(
            "[catalog.columns].barcode is required — ALREADY_DONE/CONFLICT "
            "detection is impossible without it"
        )

    if not config.output.mrc_filename.strip():
        problems.append("[output].mrc_filename must not be blank")
    return problems

"""ISBN normalization, checksum validation, and 10<->13 canonicalization.

Canonical form throughout the pipeline is ISBN-13 (see docs/DESIGN.md, "ISBN
canonicalization"). Catalog exports are frequently ISBN-10-heavy while
barcode-scanner reads are ~100% ISBN-13 EANs, so every comparison must go
through :func:`canonical_isbn13` — raw string comparison silently misses real
duplicates while all the totals still balance.
"""

from __future__ import annotations

import re

# An ISBN-shaped token inside a junky export field: digits (hyphens allowed
# inside), optionally ending in X. Length constraints are checked after
# stripping hyphens.
_ISBN_TOKEN_RE = re.compile(r"\d[\dXx-]{8,15}[\dXx]")


def normalize(code: str) -> str:
    """Strip everything but digits/X and uppercase — the raw comparable form."""
    return re.sub(r"[^0-9Xx]", "", code).upper()


def isbn10_check_digit(core9: str) -> str:
    """Check digit for a 9-digit ISBN-10 core ('X' possible)."""
    total = sum((10 - i) * int(d) for i, d in enumerate(core9))
    remainder = (11 - total % 11) % 11
    return "X" if remainder == 10 else str(remainder)


def isbn13_check_digit(core12: str) -> str:
    """Check digit for a 12-digit ISBN-13/EAN core."""
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core12))
    return str((10 - total % 10) % 10)


def is_valid_isbn10(isbn: str) -> bool:
    isbn = normalize(isbn)
    if len(isbn) != 10 or not isbn[:9].isdigit():
        return False
    if not (isbn[9].isdigit() or isbn[9] == "X"):
        return False
    return isbn10_check_digit(isbn[:9]) == isbn[9]


def is_valid_isbn13(isbn: str) -> bool:
    isbn = normalize(isbn)
    if len(isbn) != 13 or not isbn.isdigit():
        return False
    if not isbn.startswith(("978", "979")):
        return False
    return isbn13_check_digit(isbn[:12]) == isbn[12]


def isbn10_to_13(isbn10: str) -> str:
    """Convert ISBN-10 -> ISBN-13: prepend 978, recompute the check digit.

    Conversion is applied even when the ISBN-10 check digit is wrong — the
    recomputed EAN check digit rescues check-digit-only typos in the export.
    """
    isbn10 = normalize(isbn10)
    if len(isbn10) != 10:
        raise ValueError(f"not an ISBN-10: {isbn10!r}")
    if not isbn10[:9].isdigit():
        # e.g. export junk with an 'X' in a non-final position — reject
        # cleanly so callers can log-and-skip instead of crashing on int().
        raise ValueError(f"ISBN-10 core is not numeric: {isbn10!r}")
    core = "978" + isbn10[:9]
    return core + isbn13_check_digit(core)


def isbn13_to_10(isbn13: str) -> str | None:
    """Convert a 978-prefixed ISBN-13 back to ISBN-10; None for 979 (no 10-form)."""
    isbn13 = normalize(isbn13)
    if len(isbn13) != 13 or not isbn13.startswith("978"):
        return None
    core = isbn13[3:12]
    if not core.isdigit():  # junk guard, same rationale as isbn10_to_13
        return None
    return core + isbn10_check_digit(core)


def canonical_isbn13(isbn: str) -> str:
    """Canonical ISBN-13 form of any 10- or 13-length ISBN string."""
    isbn = normalize(isbn)
    if len(isbn) == 13:
        return isbn
    if len(isbn) == 10:
        return isbn10_to_13(isbn)
    raise ValueError(f"not an ISBN: {isbn!r}")


def extract_isbns(field_value: str) -> tuple[list[str], list[str]]:
    """Pull ISBN-shaped tokens out of a junky multi-value export field.

    Returns (valid_normalized, rejected_tokens): tokens that normalize to
    length 10 or 13 vs. everything else ISBN-shaped (wrong length after
    stripping hyphens — logged and skipped by the caller).
    """
    valid: list[str] = []
    rejected: list[str] = []
    for token in _ISBN_TOKEN_RE.findall(field_value or ""):
        norm = normalize(token)
        if len(norm) in (10, 13):
            valid.append(norm)
        else:
            rejected.append(token)
    return valid, rejected

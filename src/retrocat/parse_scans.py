"""Scan-file parsing: token classification, pairing state machine, dedup rules.

Input contract (docs/DESIGN.md "Scan files"): one code per line, ISBN then its
barcode, repeating. Lone barcodes (no-ISBN books) are legal; two ISBNs in a
row are not. Whitespace-only lines, CRLF, and a UTF-8 BOM are tolerated.

What counts as a barcode comes from ``BarcodeConfig`` (length, optional
min/max), nothing here hard-codes any library's numbering scheme.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Union

from .config import BarcodeConfig
from .isbn import canonical_isbn13, is_valid_isbn10, is_valid_isbn13, normalize

logger = logging.getLogger(__name__)


class ScanParseError(Exception):
    """Hard scan-file error, always carrying file + line context."""

    def __init__(self, file: str, line: int, message: str) -> None:
        self.file = file
        self.line = line
        super().__init__(f"{file}:{line}: {message}")


@dataclass(frozen=True)
class ScanPair:
    isbn: str  # normalized as scanned (10 or 13 digits)
    barcode: str
    source_file: str
    line: int  # line of the ISBN scan

    @property
    def canonical_isbn(self) -> str:
        return canonical_isbn13(self.isbn)


@dataclass(frozen=True)
class LoneBarcode:
    barcode: str
    source_file: str
    line: int


ScannedBook = Union[ScanPair, LoneBarcode]

_ISBN = "ISBN"
_BARCODE = "BARCODE"


def _classify_token(
    token: str, file: str, line: int, barcodes: BarcodeConfig
) -> str:
    """Deterministic token classification; hard error on anything ambiguous.

    Length 10/13 (digits, optional trailing X for ISBN-10) -> ISBN, checksum
    validated. Exactly ``barcodes.length`` digits -> barcode, bounds-checked
    when min/max are configured. Anything else -> malformed line, fail loud.
    """
    if len(token) == barcodes.length and token.isdigit():
        n = int(token)
        if barcodes.min is not None and n < barcodes.min:
            raise ScanParseError(
                file, line,
                f"{barcodes.length}-digit code {token!r} below configured "
                f"barcode range minimum {barcodes.min}",
            )
        if barcodes.max is not None and n > barcodes.max:
            raise ScanParseError(
                file, line,
                f"{barcodes.length}-digit code {token!r} above configured "
                f"barcode range maximum {barcodes.max}",
            )
        return _BARCODE
    if len(token) in (10, 13):
        _validate_isbn_checksum(token, file, line)
        return _ISBN
    raise ScanParseError(
        file, line,
        f"malformed line {token!r}: not a 10/13-digit ISBN or "
        f"{barcodes.length}-digit barcode",
    )


def _validate_isbn_checksum(token: str, file: str, line: int) -> None:
    """Checksum-validate every ISBN token, catches typed-in transcription typos."""
    if len(token) == 13:
        if not token.isdigit():
            raise ScanParseError(file, line, f"ISBN-13 {token!r} contains non-digits")
        if not token.startswith(("978", "979")):
            raise ScanParseError(
                file, line, f"ISBN-13 {token!r} lacks a 978/979 prefix"
            )
        if not is_valid_isbn13(token):
            raise ScanParseError(
                file, line,
                f"ISBN-13 {token!r} fails its check digit, likely a typo, re-enter it",
            )
    else:
        if not is_valid_isbn10(token):
            raise ScanParseError(
                file, line,
                f"ISBN-10 {token!r} fails its check digit, likely a typo, re-enter it",
            )


def parse_scan_file(
    path: str | Path, barcodes: BarcodeConfig
) -> list[ScannedBook]:
    """Parse one scan file via the pairing state machine.

    States: EXPECT_ISBN (no pair open) and EXPECT_BARCODE (an ISBN is pending).
    Transitions (see docs/DESIGN.md, these exact behaviors are contractual):
      EXPECT_ISBN    + ISBN    -> open a pair, EXPECT_BARCODE
      EXPECT_ISBN    + barcode -> emit LoneBarcode (no-ISBN book), stay
      EXPECT_BARCODE + barcode -> emit ScanPair, EXPECT_ISBN
      EXPECT_BARCODE + ISBN    -> hard error (message differs: identical ISBN
                                  = double-scan vs. different ISBN = missing barcode)
      EOF in EXPECT_BARCODE    -> hard error (trailing unpaired ISBN)
    """
    path = Path(path)
    fname = str(path)
    books: list[ScannedBook] = []
    pending_isbn: str | None = None
    pending_line = 0

    # utf-8-sig strips a BOM if present; splitlines handles CRLF.
    text = path.read_text(encoding="utf-8-sig")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            continue  # whitespace-only line: skip, don't error
        token = normalize(stripped)
        if not token:
            # Non-blank line with no digits at all, junk, not an empty line.
            raise ScanParseError(
                fname, lineno,
                f"malformed line {stripped!r}: not a 10/13-digit ISBN or "
                f"{barcodes.length}-digit barcode",
            )
        kind = _classify_token(token, fname, lineno, barcodes)

        if kind == _ISBN:
            if pending_isbn is not None:
                if token == pending_isbn:
                    raise ScanParseError(
                        fname, lineno,
                        f"same ISBN {token} twice in a row, likely a double-scan, "
                        "delete one line",
                    )
                raise ScanParseError(
                    fname, lineno,
                    f"two different ISBNs in a row ({pending_isbn} at line "
                    f"{pending_line}, then {token}), a barcode was never scanned "
                    "for the first one",
                )
            pending_isbn = token
            pending_line = lineno
        else:  # barcode
            if pending_isbn is not None:
                books.append(
                    ScanPair(isbn=pending_isbn, barcode=token,
                             source_file=fname, line=pending_line)
                )
                pending_isbn = None
            else:
                books.append(LoneBarcode(barcode=token, source_file=fname, line=lineno))

    if pending_isbn is not None:
        raise ScanParseError(
            fname, pending_line,
            f"file ends with unpaired ISBN {pending_isbn}, its barcode was never scanned",
        )
    return books


def dedupe_scans(books: Iterable[ScannedBook]) -> list[ScannedBook]:
    """Apply the cross-file duplicate rules.

    Same barcode + same ISBN seen twice -> silently dedupe (idempotent rescan).
    Same barcode + two different ISBNs  -> hard error (mispaired scan or a
    sticker moved between books). A barcode seen both lone and paired is the
    same class of inconsistency -> hard error.
    """
    seen: dict[str, ScannedBook] = {}
    result: list[ScannedBook] = []
    for book in books:
        prior = seen.get(book.barcode)
        if prior is None:
            seen[book.barcode] = book
            result.append(book)
            continue
        prior_isbn = prior.isbn if isinstance(prior, ScanPair) else None
        this_isbn = book.isbn if isinstance(book, ScanPair) else None
        if prior_isbn == this_isbn:
            logger.info(
                "barcode %s rescanned identically (%s:%d and %s:%d), deduped",
                book.barcode, prior.source_file, prior.line,
                book.source_file, book.line,
            )
            continue
        raise ScanParseError(
            book.source_file, book.line,
            f"barcode {book.barcode} paired with two different ISBNs "
            f"({prior_isbn or 'none'} at {prior.source_file}:{prior.line}, "
            f"{this_isbn or 'none'} here), mispaired scan or sticker on the wrong book",
        )
    return result


def parse_scan_dir(
    scans_dir: str | Path, barcodes: BarcodeConfig
) -> list[ScannedBook]:
    """Parse every *.txt in a directory (sorted for determinism) and dedupe."""
    scans_dir = Path(scans_dir)
    files = sorted(scans_dir.glob("*.txt"))
    if not files:
        raise FileNotFoundError(f"no *.txt scan files found in {scans_dir}")
    books: list[ScannedBook] = []
    for f in files:
        parsed = parse_scan_file(f, barcodes)
        logger.info("parsed %s: %d books", f.name, len(parsed))
        books.extend(parsed)
    return dedupe_scans(books)

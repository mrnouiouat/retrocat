"""Local LC call-number construction for books that resolve no authoritative
call number from OpenLibrary or LoC.

When lookup falls back to a class-level guess (Google Books categories ->
CLASS_FALLBACK_MAP), a bare class like ``BP`` or ``D`` is not shelf-able: a
whole shelf of Islam books would all read ``BP`` and collide. Here we turn that
bare class into a fuller, distinct, correctly *shaped* LC call number by adding:

  * a **Cutter number** derived from the main entry (author surname, else
    title), using the standard Library of Congress Cutter table, and
  * the **year** of publication.

The Cutter table is a deterministic offline algorithm — no network, no tokens.
Table verified 2026-07-06 against two independent transcriptions of the LC
Classification and Shelflisting Manual G 63 (itsmarc.com "LC Cutter Table:
Basic" and the Nebraska Library Commission), which agree exactly. Ground truth:
``cutter("Wills") == "W55"`` reproduces the real LC number ``BP130 .W55 2017``
for ISBN 9781101981023 (Garry Wills, *What the Qur'an Meant*), which the live
pilot resolved authoritatively — so the local path matches the real number.

The subject *class* is still only inferred, so callers tag the result
confidence=low for human spot-check. The Cutter/year are exact transforms of
real metadata, not fabrication.
"""

from __future__ import annotations

import re
import unicodedata

_ARTICLES = {"a", "an", "the"}
# A 4-digit year, 1000-2099, not embedded in a longer digit run. Handles
# "2017", "2017-05-01", "c1994" (copyright form), "June 2005".
_YEAR_RE = re.compile(r"(?<!\d)(1[0-9]{3}|20[0-9]{2})(?!\d)")


def _ranges_to_map(spec: list[tuple[str, str, int]]) -> dict[str, int]:
    """Expand a list of (start_letter, end_letter, digit) into a full a-z map.
    Ranges cover a-z with no gaps: letters between two listed breakpoints take
    the lower breakpoint's digit, per LC 'use judgement / round down' practice.
    """
    out: dict[str, int] = {}
    for start, end, digit in spec:
        for code in range(ord(start), ord(end) + 1):
            out[chr(code)] = digit
    return out


# LC Cutter table (G 63), second-letter -> digit, one table per initial class.
# After an initial vowel (A E I O U): b=2 d=3 l-m=4 n=5 p=6 r=7 s-t=8 u-y=9
_AFTER_VOWEL = _ranges_to_map([
    ("a", "c", 2), ("d", "k", 3), ("l", "m", 4), ("n", "o", 5),
    ("p", "q", 6), ("r", "r", 7), ("s", "t", 8), ("u", "z", 9),
])
# After initial S: a=2 ch=3 e=4 h-i=5 m-p=6 t=7 u=8 w-z=9 (c always -> 3)
_AFTER_S = _ranges_to_map([
    ("a", "b", 2), ("c", "d", 3), ("e", "g", 4), ("h", "l", 5),
    ("m", "s", 6), ("t", "t", 7), ("u", "v", 8), ("w", "z", 9),
])
# After initial Qu (applied to the letter following 'u'):
# a=3 e=4 i=5 o=6 r=7 t=8 y=9
_AFTER_QU = _ranges_to_map([
    ("a", "d", 3), ("e", "h", 4), ("i", "n", 5), ("o", "q", 6),
    ("r", "s", 7), ("t", "v", 8), ("w", "z", 9),
])
# After any other initial consonant: a=3 e=4 i=5 o=6 r=7 u=8 y=9
_AFTER_CONSONANT = _ranges_to_map([
    ("a", "d", 3), ("e", "h", 4), ("i", "n", 5), ("o", "q", 6),
    ("r", "t", 7), ("u", "x", 8), ("y", "z", 9),
])
# Expansion table for the next (third) letter: a-d=3 e-h=4 i-l=5 m-o=6 p-s=7
# t-v=8 w-z=9
_EXPANSION = _ranges_to_map([
    ("a", "d", 3), ("e", "h", 4), ("i", "l", 5), ("m", "o", 6),
    ("p", "s", 7), ("t", "v", 8), ("w", "z", 9),
])


def _ascii_letters(word: str) -> str:
    """Strip diacritics and non-letters: 'Darāz' -> 'Daraz', "Qur'an" -> 'Quran'."""
    decomposed = unicodedata.normalize("NFKD", word)
    return "".join(c for c in decomposed if c.isascii() and c.isalpha())


def cutter(word: str) -> str:
    """LC Cutter for a single word (no leading dot), e.g. 'Wills' -> 'W55'.

    Returns '' if the word has no usable letters. Per the LC rule a Cutter
    never ends in 0 or 1 (the tables only emit 2-9, but we guard anyway).
    """
    letters = _ascii_letters(word)
    if not letters:
        return ""
    first = letters[0].upper()
    tail = letters[1:].lower()

    if first == "S" and tail.startswith("ch"):
        primary, exp_tail = 3, tail[2:]
    elif first == "Q" and tail.startswith("u"):
        after_u = tail[1:]
        primary = _AFTER_QU.get(after_u[0]) if after_u else None
        exp_tail = after_u[1:] if after_u else ""
    else:
        if first in "AEIOU":
            table = _AFTER_VOWEL
        elif first == "S":
            table = _AFTER_S
        else:
            table = _AFTER_CONSONANT
        primary = table.get(tail[0]) if tail else None
        exp_tail = tail[1:] if tail else ""

    digits = ""
    if primary is not None:
        digits += str(primary)
        if exp_tail and (exp := _EXPANSION.get(exp_tail[0])) is not None:
            digits += str(exp)

    result = first + digits
    while result[-1:] in ("0", "1"):
        result = result[:-1]
    return result


def _first_significant_word(text: str) -> str:
    """First word that is not a leading article ('The Qur'an' -> 'Qur'an')."""
    for token in text.split():
        cleaned = _ascii_letters(token)
        if cleaned and cleaned.lower() not in _ARTICLES:
            return token
    tokens = text.split()
    return tokens[0] if tokens else ""


def main_entry_word(
    author: str | None, title: str | None, *, corporate: bool
) -> str:
    """The word to Cutter on, following LC main-entry practice.

    Personal author -> surname (last token of the first author; author strings
    arrive as 'Given Surname' per author, joined by ', '). Corporate author ->
    first significant word of the organization name. No author -> first
    significant word of the title (skipping a leading article).
    """
    if author:
        if corporate:
            return _first_significant_word(author)
        first_author = author.split(",")[0].strip()
        tokens = first_author.split()
        return tokens[-1] if tokens else first_author
    return _first_significant_word(title or "")


def extract_year(*candidates: str | None) -> str | None:
    """First 4-digit year found in any candidate date string, else None."""
    for candidate in candidates:
        if candidate:
            match = _YEAR_RE.search(str(candidate))
            if match:
                return match.group(1)
    return None


def build_call_number(
    lc_class: str,
    *,
    author: str | None = None,
    title: str | None = None,
    year: str | None = None,
    corporate: bool = False,
) -> str:
    """Assemble a full LC-shaped call number from an inferred class.

    'BP130' + author 'Garry Wills' + year '2017' -> 'BP130 .W55 2017'.
    Falls back gracefully: no Cutter -> 'BP130 2017'; no year -> 'BP130 .W55'.
    """
    cut = cutter(main_entry_word(author, title, corporate=corporate))
    parts = [lc_class]
    if cut:
        parts.append(f".{cut}")
    if year:
        parts.append(str(year))
    return " ".join(parts)

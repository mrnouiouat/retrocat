"""Tests for lc_call.py, the local LC Cutter / call-number generator.

The Cutter table is verified against LC Shelflisting Manual G 63. The anchor
case is real: cutter("Wills") == "W55" reproduces the authoritative LC number
BP130 .W55 2017 that the live pilot resolved for 9781101981023.
"""

from __future__ import annotations

import pytest

from retrocat.lc_call import (
    build_call_number,
    cutter,
    extract_year,
    main_entry_word,
)


# --------------------------------------------------------------------------
# Cutter algorithm, one case per initial-letter table, worked by hand
# --------------------------------------------------------------------------

@pytest.mark.parametrize("word, expected", [
    # Ground truth: matches the real LC number BP130 .W55 2017.
    ("Wills", "W55"),         # W consonant, i=5, expansion l=5
    # Other initial consonants: a=3 e=4 i=5 o=6 r=7 u=8 y=9
    ("Carter", "C37"),        # C consonant, a=3, expansion r=7
    ("Smith", "S65"),         # S table: m=6, expansion i=5
    ("Scott", "S36"),         # S table: c=3, expansion o=6
    ("Schmidt", "S36"),       # S table: 'ch'=3, expansion (after 'ch') m=6
    # After initial vowels: b=2 d=3 l-m=4 n=5 p=6 r=7 s-t=8 u-y=9
    ("Eaton", "E28"),         # E vowel, a=2, expansion t=8
    ("Li", "L5"),             # 2-letter word: L consonant, i=5, no expansion
    # Qu compound initial: map the letter after 'u'.
    ("Quran", "Q73"),         # Qu + r=7, expansion a=3
])
def test_cutter_worked_cases(word, expected):
    assert cutter(word) == expected


def test_cutter_strips_diacritics_and_punctuation():
    assert cutter("Darāz") == cutter("Daraz")
    assert cutter("Qur'an") == cutter("Quran")


def test_cutter_empty_or_letterless_is_blank():
    assert cutter("") == ""
    assert cutter("1994") == ""
    assert cutter("   ") == ""


def test_cutter_never_ends_in_zero_or_one():
    # The tables only emit 2-9, so no generated Cutter should end 0/1.
    for word in ["Aaron", "Zoë", "Xu", "Oo", "Bb"]:
        c = cutter(word)
        assert not c or c[-1] not in "01", (word, c)


# --------------------------------------------------------------------------
# Main-entry selection (what gets Cuttered)
# --------------------------------------------------------------------------

def test_main_entry_personal_author_uses_surname():
    assert main_entry_word("Garry Wills", "Whatever", corporate=False) == "Wills"


def test_main_entry_first_of_multiple_authors():
    # Google joins authors with ", "cutter on the FIRST author's surname.
    assert main_entry_word(
        "Camillia Fawzi El-Solh, Judy Mabro", None, corporate=False
    ) == "El-Solh"


def test_main_entry_corporate_uses_first_significant_word():
    assert main_entry_word(
        "El-Falah Foundation.", None, corporate=True
    ) == "El-Falah"
    assert main_entry_word(
        "The Riverside Historical Society", None, corporate=True
    ) == "Riverside"  # leading article skipped


def test_main_entry_no_author_falls_back_to_title_skipping_article():
    assert main_entry_word(None, "The Productive Muslim", corporate=False) == "Productive"


# --------------------------------------------------------------------------
# Year extraction
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("2017", "2017"),
    ("2017-05-01", "2017"),
    ("c1994", "1994"),
    ("June 2005", "2005"),
    ("", None),
    (None, None),
    ("n.d.", None),
])
def test_extract_year(raw, expected):
    assert extract_year(raw) == expected


def test_extract_year_first_hit_across_candidates():
    assert extract_year(None, "", "1980") == "1980"


# --------------------------------------------------------------------------
# Full assembly
# --------------------------------------------------------------------------

def test_build_call_number_full():
    # The real number for 9781101981023, rebuilt entirely locally.
    assert build_call_number(
        "BP130", author="Garry Wills", title="What the Qur'an Meant", year="2017"
    ) == "BP130 .W55 2017"


def test_build_call_number_degrades_gracefully():
    assert build_call_number("D", author="Jane Mayer") == "D .M39"
    assert build_call_number("BP", author=None, title=None) == "BP"
    assert build_call_number(
        "BP", author=None, title="1994"
    ) == "BP"  # no letters to Cutter, no year


def test_build_call_number_corporate():
    # Corporate main entry Cutters on the org's first word, not a person.
    assert build_call_number(
        "BP", author="El-Falah Foundation.", corporate=True
    ) == "BP .E44"

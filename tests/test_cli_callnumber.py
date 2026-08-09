"""Tests for the standalone `retrocat callnumber` tool.

Wraps lc_call.py (whose algorithm is tested in test_lc_call.py); these tests
cover the CLI surface: single-shot, cutter-only, batch CSV, the corporate
heuristic/override, and the error paths. No config file, no network.
"""

from __future__ import annotations

import csv
import io

from retrocat.__main__ import main


def run(capsys, *argv):
    rc = main(["callnumber", *argv])
    out = capsys.readouterr().out
    return rc, out


# --------------------------------------------------------------------------
# Single-shot
# --------------------------------------------------------------------------

def test_full_call_number_anchor_case(capsys):
    # The module's ground-truth case: reproduces the real LC number for
    # 9781101981023 entirely offline.
    rc, out = run(capsys, "--lc-class", "BP130",
                  "--author", "Garry Wills", "--year", "2017")
    assert rc == 0
    assert out.strip() == "BP130 .W55 2017"


def test_title_cutter_when_no_author(capsys):
    rc, out = run(capsys, "--lc-class", "BP", "--title", "The Productive Muslim")
    assert rc == 0
    # Leading article skipped; Cutter on 'Productive'.
    assert out.strip() == "BP .P76"


def test_year_extracted_from_messy_string(capsys):
    rc, out = run(capsys, "--lc-class", "D21", "--author", "Jane Mayer",
                  "--year", "c1994")
    assert rc == 0
    assert out.strip().endswith("1994")


def test_corporate_heuristic_is_automatic(capsys):
    # 'Foundation' triggers the org keywords: Cutter on the org's first
    # significant word, not a surname.
    rc, out = run(capsys, "--lc-class", "BP",
                  "--author", "El-Falah Foundation")
    assert rc == 0
    assert out.strip() == "BP .E44"


def test_corporate_flag_forces_org_treatment(capsys):
    # 'Acme Publishing' has no org keyword — the flag forces first-word
    # Cutter instead of surname ('Publishing').
    rc, out_forced = run(capsys, "--lc-class", "Z249", "--author",
                         "Acme Publishing", "--corporate")
    rc2, out_auto = run(capsys, "--lc-class", "Z249", "--author",
                        "Acme Publishing")
    assert rc == rc2 == 0
    assert out_forced.strip() == "Z249 .A26"   # Acme (A vowel, c=2, m=6)
    assert out_auto.strip() == "Z249 .P83"     # Publishing (surname rule)


# --------------------------------------------------------------------------
# Cutter-only
# --------------------------------------------------------------------------

def test_cutter_only(capsys):
    rc, out = run(capsys, "--cutter", "Wills")
    assert rc == 0
    assert out.strip() == "W55"


def test_cutter_only_letterless_word_errors(capsys):
    rc, _ = run(capsys, "--cutter", "1994")
    assert rc == 1


# --------------------------------------------------------------------------
# Batch CSV
# --------------------------------------------------------------------------

def test_batch_adds_call_number_column(tmp_path, capsys):
    src = tmp_path / "books.csv"
    src.write_text(
        "lc_class,author,title,year\n"
        "BP130,Garry Wills,What the Qur'an Meant,2017\n"
        "D21,,The Story of Civilization,1961\n"
        ",Someone,No Class Row,2000\n",
        encoding="utf-8",
    )
    rc, out = run(capsys, "--batch", str(src))
    assert rc == 0
    rows = list(csv.DictReader(io.StringIO(out)))
    assert [r["call_number"] for r in rows] == [
        "BP130 .W55 2017",
        "D21 .S76 1961",   # no author -> Cutter from the title
        "",                # no lc_class -> blank, never fabricated
    ]
    # Original columns survive untouched.
    assert rows[0]["author"] == "Garry Wills"


def test_batch_corporate_column(tmp_path, capsys):
    src = tmp_path / "books.csv"
    src.write_text(
        "lc_class,author,corporate\n"
        "Z249,Acme Publishing,yes\n",
        encoding="utf-8",
    )
    rc, out = run(capsys, "--batch", str(src))
    assert rc == 0
    (row,) = list(csv.DictReader(io.StringIO(out)))
    assert row["call_number"] == "Z249 .A26"  # forced corporate -> Acme


def test_batch_without_lc_class_column_errors(tmp_path, capsys):
    src = tmp_path / "books.csv"
    src.write_text("class,author\nBP,Someone\n", encoding="utf-8")
    rc, _ = run(capsys, "--batch", str(src))
    assert rc == 1


def test_batch_missing_file_errors(capsys):
    rc, _ = run(capsys, "--batch", "no-such-file.csv")
    assert rc == 1


# --------------------------------------------------------------------------
# Error / plumbing
# --------------------------------------------------------------------------

def test_no_arguments_is_a_clean_error(capsys):
    rc, _ = run(capsys)
    assert rc == 1


def test_no_config_file_needed(tmp_path, monkeypatch, capsys):
    # The pipeline commands require config.toml; this tool must not.
    monkeypatch.chdir(tmp_path)  # a directory guaranteed to have no config
    rc, out = run(capsys, "--cutter", "Schmidt")
    assert rc == 0
    assert out.strip() == "S36"

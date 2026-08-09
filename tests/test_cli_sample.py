"""Hermetic end-to-end test: the documented two-command flow over sample/.

This is the run a stranger does in the README ("run it on the sample data"),
executed through the real CLI (``main(argv)``) with the network stubbed out:
shelf triage -> operator fills the worklist -> final combined build. Asserts
bucket counts, master-table contents, worklist handoff, MARC round-trip, and
both CLI failure modes (conflict gate, config errors).

The sample tree is copied into tmp_path so nothing ever writes into sample/.
"""

from __future__ import annotations

import csv
import io
import shutil
from pathlib import Path

import pytest
from pymarc import MARCReader

import retrocat.pipeline as pipeline_mod
from retrocat.__main__ import main
from retrocat.lookup import BookMetadata

SAMPLE = Path(__file__).resolve().parent.parent / "sample"

# What the stubbed lookup "resolves" — titles for every ISBN the sample scans
# contain, so classification is deterministic without HTTP.
TITLES = {
    "9781565645998": "Abu Zayd al-Balkhi's Sustenance of the Soul",
    "9781565646988": "Qur'anic Terminology",
    "9780199836741": "Reading the Qur'an",
    "9780316168717": "The Ornament of the World",
    "9780691172422": "Hitler's American Model",
    "9780415455183": "Introducing Islam",
    "9781107620377": "The Impact of Lynching on Black Culture and Memory",
    "9780253211040": "Islam in the African-American Experience",
    "9780674061859": "Southern Horrors",
    "9780316204361": "David and Goliath",
}


class StubLookupClient:
    """Replaces LookupClient in pipeline.py — accepts its constructor kwargs,
    resolves titles from the table above, no HTTP, no cache file."""

    def __init__(self, cache_path=None, class_map=None, **kwargs) -> None:
        pass

    def lookup(self, isbn13: str) -> BookMetadata:
        title = TITLES.get(isbn13)
        return BookMetadata(
            isbn13=isbn13,
            title=title,
            author="Stub Author" if title else None,
            call_number="BP100 .S78 2020" if title else None,
            call_number_source="openlibrary" if title else None,
            confidence="high" if title else None,
        )

    def flush_cache(self) -> None:
        pass


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """A copy of sample/ plus stubbed lookup; returns the workspace root."""
    shutil.copy(SAMPLE / "config.toml", tmp_path / "config.toml")
    shutil.copy(SAMPLE / "catalog_export.csv", tmp_path / "catalog_export.csv")
    shutil.copytree(SAMPLE / "scans", tmp_path / "scans")
    shutil.copy(SAMPLE / "conflict-demo.txt", tmp_path / "conflict-demo.txt")
    monkeypatch.setattr(pipeline_mod, "LookupClient", StubLookupClient)
    return tmp_path


def cli(ws: Path, *argv: str) -> int:
    return main([
        *argv,
        "--config", str(ws / "config.toml"),
        "--export", str(ws / "catalog_export.csv"),
        "--out", str(ws / "output"),
        "--manual-dir", str(ws / "manual"),
        "--cache", str(ws / ".cache" / "lookup_cache.json"),
    ])


def read_mrc(path: Path):
    records = list(MARCReader(io.BytesIO(path.read_bytes()), to_unicode=True))
    assert all(r is not None for r in records), f"unparseable record in {path}"
    return records


def read_csv(path: Path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# --------------------------------------------------------------------------
# Step 1: shelf triage
# --------------------------------------------------------------------------

def test_shelf_run_buckets_and_worklist(workspace):
    rc = cli(workspace, "shelf", "--scan", str(workspace / "scans" / "shelf-a.txt"))
    assert rc == 0

    out = workspace / "output" / "shelf-a"
    rows = read_csv(out / "master_table.csv")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    # shelf-a exercises every non-CONFLICT bucket:
    #   500001 re-scan of an existing barcode (ISBN-10 in export, ISBN-13
    #   scanned — canonical agreement) -> ALREADY_DONE
    #   500101 known ISBN on a new barcode -> MERGE_CANDIDATE
    #   500110+500111 same ISBN twice -> two CREATE rows, one MARC resource
    #   500115 lone barcode -> MANUAL
    assert counts == {"ALREADY_DONE": 1, "MERGE_CANDIDATE": 1,
                      "CREATE": 6, "MANUAL": 1}

    # The multi-copy pair became ONE resource with two 852/876 pairs.
    records = read_mrc(out / "shelf-a.mrc")
    assert len(records) == 6  # 5 distinct CREATE ISBNs + 1 merge candidate
    (multi,) = [r for r in records
                if [f["a"] for f in r.get_fields("020")][0] == "9780415455183"]
    assert [f["p"] for f in multi.get_fields("852")] == ["500110", "500111"]

    # The merge candidate carries the export's stored ISBN-10 as a dual 020.
    (merge,) = [r for r in records
                if "9781565645998" in [f["a"] for f in r.get_fields("020")]]
    assert [f["a"] for f in merge.get_fields("020")] == [
        "9781565645998", "1565645995",
    ]

    # The worklist handoff: manual/shelf-a.csv pre-filled with the lone barcode.
    worklist = read_csv(workspace / "manual" / "shelf-a.csv")
    assert [r["barcode"] for r in worklist] == ["500115"]
    assert not worklist[0]["title"].strip()  # blank, for the operator


# --------------------------------------------------------------------------
# Step 2: operator fills the worklist; final ingests it
# --------------------------------------------------------------------------

def test_final_run_ingests_filled_worklist_and_groups_across_shelves(workspace):
    rc = cli(workspace, "shelf", "--scan", str(workspace / "scans" / "shelf-a.txt"))
    assert rc == 0

    # Operator fills in the manual book at the shelf.
    wl_path = workspace / "manual" / "shelf-a.csv"
    rows = read_csv(wl_path)
    rows[0]["title"] = "Hand-Entered Chapbook"
    rows[0]["author"] = "A. Cataloger"
    rows[0]["language"] = "Arabic"
    with open(wl_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    rc = cli(workspace, "final", "--scans", str(workspace / "scans"))
    assert rc == 0

    out = workspace / "output" / "_final"
    # [output].mrc_filename from sample/config.toml
    records = read_mrc(out / "catalog_import.mrc")

    # 11 scanned books across both shelves -> 8 resources:
    # 6 distinct CREATE ISBNs (9780199836741 on BOTH shelves grouped into one
    # resource with two copies) + 1 merge candidate + 1 filled manual book.
    master = read_csv(out / "master_table.csv")
    assert len(master) == 11
    assert len(records) == 8

    (cross_shelf,) = [r for r in records
                      if [f["a"] for f in r.get_fields("020")][:1] == ["9780199836741"]]
    assert sorted(f["p"] for f in cross_shelf.get_fields("852")) == [
        "500102", "500130",
    ]

    # The manual record: no 020, hand-entered title, operator's language in 008.
    (manual_rec,) = [r for r in records if r.get_fields("020") == []]
    assert manual_rec["245"]["a"] == "Hand-Entered Chapbook"
    assert manual_rec["852"]["p"] == "500115"
    assert manual_rec["008"].data[35:38] == "ara"

    # Master table shows the manual book as shipped.
    manual_row = next(r for r in master if r["barcode"] == "500115")
    assert manual_row["call_number_source"] == "manual"
    assert "shipped in MARC" in manual_row["notes"]

    # Nothing left unfilled.
    assert read_csv(out / "unfilled_manual.csv") == []


# --------------------------------------------------------------------------
# Failure modes through the CLI
# --------------------------------------------------------------------------

def test_conflict_demo_blocks_with_exit_1_then_allow_conflicts_ships(workspace):
    # 500003 is on record for a different ISBN in the export -> CONFLICT.
    rc = cli(workspace, "shelf", "--scan", str(workspace / "conflict-demo.txt"))
    assert rc == 1
    out = workspace / "output" / "conflict-demo"
    assert not (out / "conflict-demo.mrc").exists()
    assert (out / "master_table.csv").exists()  # blocked run still explains itself

    rc = main([
        "shelf", "--scan", str(workspace / "conflict-demo.txt"),
        "--config", str(workspace / "config.toml"),
        "--export", str(workspace / "catalog_export.csv"),
        "--out", str(workspace / "output"),
        "--manual-dir", str(workspace / "manual"),
        "--cache", str(workspace / ".cache" / "lookup_cache.json"),
        "--allow-conflicts",
    ])
    assert rc == 0
    records = read_mrc(out / "conflict-demo.mrc")
    assert len(records) == 1  # the conflicted book is still excluded


def test_missing_config_is_a_clean_exit_1(workspace):
    rc = main([
        "shelf", "--scan", str(workspace / "scans" / "shelf-a.txt"),
        "--config", str(workspace / "nope.toml"),
        "--export", str(workspace / "catalog_export.csv"),
    ])
    assert rc == 1


def test_wrong_column_mapping_is_a_clean_exit_1(workspace):
    # Break the ISBN column name: header validation must abort the run.
    cfg = (workspace / "config.toml").read_text(encoding="utf-8")
    (workspace / "config.toml").write_text(
        cfg.replace('isbn = "ISBN"', 'isbn = "ISBN-13"'), encoding="utf-8"
    )
    rc = cli(workspace, "shelf", "--scan", str(workspace / "scans" / "shelf-a.txt"))
    assert rc == 1
    assert not (workspace / "output" / "shelf-a" / "shelf-a.mrc").exists()


def test_missing_scan_file_is_a_clean_exit_1(workspace):
    rc = cli(workspace, "shelf", "--scan", str(workspace / "scans" / "ghost.txt"))
    assert rc == 1

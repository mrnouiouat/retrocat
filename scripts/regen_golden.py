"""Regenerate tests/fixtures/pilot_golden_output.mrc from the pipeline.

The golden file is a structural regression pin (see fixtures/README.md), so
after a DELIBERATE, reviewed change to the MARC mapping it is regenerated
from the pipeline itself with the same pinned inputs the golden test uses:

    python scripts/regen_golden.py

Never regenerate to silence a failing golden test you don't understand.
The byte comparison exists to catch unintended mapping drift.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from tests.test_integration_golden import (  # noqa: E402
    BUILD_DATE,
    CONFIG,
    FIXTURES,
    StubLookupClient,
)

from retrocat.pipeline import run_pipeline  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        scans = tmp_path / "scans"
        scans.mkdir()
        shutil.copy(FIXTURES / "pilot_scan_input.txt", scans / "pilot.txt")
        stub = StubLookupClient.from_expected_csv(
            FIXTURES / "pilot_expected_output.csv"
        )
        result = run_pipeline(
            scans, FIXTURES / "pilot_catalog_export.csv",
            out_dir=tmp_path / "out",
            config=CONFIG, lookup_client=stub, build_date=BUILD_DATE,
            mrc_name="pilot.mrc",
        )
        target = FIXTURES / "pilot_golden_output.mrc"
        shutil.copy(tmp_path / "out" / "pilot.mrc", target)
        print(f"wrote {result.marc_records} records to {target}")
        print(f"counts: {result.counts}")


if __name__ == "__main__":
    main()

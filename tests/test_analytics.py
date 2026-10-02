"""Offline integration checks for analytics and unchanged pipeline behavior."""

import csv
import sqlite3
from contextlib import closing
from datetime import date

import pytest

from retrocat.__main__ import main
from retrocat.analytics import write_reports
from retrocat.config import BarcodeConfig, Config, LibraryConfig
from retrocat.lookup import BookMetadata
from retrocat.manual import ManualEntry
from retrocat.parse_scans import ScanParseError
from retrocat.pipeline import PipelineError, run_pipeline


ISBN = '9781565645998'
OTHER = '9780312156480'
CONFIG = Config(
    library=LibraryConfig(home_library='Test Library'),
    barcodes=BarcodeConfig(valid_new_ranges=((100, 999999),)),
)


class Lookup:
    def __init__(self, **kwargs):
        pass

    def lookup(self, isbn):
        return BookMetadata(isbn, title='A title', call_number='AC .T45',
                            call_number_source='default', confidence='low')

    def flush_cache(self):
        pass


@pytest.fixture
def workspace(tmp_path):
    scans = tmp_path / 'shelf.txt'
    # Two copies of a merge candidate plus a no-ISBN manual item.
    scans.write_text(f'{ISBN}\n000100\n{ISBN}\n000101\n000102\n')
    export = tmp_path / 'catalog.csv'
    export.write_text('ISBN,Barcode,Title,Author,Call Number\n'
                      f'{ISBN},000001,Existing,Author,BP130\n')
    return dict(scans_dir=scans, export_path=export, out_dir=tmp_path / 'out',
                config=CONFIG, lookup_client=Lookup(), build_date=date(2026, 1, 1))


def run(workspace, db, **kwargs):
    return run_pipeline(**(workspace | kwargs), analytics_db=db)


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return closing(db)


def test_snapshots_counts_provenance_and_queue_do_not_multiply_copies(workspace, tmp_path):
    db_path = tmp_path / 'history.sqlite'
    result = run(workspace, db_path)
    assert result.marc_records == 1
    with connect(db_path) as db:
        summary = db.execute('SELECT * FROM run_summary').fetchone()
        assert summary['status'] == 'completed'
        assert summary['scanned_total'] == summary['recorded_items'] == 3
        assert summary['included_items'] == 2
        assert summary['marc_records'] == 1
        assert summary['pending_manual'] == 1
        # Each merge copy has two issues, but only three items need review.
        assert summary['issue_count'] == 5
        assert summary['review_items'] == 3
        assert dict(db.execute('SELECT action, item_count FROM action_counts')) == {
            'CREATE': 0, 'MERGE_CANDIDATE': 2, 'ALREADY_DONE': 0,
            'MANUAL': 1, 'CONFLICT': 0,
        }
        assert db.execute('SELECT COUNT(*) FROM provenance WHERE source = "default"').fetchone()[0] == 2
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        item = db.execute('SELECT * FROM run_items WHERE barcode = "000100"').fetchone()
        assert item['source_line'] == 1
        assert item['shelf'] == 'shelf'
        report = (workspace['out_dir'] / 'run_report.md').read_bytes()
        queue = (workspace['out_dir'] / 'review_queue.csv').read_bytes()
        write_reports(db, summary['run_id'], workspace['out_dir'])
        assert (workspace['out_dir'] / 'run_report.md').read_bytes() == report
        assert (workspace['out_dir'] / 'review_queue.csv').read_bytes() == queue
    with (workspace['out_dir'] / 'review_queue.csv').open(encoding='utf-8-sig') as stream:
        rows = list(csv.DictReader(stream))
    assert [int(r['priority']) for r in rows] == [2, 2, 2, 3, 3]
    assert rows[0]['barcode'] == '000100'
    assert all(r['run_id'] == summary['run_id'] for r in rows)


def test_enabled_and_disabled_outputs_are_identical_and_history_appends(workspace, tmp_path):
    plain = run(workspace, None)
    original = {p.name: p.read_bytes() for p in workspace['out_dir'].iterdir()}
    assert 'run_report.md' not in original
    db_path = tmp_path / 'history.sqlite'
    enabled = run(workspace, db_path)
    assert enabled == plain
    assert all((workspace['out_dir'] / name).read_bytes() == content
               for name, content in original.items())
    run(workspace, db_path)
    with connect(db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM run_items').fetchone()[0] == 6


@pytest.mark.parametrize('call, expected_source, default_issues', [
    ('', 'manual_default', 1), ('BP130 .M45', 'manual', 0),
])
def test_filled_manual_is_distinct_from_pending_and_default_is_reviewed(
    workspace, tmp_path, call, expected_source, default_issues,
):
    db_path = tmp_path / 'history.sqlite'
    run(workspace, db_path, manual_entries=[
        ManualEntry('shelf', '000102', title='Hand entered', call_number=call),
    ])
    with connect(db_path) as db:
        summary = db.execute('SELECT * FROM run_summary').fetchone()
        assert summary['pending_manual'] == 0
        assert summary['included_items'] == 3
        item = db.execute('SELECT * FROM run_items WHERE barcode = "000102"').fetchone()
        assert item['action'] == 'MANUAL'
        assert item['manual_resolved'] == item['included_in_marc'] == 1
        assert db.execute('SELECT source FROM provenance WHERE barcode = "000102"').fetchone()[0] == expected_source
        assert db.execute('SELECT COUNT(*) FROM issues WHERE barcode = "000102" '
                          'AND code = "default_class"').fetchone()[0] == default_issues


@pytest.mark.parametrize('allow_conflicts', [False, True])
def test_conflict_failure_or_override_retains_review_and_actual_inclusion(
    workspace, tmp_path, allow_conflicts,
):
    # Existing barcode belongs to a different ISBN.
    workspace['scans_dir'].write_text(f'{OTHER}\n000001\n{ISBN}\n000100\n')
    db_path = tmp_path / 'history.sqlite'
    if allow_conflicts:
        run(workspace, db_path, allow_conflicts=True)
    else:
        with pytest.raises(PipelineError):
            run(workspace, db_path)
    with connect(db_path) as db:
        summary = db.execute('SELECT * FROM run_summary').fetchone()
        assert summary['status'] == ('completed' if allow_conflicts else 'failed')
        assert summary['included_items'] == int(allow_conflicts)
        assert db.execute('SELECT included_in_marc FROM run_items WHERE barcode = "000001"').fetchone()[0] == 0
        assert db.execute('SELECT priority FROM issues WHERE code = "conflict"').fetchone()[0] == 1


def test_early_failure_records_unknown_counts_not_clean_run(workspace, tmp_path):
    workspace['scans_dir'].write_text('invalid input\n')
    db_path = tmp_path / 'history.sqlite'
    with pytest.raises(ScanParseError):
        run(workspace, db_path)
    with connect(db_path) as db:
        summary = db.execute('SELECT * FROM run_summary').fetchone()
        assert summary['status'] == 'failed'
        assert summary['scanned_total'] is None
        assert summary['pending_manual'] is None
        assert summary['review_items'] is None
        assert all(r[0] is None for r in db.execute('SELECT item_count FROM action_counts'))
    report = (workspace['out_dir'] / 'run_report.md').read_text()
    assert 'Items needing review: not available' in report
    assert 'invalid input' in report


def test_marc_failure_does_not_claim_manual_item_shipped(workspace, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('simulated MARC write failure')
    monkeypatch.setattr('retrocat.pipeline.build_marc_file', fail)
    db_path = tmp_path / 'history.sqlite'
    with pytest.raises(OSError, match='MARC write failure'):
        run(workspace, db_path, manual_entries=[ManualEntry('shelf', '000102', title='Manual')])
    with connect(db_path) as db:
        assert db.execute('SELECT included_items FROM run_summary').fetchone()[0] == 0
        assert db.execute('SELECT manual_resolved FROM run_items WHERE barcode = "000102"').fetchone()[0] == 1


def test_existing_without_verified_isbn_gets_review(workspace, tmp_path):
    workspace['scans_dir'].write_text('000001\n')
    db_path = tmp_path / 'history.sqlite'
    run(workspace, db_path)
    with connect(db_path) as db:
        assert db.execute('SELECT code FROM issues').fetchone()[0] == 'existing_unverified'


def test_database_failure_does_not_change_success_or_original_error(workspace, tmp_path, caplog):
    blocker = tmp_path / 'not_a_directory'
    blocker.write_text('keep')
    db_path = blocker / 'history.sqlite'
    assert run(workspace, db_path).marc_records == 1
    workspace['scans_dir'].write_text('bad input\n')
    with pytest.raises(ScanParseError):
        run(workspace, db_path)
    assert 'analytics unavailable' in caplog.text
    assert blocker.read_text() == 'keep'


def test_unrelated_database_is_not_modified(workspace, tmp_path, caplog):
    db_path = tmp_path / 'unrelated.sqlite'
    with connect(db_path) as db:
        db.execute('CREATE TABLE other (value TEXT)')
        db.execute('PRAGMA user_version = 1')
    original = db_path.read_bytes()
    run(workspace, db_path)
    assert 'unrelated database' in caplog.text
    assert db_path.read_bytes() == original


def test_snapshot_transaction_rolls_back_on_failure(workspace, tmp_path, monkeypatch, caplog):
    from retrocat.analytics import RunAnalytics
    original = RunAnalytics._save_items
    def fail(self, db):
        original(self, db)
        raise RuntimeError('simulated snapshot failure')
    monkeypatch.setattr(RunAnalytics, '_save_items', fail)
    db_path = tmp_path / 'history.sqlite'
    assert run(workspace, db_path).marc_records == 1
    with connect(db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM run_items').fetchone()[0] == 0
    assert 'simulated snapshot failure' in caplog.text
    monkeypatch.setattr(RunAnalytics, '_save_items', original)
    run(workspace, db_path)
    with connect(db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1


def test_report_failure_keeps_committed_history(workspace, tmp_path, monkeypatch, caplog):
    def fail(*args):
        raise OSError('simulated report failure')
    monkeypatch.setattr('retrocat.analytics.write_reports', fail)
    db_path = tmp_path / 'history.sqlite'
    assert run(workspace, db_path).marc_records == 1
    with connect(db_path) as db:
        assert db.execute('SELECT recorded_items FROM run_summary').fetchone()[0] == 3
    assert 'simulated report failure' in caplog.text


@pytest.mark.parametrize('source, call, expected', [
    ('class_fallback', 'BP .T45', 'estimated_call_number'),
    (None, None, 'blank_call_number'),
    ('openlibrary', 'BP130', None),
])
def test_quality_rules_do_not_flag_matching_authoritative_value(
    workspace, tmp_path, source, call, expected,
):
    class QualityLookup(Lookup):
        def lookup(self, isbn):
            return BookMetadata(isbn, title='A title', call_number=call,
                                call_number_source=source,
                                confidence='high' if source == 'openlibrary' else 'low')
    workspace['scans_dir'].write_text(f'{ISBN}\n000100\n')
    db_path = tmp_path / 'history.sqlite'
    run(workspace, db_path, lookup_client=QualityLookup())
    with connect(db_path) as db:
        codes = {r[0] for r in db.execute('SELECT code FROM issues')}
        if expected:
            assert expected in codes
        else:
            assert codes == set()


@pytest.mark.parametrize('command', ['shelf', 'final'])
def test_cli_opt_in(workspace, tmp_path, monkeypatch, command):
    monkeypatch.setattr('retrocat.pipeline.LookupClient', Lookup)
    config = tmp_path / 'config.toml'
    config.write_text('[library]\nhome_library = "Test Library"\n')
    db_path = tmp_path / 'history.sqlite'
    scan_args = (['--scan', str(workspace['scans_dir'])] if command == 'shelf'
                 else ['--scans', str(tmp_path)])
    assert main([command, *scan_args, '--export', str(workspace['export_path']),
                 '--config', str(config), '--out', str(workspace['out_dir']),
                 '--manual-dir', str(tmp_path / 'manual'),
                 '--analytics-db', str(db_path)]) == 0
    with connect(db_path) as db:
        assert db.execute('SELECT status FROM runs').fetchone()[0] == 'completed'

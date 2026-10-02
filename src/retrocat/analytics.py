"""Optional, local post-run analytics. No decisions flow back into the pipeline.

One transaction per execution; reports are derived from SQL views. Provenance
records only what the existing resolver knows, not guessed field-level sources.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pymarc import Record

from . import __version__
from .catalog import ExistingCatalog
from .classify import Action, Classification
from .config import Config
from .lookup import BookMetadata
from .manual import ManualEntry, manual_metadata
from .reconcile import ReconcileRow

logger = logging.getLogger(__name__)
APPLICATION_ID = 0x52434154  # RCAT; refuse to add tables to an unrelated database.

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    scan_path TEXT NOT NULL, catalog_path TEXT NOT NULL, output_path TEXT NOT NULL,
    config_json TEXT NOT NULL, retrocat_version TEXT NOT NULL,
    allow_conflicts INTEGER NOT NULL, scanned_total INTEGER, marc_records INTEGER,
    item_details_available INTEGER NOT NULL CHECK (item_details_available IN (0, 1)),
    error TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (action TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS run_items (
    run_id TEXT NOT NULL REFERENCES runs(run_id), barcode TEXT NOT NULL,
    shelf TEXT NOT NULL, source_file TEXT NOT NULL, source_line INTEGER NOT NULL,
    isbn TEXT, action TEXT NOT NULL REFERENCES actions(action),
    title TEXT NOT NULL, call_number TEXT NOT NULL, confidence TEXT NOT NULL,
    manual_resolved INTEGER NOT NULL CHECK (manual_resolved IN (0, 1)),
    included_in_marc INTEGER NOT NULL CHECK (included_in_marc IN (0, 1)),
    note TEXT NOT NULL, PRIMARY KEY (run_id, barcode)
);
CREATE TABLE IF NOT EXISTS provenance (
    run_id TEXT NOT NULL, barcode TEXT NOT NULL, field TEXT NOT NULL,
    value TEXT NOT NULL, source TEXT NOT NULL,
    PRIMARY KEY (run_id, barcode, field),
    FOREIGN KEY (run_id, barcode) REFERENCES run_items(run_id, barcode)
);
CREATE TABLE IF NOT EXISTS issues (
    run_id TEXT NOT NULL, barcode TEXT NOT NULL, code TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 3),
    detail TEXT NOT NULL, next_action TEXT NOT NULL,
    PRIMARY KEY (run_id, barcode, code),
    FOREIGN KEY (run_id, barcode) REFERENCES run_items(run_id, barcode)
);
CREATE VIEW IF NOT EXISTS action_counts AS
    SELECT r.run_id, a.action,
      CASE WHEN r.item_details_available = 1 THEN COUNT(i.barcode) END AS item_count
    FROM runs r CROSS JOIN actions a
    LEFT JOIN run_items i ON i.run_id = r.run_id AND i.action = a.action
    GROUP BY r.run_id, a.action;
CREATE VIEW IF NOT EXISTS run_summary AS
    SELECT r.*,
      (SELECT COUNT(*) FROM run_items i WHERE i.run_id = r.run_id) AS recorded_items,
      (SELECT COUNT(*) FROM run_items i WHERE i.run_id = r.run_id
        AND i.included_in_marc = 1) AS included_items,
      CASE WHEN r.item_details_available = 1 THEN
        (SELECT COUNT(*) FROM run_items i WHERE i.run_id = r.run_id
          AND i.action = 'MANUAL' AND i.manual_resolved = 0) END AS pending_manual,
      CASE WHEN r.item_details_available = 1 THEN
        (SELECT COUNT(*) FROM issues q WHERE q.run_id = r.run_id) END AS issue_count,
      CASE WHEN r.item_details_available = 1 THEN
        (SELECT COUNT(DISTINCT barcode) FROM issues q
          WHERE q.run_id = r.run_id) END AS review_items
    FROM runs r;
CREATE VIEW IF NOT EXISTS review_queue AS
    SELECT q.run_id, q.priority, i.shelf, q.barcode, i.isbn, i.action,
      i.title, q.code, q.detail, q.next_action, i.source_file, i.source_line
    FROM issues q JOIN run_items i
      ON i.run_id = q.run_id AND i.barcode = q.barcode;
CREATE VIEW IF NOT EXISTS call_number_sources AS
    SELECT run_id, source, COUNT(*) AS item_count
    FROM provenance WHERE field = 'call_number' GROUP BY run_id, source;
"""


class RunAnalytics:
    """Best-effort observer; never masks a pipeline failure or alters its result.

    The pipeline supplies existing in-memory objects at a few stage boundaries.
    Early failures get a run header; item detail is available after metadata/manual
    resolution. No cache inspection, additional HTTP, or reclassification occurs.
    """

    def __init__(
        self, db_path: str | Path | None, scans: str | Path, export: str | Path,
        out_dir: str | Path, config: Config, allow_conflicts: bool,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else None
        self.scans, self.export, self.out_dir = Path(scans), Path(export), Path(out_dir)
        self.config, self.allow_conflicts = config, allow_conflicts
        self.scanned_total: int | None = None
        self.classification: Classification | None = None
        self.metadata: dict[str, BookMetadata] = {}
        self.manual: dict[str, ManualEntry] = {}
        self.default_class = 'AC'
        self.catalog: ExistingCatalog | None = None
        self.reconcile_rows: list[ReconcileRow] = []
        self.records: list[Record] | None = None

    def __enter__(self):
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.run_id = uuid4().hex
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.db_path is not None:
            try:
                self.save(exc)
            except Exception as analytics_error:
                # Analytics is additive, including when its disk/DB is unavailable.
                logger.warning("analytics unavailable for run %s: %s",
                               self.run_id, analytics_error)
        return False

    def save(self, error: BaseException | None) -> None:
        assert self.db_path is not None
        if self.db_path.resolve() in {
            (self.out_dir / 'run_report.md').resolve(),
            (self.out_dir / 'review_queue.csv').resolve(),
        }:
            raise ValueError('analytics database path conflicts with an analytics report')
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys = ON')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f'unsupported analytics schema version {version}')
            application_id = db.execute('PRAGMA application_id').fetchone()[0]
            if application_id not in (0, APPLICATION_ID) or (application_id == 0 and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
            ).fetchone()):
                raise ValueError('analytics path contains an unrelated database')
            db.executescript(SCHEMA)
            with db:
                db.execute('PRAGMA user_version = 1')
                db.execute(f'PRAGMA application_id = {APPLICATION_ID}')
                db.executemany('INSERT OR IGNORE INTO actions VALUES (?)',
                               [(a.value,) for a in Action])
                db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                    self.run_id, self.started_at,
                    datetime.now(timezone.utc).isoformat(),
                    'completed' if error is None else 'failed',
                    str(self.scans.resolve()), str(self.export.resolve()),
                    str(self.out_dir.resolve()), json.dumps(asdict(self.config), sort_keys=True),
                    __version__, int(self.allow_conflicts), self.scanned_total,
                    len(self.records) if self.records is not None else None,
                    int(self.classification is not None),
                    str(error) if error is not None else '',
                ))
                self._save_items(db)
            # Committed history survives report-write failures.
            write_reports(db, self.run_id, self.out_dir)
        logger.info('analytics run %s saved to %s', self.run_id, self.db_path)

    def _save_items(self, db: sqlite3.Connection) -> None:
        if self.classification is None:
            return
        assert self.catalog is not None
        included = {
            f['p'] for record in self.records or [] for f in record.get_fields('876')
        }
        differences = {r.isbn: r for r in self.reconcile_rows if r.needs_fix}
        for book in self.classification.books:
            entry = self.manual.get(book.barcode)
            meta = (manual_metadata(entry, self.default_class) if entry
                    else self.metadata.get(book.canonical_isbn or ''))
            source = (meta.call_number_source or 'unknown') if meta else 'unknown'
            if entry:
                source = 'manual' if entry.call_number.strip() else 'manual_default'
            call = (meta.call_number or '') if meta else ''
            db.execute('INSERT INTO run_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                self.run_id, book.barcode, Path(book.book.source_file).stem,
                str(Path(book.book.source_file).resolve()), book.book.line, book.canonical_isbn,
                book.action.value, (meta.title or '') if meta else '', call,
                (meta.confidence or '') if meta else '', int(entry is not None),
                int(book.barcode in included), book.note,
            ))
            if call:
                db.execute('INSERT INTO provenance VALUES (?,?,?,?,?)',
                           (self.run_id, book.barcode, 'call_number', call, source))

            def issue(code, priority, detail, next_action):
                db.execute('INSERT INTO issues VALUES (?,?,?,?,?,?)',
                           (self.run_id, book.barcode, code, priority, detail, next_action))

            if book.action == Action.CONFLICT:
                issue('conflict', 1, book.note, 'Check the physical pairing and catalog record.')
            elif book.action == Action.MANUAL and entry is None:
                issue('manual_pending', 2, book.note, 'Fill the shelf manual worklist from the book.')
            elif book.action == Action.ALREADY_DONE:
                known = self.catalog.barcode_to_isbns.get(book.barcode, set())
                if book.canonical_isbn is None or not known:
                    issue('existing_unverified', 2, book.note,
                          'Verify the physical title against the existing catalog record.')
            elif meta and meta.resolved:
                if not call:
                    issue('blank_call_number', 2, 'No call number resolved.',
                          'Assign a call number after checking the book.')
                elif source in ('default', 'manual_default'):
                    issue('default_class', 2, f'Generated from the default class: {call}',
                          'Verify the subject class and shelving before import.')
                elif meta.confidence == 'low' and source != 'manual':
                    issue('estimated_call_number', 3, f'Estimated call number: {call}',
                          'Spot-check the subject class and call number.')
            row = differences.get(book.canonical_isbn)
            if row and book.action == Action.MERGE_CANDIDATE:
                issue('call_number_difference', 3,
                      f'Existing: {row.existing_call_number or "(blank)"}; resolved: {row.resolved_call_number}',
                      'Compare both values; a difference does not establish an error.')


def write_reports(db: sqlite3.Connection, run_id: str, out_dir: str | Path) -> None:
    """Stable ordering and SQL-derived totals; queue is one row per item/issue."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = db.execute('SELECT * FROM run_summary WHERE run_id = ?', (run_id,)).fetchone()
    actions = db.execute('SELECT * FROM action_counts WHERE run_id = ? ORDER BY action',
                         (run_id,)).fetchall()
    queue = db.execute('SELECT * FROM review_queue WHERE run_id = ? '
                       'ORDER BY priority, shelf, barcode, code', (run_id,))
    header = [column[0] for column in queue.description]
    rows = queue.fetchall()
    queue_path = out_dir / 'review_queue.csv'
    queue_tmp = out_dir / f'.review_queue.{run_id}.tmp'
    report_tmp = out_dir / f'.run_report.{run_id}.tmp'
    try:
        with queue_tmp.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
        def known(value):
            return 'not available' if value is None else str(value)
        lines = [
            '# Retrocat run report', '', f'Run: `{run_id}`',
            f'Status: **{summary["status"]}**',
            f'Scans: {summary["scan_path"]}', '',
            f'- Scanned items after deduplication: {known(summary["scanned_total"])}',
            f'- Recorded item details: {summary["recorded_items"]}',
            f'- MARC resource records written: {known(summary["marc_records"])}',
            f'- Physical copies included in this run’s MARC: {summary["included_items"]}',
            f'- Pending manual identification: {known(summary["pending_manual"])}',
            f'- Items needing review: {known(summary["review_items"])}',
            f'- Review issues: {known(summary["issue_count"])}', '',
            '## Actions', '', '| Action | Items |', '|---|---:|',
            *[f'| {a["action"]} | {known(a["item_count"])} |' for a in actions], '',
            '## Review queue', '',
            'Open `review_queue.csv`. Priority 1: conflicts; 2: identification or '
            'shelving checks; 3: spot-checks and call-number differences.', '',
        ]
        for issue in db.execute(
            'SELECT code, priority, COUNT(*) AS n FROM issues WHERE run_id = ? '
            'GROUP BY code, priority ORDER BY priority, code', (run_id,)
        ):
            lines.append(f'- {issue["code"]}: {issue["n"]} item(s)')
        lines += ['', '## Call-number provenance', '']
        for source in db.execute(
            'SELECT * FROM call_number_sources WHERE run_id = ? ORDER BY source', (run_id,)
        ):
            lines.append(f'- {source["source"]}: {source["item_count"]} item(s)')
        if summary['error']:
            lines += ['', '## Pipeline failure', '', summary['error']]
        lines += [
            '', 'Counts describe this execution only. Shelf and final runs overlap; '
            'do not sum them as collection progress. MANUAL is an action, not proof '
            'that identification is still pending. MARC inclusion is not ILS import confirmation.',
            '', 'On early failure, item details and their derived counts may be incomplete. '
            'Call-number provenance records the resolver’s source label, not verified accuracy. '
            'The queue is advisory; make corrections through the existing workflow and rerun.', '',
        ]
        report_tmp.write_text('\n'.join(lines), encoding='utf-8')
        queue_tmp.replace(queue_path)
        report_tmp.replace(out_dir / 'run_report.md')
    finally:
        queue_tmp.unlink(missing_ok=True)
        report_tmp.unlink(missing_ok=True)

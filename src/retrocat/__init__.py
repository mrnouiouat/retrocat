"""retrocat: retrospective conversion cataloging.

Converts paired barcode-scanner output (ISBN + item barcode per book) into a
validated MARC21 ``.mrc`` file for bulk import into a library ILS, deduping
against an export of the existing catalog and emitting reconciliation reports
before any MARC file is written.
"""

__version__ = "0.1.0"

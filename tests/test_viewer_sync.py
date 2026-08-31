from __future__ import annotations

import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from codex_journal.viewer_sync import (
    CatalogSnapshot,
    SessionSnapshot,
    compare_snapshots,
    rebuild_search_index_atomic,
)


def item(fingerprint: str, entries: int, status: str, errors: int = 0) -> SessionSnapshot:
    return SessionSnapshot(fingerprint, entries, status, errors)


class ViewerSyncTests(unittest.TestCase):
    def test_change_summary_counts_new_changed_entries_lifecycle_and_errors(self) -> None:
        before = CatalogSnapshot(
            {
                "active": item("a", 2, "active"),
                "changed": item("b", 4, "completed", 1),
                "stable": item("c", 3, "completed"),
            }
        )
        after = CatalogSnapshot(
            {
                "active": item("aa", 5, "completed", 1),
                "changed": item("bb", 6, "completed", 2),
                "stable": item("c", 3, "completed"),
                "new": item("d", 7, "active", 2),
            }
        )
        summary = compare_snapshots(before, after)
        self.assertEqual(summary.new_sessions, 1)
        self.assertEqual(summary.changed_sessions, 2)
        self.assertEqual(summary.new_entries, 12)
        self.assertEqual(summary.lifecycle_changes, 1)
        self.assertEqual(summary.new_extraction_errors, 4)

    def test_unchanged_snapshot_has_stable_summary(self) -> None:
        snapshot = CatalogSnapshot({"stable": item("same", 3, "completed")})
        summary = compare_snapshots(snapshot, snapshot)
        self.assertFalse(summary.changed)
        self.assertEqual(summary.describe(), "No generated journal changes.")

    def test_reduced_entry_count_never_claims_negative_new_entries(self) -> None:
        before = CatalogSnapshot({"replaced": item("a", 10, "completed")})
        after = CatalogSnapshot({"replaced": item("b", 2, "incomplete")})
        summary = compare_snapshots(before, after)
        self.assertEqual(summary.changed_sessions, 1)
        self.assertEqual(summary.new_entries, 0)
        self.assertEqual(summary.lifecycle_changes, 1)

    def test_search_index_rebuild_replaces_destination_atomically(self) -> None:
        catalog = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "state" / "viewer.sqlite3"
            with patch("codex_journal.viewer_sync.JournalSearchIndex") as index_class, patch(
                "codex_journal.viewer_sync.os.replace", wraps=os.replace
            ) as replace:
                index_class.return_value.__enter__.return_value.rebuild.return_value = 42
                count = rebuild_search_index_atomic(catalog, destination)
            self.assertEqual(count, 42)
            self.assertTrue(destination.is_file())
            self.assertEqual(replace.call_count, 1)
            self.assertEqual(replace.call_args.args[1], destination)


if __name__ == "__main__":
    unittest.main()

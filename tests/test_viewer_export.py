from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_journal.viewer_annotations import AnnotationStore, AnnotationTarget
from codex_journal.viewer_activity import ActivityBucket, ActivityReport
from codex_journal.viewer_catalog import CatalogDetail, CatalogEntry, CatalogSession
from codex_journal.viewer_compare import compare_details
from codex_journal.viewer_export import (
    PRIVACY_WARNING,
    activity_document,
    comparison_document,
    include_private_notes,
    render_export,
    render_preview,
    selected_entries_document,
    write_export_atomic,
)


def detail() -> CatalogDetail:
    session = CatalogSession(
        "session-1", None, "completed", "2026-08-31T10:00:00Z", "2026-08-31T10:01:00Z",
        "Europe/Zurich", "~/PRIVATE/PATH", "Example/project", "main", "cli", "f" * 64,
        3, 1, 0, Path("journal/example.md"), Path("journal/example.provenance.json"),
    )
    entries = tuple(
        CatalogEntry(i, f"12:0{i}", f"Safe entry {i}.", 10 + i, f"2026-08-31T10:0{i}:00Z", "a" * 64, i == 1)
        for i in range(3)
    )
    return CatalogDetail(session, entries, ())


class ViewerExportTests(unittest.TestCase):
    def test_selected_and_range_exports_are_deterministic_and_sanitized(self) -> None:
        selected = selected_entries_document(detail(), {0, 2})
        ranged = selected_entries_document(detail(), {0, 2}, inclusive_range=True)
        self.assertEqual(len(selected.entries), 2)
        self.assertEqual(len(ranged.entries), 3)
        first = render_export(selected, "json")
        self.assertEqual(first, render_export(selected, "json"))
        payload = json.loads(first)
        self.assertEqual(payload["entries"][0]["text"], "Safe entry 0.")
        self.assertNotIn("working_directory", first.decode())
        self.assertNotIn("PRIVATE/PATH", first.decode())

    def test_notes_are_excluded_by_default_and_require_explicit_attachment(self) -> None:
        document = selected_entries_document(detail(), {0})
        with tempfile.TemporaryDirectory() as directory, AnnotationStore(
            Path(directory) / "annotations.db"
        ) as store:
            store.save_note(AnnotationTarget("session-1"), "PRIVATE SESSION NOTE")
            store.save_note(AnnotationTarget("session-1", 10), "PRIVATE ENTRY NOTE")
            self.assertNotIn("PRIVATE", render_export(document, "markdown").decode())
            included = include_private_notes(document, store)
            rendered = render_export(included, "markdown").decode()
            self.assertIn("PRIVATE SESSION NOTE", rendered)
            self.assertIn("PRIVATE ENTRY NOTE", rendered)

    def test_preview_lists_exact_scope_counts_and_warning(self) -> None:
        preview = render_preview(selected_entries_document(detail(), {1}))
        self.assertIn(PRIVACY_WARNING, preview)
        self.assertIn("Sessions (1)", preview)
        self.assertIn("Entries (1)", preview)
        self.assertIn("session-1 / entry 1 / sequence 11", preview)
        self.assertIn("Private notes (0)", preview)

    def test_comparison_and_activity_scopes_export_exact_generated_references(self) -> None:
        left = detail()
        right = CatalogDetail(
            replace(left.session, session_id="session-2"),
            (replace(left.entries[0], text="Contradictory replacement."),),
            (),
        )
        comparison = comparison_document(compare_details(left, right))
        self.assertEqual(len(comparison.sessions), 2)
        self.assertTrue(comparison.comparison)
        self.assertIn("left only", render_export(comparison, "markdown").decode())

        bucket = ActivityBucket(
            "2026-08-31",
            "2026-08-31",
            "2026-08-31",
            ("session-1",),
            (("session-1", "completed"),),
            (("session-1", "Example/project"),),
            (("session-1", 0),),
            (("completed", 1),),
            (("Example/project", 1),),
            (("test", 1),),
        )
        activity = activity_document(ActivityReport((bucket,), (), ()), (left,))
        preview = render_preview(activity)
        self.assertIn("activity 2026-08-31: sessions=session-1; entries=session-1:0", preview)
        self.assertIn("Activity buckets: 1", preview)

    def test_markdown_escapes_links_images_headings_and_raw_html(self) -> None:
        source = detail()
        unsafe = CatalogEntry(
            0,
            "12:00",
            "# [link](https://example.invalid) ![image](https://example.invalid/x) <script>",
            10,
            "2026-08-31T10:00:00Z",
            "a" * 64,
            False,
        )
        document = selected_entries_document(CatalogDetail(source.session, (unsafe,), ()), {0})
        markdown = render_export(document, "markdown").decode()
        self.assertIn(r"\# \[link\](https://example.invalid)", markdown)
        self.assertIn(r"!\[image\](https://example.invalid/x)", markdown)
        self.assertIn(r"\<script\>", markdown)

    def test_atomic_write_requires_absolute_destination_and_overwrite_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "export.md"
            with patch("codex_journal.viewer_export.os.replace", wraps=os.replace) as replace:
                write_export_atomic(destination, b"first\n")
            replace.assert_called_once()
            self.assertEqual(destination.read_bytes(), b"first\n")
            with self.assertRaises(FileExistsError):
                write_export_atomic(destination, b"second\n")
            write_export_atomic(destination, b"second\n", overwrite=True)
            self.assertEqual(destination.read_bytes(), b"second\n")
            with self.assertRaises(ValueError):
                write_export_atomic(Path("relative.md"), b"no\n")

    def test_write_failure_leaves_no_partial_target_or_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "export.json"
            with patch("codex_journal.viewer_export.os.replace", side_effect=OSError("failure")):
                with self.assertRaises(OSError):
                    write_export_atomic(destination, b"content")
            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
    activity_document,
    comparison_document,

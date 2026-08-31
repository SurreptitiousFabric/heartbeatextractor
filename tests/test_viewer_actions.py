from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_journal.viewer_actions import (
    ProjectPathError,
    copy_one_entry,
    copy_selected_range,
    project_directory_uri,
    resolve_project_directory,
)
from codex_journal.viewer_catalog import CatalogEntry


def entry(index: int, text: str) -> CatalogEntry:
    return CatalogEntry(index, f"12:{index:02d}", text, index, "2026-08-31T10:00:00Z", "a" * 64, False)


class ViewerActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        self.project = self.home / "src" / "project"
        self.project.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_project_path_is_canonical_local_and_inside_home(self) -> None:
        self.assertEqual(
            resolve_project_directory("~/src/project", home=self.home),
            self.project.resolve(),
        )
        self.assertEqual(
            project_directory_uri(str(self.project), home=self.home),
            self.project.resolve().as_uri(),
        )

    def test_missing_relative_outside_and_symlink_escape_fail_closed(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.home / "escape").symlink_to(outside, target_is_directory=True)
        for unsafe in (None, "relative/path", str(outside), "~/missing", "~/escape"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ProjectPathError):
                resolve_project_directory(unsafe, home=self.home)

    def test_copy_one_uses_only_sanitized_visible_text_and_timestamp(self) -> None:
        payload = copy_one_entry(entry(0, "Reviewed [REDACTED] token."))
        self.assertEqual(payload.entry_count, 1)
        self.assertEqual(
            payload.text,
            "1 sanitized journal entry\n12:00  Reviewed [REDACTED] token.\n",
        )

    def test_copy_range_is_inclusive_and_treats_markup_as_plain_text(self) -> None:
        entries = (
            entry(0, "First."),
            entry(1, "<b>Literal, not markup.</b>"),
            entry(2, "Third."),
            entry(3, "Fourth."),
        )
        payload = copy_selected_range(entries, {1, 3})
        self.assertEqual(payload.entry_count, 3)
        self.assertIn("12:01  <b>Literal, not markup.</b>", payload.text)
        self.assertIn("12:02  Third.", payload.text)
        self.assertIn("12:03  Fourth.", payload.text)

    def test_copy_range_requires_selection(self) -> None:
        with self.assertRaisesRegex(ValueError, "No timeline entries"):
            copy_selected_range((entry(0, "First."),), set())


if __name__ == "__main__":
    unittest.main()

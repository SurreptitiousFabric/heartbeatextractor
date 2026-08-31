from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_journal.viewer_annotations import AnnotationStore, AnnotationTarget


class ViewerAnnotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.generated = self.root / "journal" / "session.md"
        self.generated.parent.mkdir()
        self.generated.write_bytes(b"generated journal\n")
        self.original = self.generated.read_bytes()
        self.store = AnnotationStore(self.root / "state" / "annotations.db")
        self.session = AnnotationTarget("session-1")
        self.entry = AnnotationTarget("session-1", 42)

    def tearDown(self) -> None:
        self.store.close()
        self.assertEqual(self.generated.read_bytes(), self.original)
        self.temp.cleanup()

    def test_session_and_entry_bookmarks_toggle_list_and_filter(self) -> None:
        self.assertTrue(self.store.toggle_bookmark(self.session))
        self.assertTrue(self.store.toggle_bookmark(self.entry))
        self.assertEqual(len(self.store.list_bookmarks()), 2)
        self.assertEqual(self.store.bookmarked_session_ids(), frozenset({"session-1"}))
        self.assertFalse(self.store.toggle_bookmark(self.entry))
        self.assertEqual(len(self.store.list_bookmarks()), 1)

    def test_notes_create_edit_list_and_transactional_delete(self) -> None:
        created = self.store.save_note(self.session, " First private note. ")
        self.assertEqual(created.text, "First private note.")
        edited = self.store.save_note(self.session, "Edited private note.")
        self.assertEqual(self.store.get_note(self.session), edited)
        self.store.save_note(self.entry, "Exact entry note.")
        self.assertEqual(len(self.store.list_notes()), 2)
        self.assertTrue(self.store.delete_note(self.session))
        self.assertIsNone(self.store.get_note(self.session))
        self.assertFalse(self.store.delete_note(self.session))

    def test_empty_oversized_and_unknown_preference_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save_note(self.session, "   ")
        with self.assertRaises(ValueError):
            self.store.save_note(self.session, "x" * (64 * 1024 + 1))
        with self.assertRaises(ValueError):
            self.store.set_preference("remote", "yes")

    def test_preferences_are_private_and_separate(self) -> None:
        self.store.set_preference("theme", "dark")
        self.store.set_preference("sync_on_launch", "true")
        self.assertEqual(self.store.get_preference("theme", "system"), "dark")
        self.assertEqual(self.store.get_preference("periodic_sync", "false"), "false")
        self.assertNotIn(b"dark", self.generated.read_bytes())

    def test_note_content_is_not_written_outside_annotation_database(self) -> None:
        sentinel = "PRIVATE_NOTE_MUST_STAY_LOCAL"
        self.store.save_note(self.entry, sentinel)
        files_with_sentinel = [
            path for path in self.root.rglob("*") if path.is_file() and sentinel.encode() in path.read_bytes()
        ]
        self.assertEqual(files_with_sentinel, [self.root / "state" / "annotations.db"])

    def test_invalid_targets_fail_closed(self) -> None:
        for target in (AnnotationTarget(""), AnnotationTarget("x" * 257), AnnotationTarget("ok", -2)):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "invalid"):
                self.store.toggle_bookmark(target)

    def test_corrupt_and_symlinked_annotation_state_fail_closed(self) -> None:
        corrupt = self.root / "corrupt.db"
        corrupt.write_bytes(b"not a sqlite database")
        with self.assertRaisesRegex(ValueError, "malformed"):
            AnnotationStore(corrupt)
        link = self.root / "link.db"
        link.symlink_to(self.store.path)
        with self.assertRaisesRegex(ValueError, "symbolic-link"):
            AnnotationStore(link)


if __name__ == "__main__":
    unittest.main()

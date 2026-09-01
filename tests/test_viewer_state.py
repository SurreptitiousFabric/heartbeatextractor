from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_journal.viewer_state import ViewerState, ViewerStateStore
from codex_journal.viewer_state import MAX_VIEWER_STATE_BYTES


class ViewerStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state" / "viewer-state.json"
        self.store = ViewerStateStore(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_or_malformed_state_fails_closed_to_defaults(self) -> None:
        self.assertEqual(self.store.load(), ViewerState())
        self.path.parent.mkdir()
        self.path.write_text("not json", encoding="utf-8")
        self.assertEqual(self.store.load(), ViewerState())

    def test_safe_ui_state_round_trips_without_search_or_journal_text(self) -> None:
        state = ViewerState(
            selected_session_id="session-1",
            filters={"project": "Example/repo", "redacted_only": True},
            window_width=1200,
            window_height=800,
            content_visible=True,
            timeline_entry_index=17,
            timeline_density="compact",
        )
        self.store.save(state)
        self.assertEqual(self.store.load(), state)
        body = self.path.read_text(encoding="utf-8")
        self.assertNotIn("search", body)
        self.assertNotIn("journal_text", body)

    def test_unknown_fields_filters_and_theme_fail_closed(self) -> None:
        self.path.parent.mkdir()
        self.path.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "theme": "remote-theme",
                    "sync_on_launch": "yes",
                    "periodic_sync": 1,
                    "filters": {"raw_source": "private", "status": "completed"},
                    "window_width": 1,
                    "timeline_entry_index": -1,
                    "timeline_density": "microscopic",
                }
            ),
            encoding="utf-8",
        )
        loaded = self.store.load()
        self.assertEqual(loaded.filters, {"status": "completed"})
        self.assertEqual(loaded.window_width, 1180)
        self.assertEqual(loaded.timeline_entry_index, 0)
        self.assertEqual(loaded.timeline_density, "comfortable")

    def test_save_uses_atomic_replace(self) -> None:
        with patch("codex_journal.viewer_state.os.replace", wraps=__import__("os").replace) as replace:
            self.store.save(ViewerState())
        replace.assert_called_once()
        self.assertEqual(replace.call_args.args[1], self.path)

    def test_oversized_and_symlinked_state_fail_closed(self) -> None:
        self.path.parent.mkdir()
        self.path.write_bytes(b"x" * (MAX_VIEWER_STATE_BYTES + 1))
        self.assertEqual(self.store.load(), ViewerState())
        self.path.unlink()
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text('{"format_version": 1, "selected_session_id": "outside"}', encoding="utf-8")
        self.path.symlink_to(outside)
        self.assertEqual(self.store.load(), ViewerState())
        with self.assertRaisesRegex(ValueError, "symbolic-link"):
            self.store.save(ViewerState())


if __name__ == "__main__":
    unittest.main()

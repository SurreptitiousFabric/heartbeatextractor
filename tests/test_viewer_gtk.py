from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
import warnings
from dataclasses import dataclass
from pathlib import Path

from codex_journal.engine import JournalEngine
from codex_journal.viewer import ViewerUnavailable, load_gtk
from codex_journal.viewer_tags import classify_entry


FIXTURES = Path(__file__).parent / "fixtures"
GTK_REASON = "GTK viewer dependency or graphical display unavailable"
warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    Adw, Gio, GLib, Gtk = load_gtk()
    GTK_AVAILABLE = bool(
        (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))
        and Gtk.init_check()
    )
    if GTK_AVAILABLE:
        from codex_journal.viewer_ui import JournalWindow
except ViewerUnavailable:
    GTK_AVAILABLE = False


@dataclass
class ViewerHarness:
    journal: object

    def action(self, name: str) -> object:
        action = self.journal.window.lookup_action(name)
        if action is None:
            raise AssertionError(f"missing window action: {name}")
        return action

    def activate(self, name: str) -> None:
        self.action(name).activate(None)

    def select_session(self, index: int) -> None:
        sessions = self.journal.browser.session_list
        sessions.select_row(sessions.get_row_at_index(index))

    def close(self) -> None:
        self.journal.close()
        self.journal.window.destroy()


@unittest.skipUnless(GTK_AVAILABLE, GTK_REASON)
class ViewerGtkTests(unittest.TestCase):
    counter = 0

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.state = self.root / "codex-state"
        type(self).counter += 1
        self.app = Adw.Application(
            application_id=(
                f"com.surreptitiousfabric.HeartbeatExtractor.Test{type(self).counter}"
            ),
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.assertTrue(self.app.register())
        self.harness: ViewerHarness | None = None

    def tearDown(self) -> None:
        if self.harness is not None:
            self.harness.close()
        self.app.quit()
        self._drain()
        self.temp.cleanup()

    def _fixture(self, name: str) -> Path:
        target = self.state / "sessions" / "2026" / "08" / "31" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / name, target)
        return target

    def _generate(self) -> None:
        JournalEngine(self.repo, self.state, home=Path("/home/tester")).sync(
            timezone_name="Europe/Zurich"
        )

    def _open(self) -> ViewerHarness:
        window = JournalWindow(
            self.app, self.repo, self.state, (Adw, Gio, GLib, Gtk)
        )
        window.present()
        self.assertTrue(self._spin_until(lambda: window.ready))
        self.harness = ViewerHarness(window)
        return self.harness

    def _spin_until(self, condition: object, timeout: float = 8.0) -> bool:
        deadline = time.monotonic() + timeout
        context = GLib.MainContext.default()
        while time.monotonic() < deadline:
            if context.pending():
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    context.iteration(False)
            if condition():
                return True
            time.sleep(0.01)
        return False

    def _drain(self) -> None:
        context = GLib.MainContext.default()
        for _index in range(10):
            if not context.pending():
                break
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                context.iteration(False)

    def test_actions_selection_and_comparison_use_real_widgets(self) -> None:
        self._fixture("normal_completed.jsonl")
        self._fixture("subagent.jsonl")
        self._generate()
        viewer = self._open()
        for action in (
            "previous-session",
            "next-session",
            "next-entry",
            "focus-search",
            "refresh",
            "sync",
            "bookmark",
            "compare",
            "activity",
            "export",
            "help",
        ):
            self.assertIsNotNone(viewer.action(action), action)
        self.assertEqual(len(viewer.journal.browser.model.sessions), 2)
        viewer.select_session(1)
        self._drain()
        viewer.activate("compare")
        self.assertIsNotNone(viewer.journal.comparison.report)
        self.assertEqual(len(viewer.journal.comparison.recent_session_ids), 2)

    def test_reworked_navigation_exposes_context_filters_and_reliable_help(self) -> None:
        self._fixture("normal_completed.jsonl")
        self._generate()
        viewer = self._open()
        window = viewer.journal
        session = window.browser.model.selected
        self.assertIsNotNone(session)
        self.assertEqual(window.shortcuts_button.get_label(), "Keyboard shortcuts")
        self.assertGreaterEqual(window.timeline.more_actions_button.get_menu_model().get_n_items(), 6)
        self.assertEqual(window.main_title.get_title(), session.project)
        self.assertIn("Europe/Zurich", session.rendered_timezone)
        self.assertIn("Displaying 1 generated journal", window.sync.status.get_label())
        self.assertTrue(window.browser.summary(session.session_id))

        project_filter = window.browser.filter_widget("project")
        project_filter.set_selected(1)
        self._drain()
        self.assertTrue(window.browser.clear_filters_button.get_visible())
        self.assertIn("active filter", window.browser.filter_status.get_label())
        window.browser.clear_filters()
        self.assertEqual(project_filter.get_selected(), 0)
        self.assertFalse(window.browser.clear_filters_button.get_visible())

    def test_selection_density_and_provenance_behaviors_are_explicit(self) -> None:
        self._fixture("normal_completed.jsonl")
        self._generate()
        viewer = self._open()
        timeline = viewer.journal.timeline
        self.assertTrue(timeline.indexes)
        first_index = min(timeline.indexes)
        first_row = timeline.row(first_index)
        first_check = timeline.selection_checkbox(first_index)
        self.assertFalse(first_row.get_expanded())
        self.assertFalse(first_check.get_visible())

        timeline.set_selection_mode(True)
        self.assertTrue(timeline.selection_bar.get_reveal_child())
        self.assertTrue(first_check.get_visible())
        first_check.set_active(True)
        self._drain()
        self.assertEqual(timeline.selection_count.get_label(), "1 selected")
        self.assertTrue(timeline.copy_selection_button.get_sensitive())
        timeline.set_selection_mode(False)
        self.assertFalse(first_check.get_visible())
        self.assertEqual(timeline.selection_count.get_label(), "0 selected")

        timeline.density_dropdown.set_selected(1)
        self._drain()
        compact_row = timeline.row(min(timeline.indexes))
        self.assertEqual(compact_row.get_label_widget().get_margin_top(), 5)
        tagged_index = next(
            entry.index for entry in timeline.detail.entries if classify_entry(entry.text)
        )
        tagged_body = timeline.row(tagged_index).get_label_widget().get_last_child()
        self.assertIsInstance(tagged_body.get_last_child(), Gtk.FlowBox)
        viewer.activate("next-entry")
        self.assertFalse(timeline.row(timeline.current_entry_index).get_expanded())

    def test_empty_and_malformed_generated_states_are_intentional(self) -> None:
        (self.repo / "journal").mkdir(parents=True)
        viewer = self._open()
        self.assertEqual(viewer.journal.visible_state, "empty")
        viewer.close()
        self.harness = None

        malformed = self.repo / "journal" / "bad.md"
        malformed.write_text("not generated metadata\n", encoding="utf-8")
        viewer = self._open()
        self.assertEqual(viewer.journal.visible_state, "error")
        self.assertTrue(viewer.journal.browser.catalog.diagnostics)

    def test_async_sync_completes_without_blocking_widget_input(self) -> None:
        self._fixture("active_append.jsonl")
        self._generate()
        viewer = self._open()
        sync = viewer.journal.sync
        self.assertFalse(sync.running)
        viewer.activate("sync")
        self.assertTrue(sync.running)
        self.assertFalse(sync.start())
        viewer.journal.browser.search_entry.set_text("safe query while syncing")
        self.assertEqual(
            viewer.journal.browser.search_entry.get_text(), "safe query while syncing"
        )
        self.assertTrue(self._spin_until(lambda: not sync.running))
        self.assertIsNotNone(sync.last_sync_at)
        self.assertIn("discovered=1", sync.last_sync_summary)


if __name__ == "__main__":
    unittest.main()

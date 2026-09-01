from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
import warnings
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
        self.window: JournalWindow | None = None

    def tearDown(self) -> None:
        if self.window is not None:
            self.window._on_close()
            self.window.window.destroy()
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

    def _open(self) -> JournalWindow:
        self.window = JournalWindow(
            self.app, self.repo, self.state, (Adw, Gio, GLib, Gtk)
        )
        self.window.present()
        self.assertTrue(
            self._spin_until(
                lambda: self.window._state_restored and not self.window._loading
            )
        )
        return self.window

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
        window = self._open()
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
            self.assertIsNotNone(window.window.lookup_action(action), action)
        self.assertEqual(len(window.model.sessions), 2)
        window.session_list.select_row(window.session_list.get_row_at_index(1))
        self._drain()
        window._compare_recent()
        self.assertIsNotNone(window._comparison_report)
        self.assertEqual(len(window._recent_session_ids), 2)

    def test_reworked_navigation_exposes_context_filters_and_reliable_help(self) -> None:
        self._fixture("normal_completed.jsonl")
        self._generate()
        window = self._open()
        session = window.model.selected
        self.assertIsNotNone(session)
        self.assertEqual(window.shortcuts_button.get_label(), "Keyboard shortcuts")
        self.assertGreaterEqual(window.more_actions_button.get_menu_model().get_n_items(), 6)
        self.assertEqual(window.main_title.get_title(), session.project)
        self.assertIn("Europe/Zurich", session.rendered_timezone)
        self.assertIn("Displaying 1 generated journal", window.sync_status.get_label())
        self.assertTrue(window._session_summaries[session.session_id])

        project_filter = window._filter_widgets["project"]
        project_filter.set_selected(1)
        self._drain()
        self.assertTrue(window.clear_filters_button.get_visible())
        self.assertIn("active filter", window.filter_status.get_label())
        window._clear_filters()
        self.assertEqual(project_filter.get_selected(), 0)
        self.assertFalse(window.clear_filters_button.get_visible())

    def test_selection_density_and_provenance_behaviors_are_explicit(self) -> None:
        self._fixture("normal_completed.jsonl")
        self._generate()
        window = self._open()
        self.assertTrue(window._timeline_widgets)
        first_index = min(window._timeline_widgets)
        first_row = window._timeline_widgets[first_index]
        first_check = window._selection_checks[first_index]
        self.assertFalse(first_row.get_expanded())
        self.assertFalse(first_check.get_visible())

        window._set_selection_mode(True)
        self.assertTrue(window.selection_bar.get_reveal_child())
        self.assertTrue(first_check.get_visible())
        first_check.set_active(True)
        self._drain()
        self.assertEqual(window.selection_count.get_label(), "1 selected")
        self.assertTrue(window.copy_selection_button.get_sensitive())
        window._set_selection_mode(False)
        self.assertFalse(first_check.get_visible())
        self.assertEqual(window.selection_count.get_label(), "0 selected")

        window.density_dropdown.set_selected(1)
        self._drain()
        compact_row = window._timeline_widgets[min(window._timeline_widgets)]
        self.assertEqual(compact_row.get_label_widget().get_margin_top(), 5)
        tagged_index = next(
            entry.index for entry in window.current_detail.entries if classify_entry(entry.text)
        )
        tagged_body = window._timeline_widgets[tagged_index].get_label_widget().get_last_child()
        self.assertIsInstance(tagged_body.get_last_child(), Gtk.FlowBox)
        window._move_entry(1)
        self.assertFalse(window._timeline_widgets[window.current_entry_index].get_expanded())

    def test_empty_and_malformed_generated_states_are_intentional(self) -> None:
        (self.repo / "journal").mkdir(parents=True)
        window = self._open()
        self.assertEqual(window.main_stack.get_visible_child_name(), "empty")
        window._on_close()
        window.window.destroy()
        self.window = None

        malformed = self.repo / "journal" / "bad.md"
        malformed.write_text("not generated metadata\n", encoding="utf-8")
        window = self._open()
        self.assertEqual(window.main_stack.get_visible_child_name(), "error")
        self.assertTrue(window.catalog.diagnostics)

    def test_async_sync_completes_without_blocking_widget_input(self) -> None:
        self._fixture("active_append.jsonl")
        self._generate()
        window = self._open()
        self.assertFalse(window._sync_running)
        window._start_sync()
        self.assertTrue(window._sync_running)
        self.assertFalse(window._start_sync())
        window.search_entry.set_text("safe query while syncing")
        self.assertEqual(window.search_entry.get_text(), "safe query while syncing")
        self.assertTrue(self._spin_until(lambda: not window._sync_running))
        self.assertIsNotNone(window.last_sync_at)
        self.assertIn("discovered=1", window.last_sync_summary)


if __name__ == "__main__":
    unittest.main()

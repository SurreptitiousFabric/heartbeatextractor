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

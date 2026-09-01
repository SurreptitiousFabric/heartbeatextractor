from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
UI_MODULES = tuple(sorted((ROOT / "src" / "codex_journal").glob("viewer_ui*.py")))


class ViewerArchitectureTests(unittest.TestCase):
    def test_ui_modules_and_total_remain_bounded(self) -> None:
        line_counts = {
            path.name: len(path.read_text(encoding="utf-8").splitlines())
            for path in UI_MODULES
        }
        self.assertLess(line_counts["viewer_ui.py"], 800)
        self.assertTrue(all(count <= 700 for count in line_counts.values()), line_counts)
        self.assertLessEqual(sum(line_counts.values()), 2172)

    def test_window_shell_has_bounded_state_and_methods(self) -> None:
        tree = ast.parse((ROOT / "src" / "codex_journal" / "viewer_ui.py").read_text())
        shell = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "JournalWindow"
        )
        methods = [node.name for node in shell.body if isinstance(node, ast.FunctionDef)]
        attributes = {
            node.attr
            for node in ast.walk(shell)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }
        self.assertLessEqual(len(methods), 35)
        self.assertLessEqual(len(attributes), 35)
        for feature_method in ("_render_session", "_start_sync", "_compare_recent", "_open_export_preview"):
            self.assertNotIn(feature_method, methods)

    def test_gtk_tests_use_the_shared_harness_not_window_internals(self) -> None:
        body = (ROOT / "tests" / "test_viewer_gtk.py").read_text(encoding="utf-8")
        self.assertIn("class ViewerHarness", body)
        self.assertNotIn("window._", body)
        self.assertNotIn("journal._", body)


if __name__ == "__main__":
    unittest.main()

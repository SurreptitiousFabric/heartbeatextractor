from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class DocumentationAndPackagingTests(unittest.TestCase):
    def test_readme_is_mise_first_and_documents_every_primary_workflow(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        required = (
            "mise run bootstrap",
            "codex-journal view",
            "Sync on launch",
            "five-minute refresh",
            "bookmarks",
            "private notes",
            "comparison",
            "activity",
            "Markdown or JSON export",
            "Review every generated journal",
        )
        for value in required:
            self.assertIn(value, readme)
        self.assertNotIn("python3 -m venv", readme)
        self.assertNotIn("/usr/bin/python -m", readme)

    def test_architecture_and_viewer_docs_record_privacy_and_shortcuts(self) -> None:
        architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(
            encoding="utf-8"
        )
        viewer = (ROOT / "docs" / "VIEWER.md").read_text(encoding="utf-8")
        for excluded in ("prompt", "hidden reasoning", "tool output"):
            self.assertIn(excluded, architecture)
        for shortcut in ("Ctrl+Page Up", "Ctrl+R", "Ctrl+E", "Ctrl+Shift+/"):
            self.assertIn(shortcut, viewer)
        self.assertIn("notes are never full-text indexed", viewer)
        self.assertIn("not a daemon", viewer)

    def test_core_dependencies_remain_empty_and_viewer_is_optional(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as source:
            project = tomllib.load(source)["project"]
        self.assertEqual(project["dependencies"], [])
        self.assertEqual(
            project["optional-dependencies"]["viewer"], ["PyGObject>=3.54,<4"]
        )

    def test_public_docs_contain_no_real_home_or_state_root(self) -> None:
        public_docs = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "docs").glob("*.md")
        ) + (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn(str(Path.home()), public_docs)
        self.assertNotIn(str(Path.home() / ".codex"), public_docs)


if __name__ == "__main__":
    unittest.main()

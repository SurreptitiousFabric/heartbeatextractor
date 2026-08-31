from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from codex_journal.engine import JournalEngine
from codex_journal.viewer_annotations import AnnotationStore, AnnotationTarget
from codex_journal.viewer_catalog import JournalCatalog, JournalSearchIndex
from codex_journal.viewer_export import (
    include_private_notes,
    render_export,
    render_preview,
    selected_entries_document,
)


FIXTURES = Path(__file__).parent / "fixtures"


class ViewerPrivacyBoundaryTests(unittest.TestCase):
    def test_raw_source_classes_never_cross_the_generated_data_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            state_root = root / "codex-state"
            source = state_root / "sessions" / "2026" / "08" / "31" / "normal.jsonl"
            source.parent.mkdir(parents=True)
            shutil.copyfile(FIXTURES / "normal_completed.jsonl", source)

            JournalEngine(repository, state_root, home=Path("/home/tester")).sync(
                timezone_name="Europe/Zurich"
            )
            catalog = JournalCatalog(repository)
            catalog.refresh()
            detail = catalog.load_detail(catalog.sessions[0].session_id)

            database = repository / "state" / "viewer.sqlite3"
            with JournalSearchIndex(database) as index:
                index.rebuild(catalog)
                search_text = "\n".join(hit.text for hit in index.search(""))

            private_note = "PRIVATE NOTE IS OPT-IN ONLY"
            with AnnotationStore(repository / "state" / "annotations.db") as annotations:
                annotations.save_note(AnnotationTarget(detail.session.session_id), private_note)
                document = selected_entries_document(
                    detail, {entry.index for entry in detail.entries}
                )
                default_export = render_export(document, "json").decode("utf-8")
                opted_in = include_private_notes(document, annotations)
                opted_in_export = render_export(opted_in, "json").decode("utf-8")

            visible_outputs = "\n".join(
                (
                    *(entry.text for entry in detail.entries),
                    search_text,
                    default_export,
                    render_preview(document),
                )
            )
            forbidden = (
                "PRIVATE USER PROMPT MUST NOT APPEAR",
                "HIDDEN REASONING MUST NOT APPEAR",
                "unsafe command arguments",
                "RAW TOOL OUTPUT MUST NOT APPEAR",
                "COMPLETE FINAL RESPONSE MUST NOT APPEAR",
                "/home/tester/.codex",
                private_note,
            )
            for sentinel in forbidden:
                self.assertNotIn(sentinel, visible_outputs)
                self.assertNotIn(sentinel.encode(), database.read_bytes())
            self.assertIn(private_note, opted_in_export)


if __name__ == "__main__":
    unittest.main()

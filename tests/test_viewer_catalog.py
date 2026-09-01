from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from codex_journal.engine import JournalEngine
from codex_journal.viewer_annotations import AnnotationStore, AnnotationTarget
from codex_journal.viewer_catalog import (
    CatalogError,
    JournalCatalog,
    JournalSearchIndex,
)
from codex_journal.viewer_model import SessionBrowserModel
from codex_journal.viewer_tags import classify_entry


FIXTURES = Path(__file__).parent / "fixtures"


class ViewerCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.source = self.root / "codex-state"
        (self.repo / "journal").mkdir(parents=True)
        (self.repo / "projects").mkdir()
        (self.repo / "state").mkdir()
        (self.repo / "INDEX.md").write_text("# Codex session journals\n", encoding="utf-8")
        target = self.source / "sessions" / "2026" / "08" / "31"
        target.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "normal_completed.jsonl", target / "normal.jsonl")
        shutil.copyfile(FIXTURES / "subagent.jsonl", target / "subagent.jsonl")
        JournalEngine(self.repo, self.source, home=Path("/home/tester")).sync(timezone_name="Europe/Zurich")
        self.catalog = JournalCatalog(self.repo)
        self.catalog.refresh()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_metadata_is_eager_but_timelines_are_lazy(self) -> None:
        self.assertEqual(len(self.catalog.sessions), 2)
        self.assertFalse(self.catalog.diagnostics)
        self.assertEqual(self.catalog._details, {})
        session = self.catalog.sessions[0]
        self.assertIn(session.source_kind, {"cli", "subagent"})
        detail = self.catalog.load_detail(session.session_id)
        self.assertEqual(len(detail.entries), session.entry_count)
        self.assertIn(session.session_id, self.catalog._details)

    def test_parent_child_and_project_indexes_use_generated_metadata(self) -> None:
        parent = next(session for session in self.catalog.sessions if self.catalog.children_of(session.session_id))
        children = self.catalog.children_of(parent.session_id)
        self.assertTrue(children)
        self.assertTrue(all(self.catalog.parent_of(child.session_id) == parent for child in children))
        self.assertTrue(self.catalog.projects)

    def test_search_index_contains_only_sanitized_generated_text(self) -> None:
        raw_secret = "RAW_PROMPT_MUST_NEVER_ENTER_VIEWER"
        (self.source / "unrelated-private-record.jsonl").write_text(raw_secret, encoding="utf-8")
        database = self.repo / "state" / "viewer.sqlite3"
        with JournalSearchIndex(database) as index:
            count = index.rebuild(self.catalog)
            self.assertGreater(count, 0)
            first = self.catalog.load_detail(self.catalog.sessions[0].session_id).entries[0]
            term = first.text.split()[0].strip(".,:;!?")
            self.assertTrue(index.search(term))
            self.assertFalse(index.search(raw_secret))
        self.assertNotIn(raw_secret.encode(), database.read_bytes())

    def test_session_summaries_use_first_sanitized_indexed_entry(self) -> None:
        with JournalSearchIndex(self.root / "summary.sqlite3") as index:
            index.rebuild(self.catalog)
            summaries = index.session_summaries()
        for session in self.catalog.sessions:
            detail = self.catalog.load_detail(session.session_id)
            if detail.entries:
                self.assertEqual(summaries[session.session_id], detail.entries[0].text)

    def test_search_schema_contains_only_text_tag_and_navigation_fields(self) -> None:
        with JournalSearchIndex(self.root / "schema.sqlite3") as index:
            columns = tuple(
                row[1] for row in index.connection.execute("PRAGMA table_info(journal_search)")
            )
            version = index.connection.execute(
                "SELECT value FROM viewer_index_meta WHERE key = 'schema_version'"
            ).fetchone()
        self.assertEqual(columns, ("session_id", "entry_index", "timestamp_utc", "text", "tags"))
        self.assertEqual(version, ("3",))

    def test_search_tags_compose_with_authoritative_browser_metadata_filters(self) -> None:
        database = self.repo / "state" / "viewer.sqlite3"
        tagged = next(
            (
                (session, entry)
                for session in self.catalog.sessions
                for entry in self.catalog.load_detail(session.session_id).entries
                if classify_entry(entry.text)
            ),
            None,
        )
        self.assertIsNotNone(tagged)
        assert tagged is not None
        session, entry = tagged
        tag = classify_entry(entry.text)[0]
        with JournalSearchIndex(database) as index:
            index.rebuild(self.catalog)
            hits = index.search("", tags=(tag,))
        self.assertTrue(hits)
        browser = SessionBrowserModel(self.catalog)
        browser.set_search_hits(hits, active=True)
        browser.set_filter("project", session.project)
        browser.set_filter("status", session.status)
        browser.set_filter("date_from", session.local_date)
        browser.set_filter("date_to", session.local_date)
        self.assertTrue(browser.sessions)
        hit_ids = {hit.session_id for hit in hits}
        self.assertTrue(all(item.session_id in hit_ids for item in browser.sessions))
        self.assertTrue(all(item.project == session.project for item in browser.sessions))

    def test_index_rebuild_does_not_fill_lazy_detail_cache(self) -> None:
        self.catalog._details.clear()
        with JournalSearchIndex(self.repo / "state" / "viewer.sqlite3") as index:
            self.assertGreater(index.rebuild(self.catalog), 0)
        self.assertEqual(self.catalog._details, {})

    def test_private_notes_are_not_indexed(self) -> None:
        sentinel = "PRIVATE_NOTE_NOT_SEARCHABLE"
        with AnnotationStore(self.repo / "state" / "annotations.db") as annotations:
            annotations.save_note(AnnotationTarget(self.catalog.sessions[0].session_id), sentinel)
        with JournalSearchIndex(self.repo / "state" / "viewer.sqlite3") as index:
            index.rebuild(self.catalog)
            self.assertFalse(index.search(sentinel))

    def test_unknown_search_tag_fails_closed(self) -> None:
        with JournalSearchIndex(self.repo / "state" / "viewer.sqlite3") as index:
            index.rebuild(self.catalog)
            with self.assertRaisesRegex(CatalogError, "unknown deterministic"):
                index.search("", tags=("invented",))

    def test_corrupt_and_symlinked_search_state_fail_closed(self) -> None:
        corrupt = self.repo / "state" / "corrupt.sqlite3"
        corrupt.write_bytes(b"not a sqlite database")
        with self.assertRaisesRegex(CatalogError, "unavailable or malformed"):
            JournalSearchIndex(corrupt)
        target = self.repo / "state" / "target.sqlite3"
        target.touch()
        link = self.repo / "state" / "link.sqlite3"
        link.symlink_to(target)
        with self.assertRaisesRegex(CatalogError, "symbolic-link"):
            JournalSearchIndex(link)

    def test_malformed_generated_artifact_fails_closed(self) -> None:
        malformed = self.repo / "journal" / "bad.md"
        malformed.write_text("---\nsession_id: not-json\n---\nprivate body\n", encoding="utf-8")
        self.catalog.refresh()
        self.assertEqual(len(self.catalog.sessions), 2)
        self.assertTrue(any("bad.md" in diagnostic and "malformed metadata" in diagnostic for diagnostic in self.catalog.diagnostics))

    def test_detail_size_limit_is_enforced(self) -> None:
        session = self.catalog.sessions[0]
        limited = JournalCatalog(self.repo, max_journal_bytes=1)
        limited.refresh()
        with self.assertRaisesRegex(CatalogError, "generated journal exceeds size limit"):
            limited.load_detail(session.session_id)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from codex_journal.engine import JournalEngine
from codex_journal.viewer_catalog import JournalCatalog
from codex_journal.viewer_catalog import SearchHit
from codex_journal.viewer_model import ALL, SessionBrowserModel, display_start, session_badges


FIXTURES = Path(__file__).parent / "fixtures"


class ViewerModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.source = self.root / "state"
        target = self.source / "sessions" / "2026" / "08" / "31"
        target.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "normal_completed.jsonl", target / "normal.jsonl")
        shutil.copyfile(FIXTURES / "subagent.jsonl", target / "subagent.jsonl")
        JournalEngine(self.repo, self.source, home=Path("/home/tester")).sync(
            timezone_name="Europe/Zurich"
        )
        self.catalog = JournalCatalog(self.repo)
        self.catalog.refresh()
        self.model = SessionBrowserModel(self.catalog)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_filter_options_and_counts_are_deterministic(self) -> None:
        self.assertEqual(self.model.projects, tuple(sorted(self.model.projects, key=str.casefold)))
        self.assertEqual(self.model.dates, tuple(sorted(self.model.dates, reverse=True)))
        self.assertEqual(self.model.counts.visible, 2)
        self.assertEqual(self.model.counts.total, 2)
        self.assertGreaterEqual(self.model.counts.projects, 1)

    def test_project_branch_date_and_status_filters_compose(self) -> None:
        session = self.model.sessions[0]
        self.model.set_filter("project", session.project)
        self.model.set_filter("date_from", session.local_date)
        self.model.set_filter("date_to", session.local_date)
        if session.branch:
            self.model.set_filter("branch", session.branch)
        self.model.set_filter("status", session.status)
        self.assertTrue(self.model.sessions)
        self.assertTrue(all(item.project == session.project for item in self.model.sessions))
        self.model.set_filter("project", ALL)
        self.assertIsNone(self.model.filters.project)

    def test_selection_moves_safely_when_filter_hides_it(self) -> None:
        chosen = self.model.select_first()
        self.assertIsNotNone(chosen)
        visible = self.model.set_filter("project", "project-that-does-not-exist")
        self.assertEqual(visible, ())
        self.assertIsNone(self.model.selected)
        with self.assertRaisesRegex(ValueError, "not visible"):
            self.model.select(chosen.session_id)

    def test_row_labels_and_warning_badges_use_safe_metadata(self) -> None:
        session = self.model.sessions[0]
        self.assertIn(session.local_date, display_start(session))
        self.assertIn(session.status, session_badges(session))
        subagent = next(item for item in self.model.sessions if item.source_kind == "subagent")
        self.assertIn("sub-agent", session_badges(subagent))

    def test_unknown_filter_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown browser filter"):
            self.model.set_filter("raw_source", "private")

    def test_search_hits_filter_sessions_and_remember_entry(self) -> None:
        session = self.model.sessions[-1]
        hit = SearchHit(
            session.session_id,
            7,
            session.started_at_utc,
            "Safe matching context.",
            session.project,
            session.branch,
            ("test",),
            False,
        )
        visible = self.model.set_search_hits((hit,), active=True)
        self.assertEqual([item.session_id for item in visible], [session.session_id])
        self.assertEqual(self.model.matching_entry(session.session_id), 7)
        self.model.set_search_hits((), active=False)
        self.assertEqual(len(self.model.sessions), 2)


if __name__ == "__main__":
    unittest.main()

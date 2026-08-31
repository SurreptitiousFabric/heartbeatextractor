from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from codex_journal.engine import JournalEngine
from codex_journal.viewer_activity import build_activity_report, fill_daily_range
from codex_journal.viewer_catalog import CatalogDetail, CatalogEntry, CatalogSession, JournalCatalog


FIXTURES = Path(__file__).parent / "fixtures"


class ViewerActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.source = self.root / "state" / "sessions" / "2026" / "08" / "31"
        self.source.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "normal_completed.jsonl", self.source / "normal.jsonl")
        shutil.copyfile(FIXTURES / "active_append.jsonl", self.source / "active.jsonl")
        JournalEngine(self.repo, self.root / "state", home=Path("/home/tester")).sync(
            timezone_name="Europe/Zurich"
        )
        self.catalog = JournalCatalog(self.repo)
        self.catalog.refresh()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_daily_weekly_status_project_and_tag_counts_are_traceable(self) -> None:
        report = build_activity_report(self.catalog)
        self.assertTrue(report.days)
        self.assertTrue(report.weeks)
        self.assertTrue(report.projects)
        self.assertEqual(
            sum(bucket.entries for bucket in report.days),
            sum(session.entry_count for session in self.catalog.sessions),
        )
        refs = [ref for bucket in report.days for ref in bucket.entry_refs]
        self.assertEqual(len(refs), len(set(refs)))
        statuses = dict(report.days[0].statuses)
        self.assertIn("active", {status for day in report.days for status, _count in day.statuses})
        self.assertTrue(statuses)
        weekly_statuses = {status for week in report.weeks for status, _count in week.statuses}
        self.assertIn("active", weekly_statuses)
        self.assertIn("completed", weekly_statuses)

    def test_project_calendar_points_to_exact_sessions_and_dates(self) -> None:
        report = build_activity_report(self.catalog)
        for project in report.projects:
            for day in project.days:
                self.assertEqual(day.key, day.start_date)
                self.assertTrue(day.session_ids)
                self.assertTrue(all(self.catalog.get(session_id) for session_id in day.session_ids))

    def test_empty_periods_are_explicit(self) -> None:
        report = build_activity_report(self.catalog)
        filled = fill_daily_range(report, "2026-08-30", "2026-09-02")
        self.assertEqual(len(filled), 4)
        self.assertEqual([item.key for item in filled], ["2026-08-30", "2026-08-31", "2026-09-01", "2026-09-02"])
        self.assertTrue(any(not item.session_ids for item in filled))

    def test_dst_fallback_entries_use_generated_timezone_bucketing(self) -> None:
        session = CatalogSession(
            "dst-session",
            None,
            "completed",
            "2026-10-25T00:00:00Z",
            "2026-10-25T02:00:00Z",
            "Europe/Zurich",
            "~/src/example",
            "Example/project",
            "main",
            "cli",
            "f" * 64,
            2,
            0,
            0,
            Path("journal/2026/10/25/example.md"),
            Path("journal/2026/10/25/example.provenance.json"),
        )
        entries = (
            CatalogEntry(0, "02:30", "Before fallback.", 10, "2026-10-25T00:30:00Z", "a" * 64, False),
            CatalogEntry(1, "02:30", "After fallback.", 11, "2026-10-25T01:30:00Z", "b" * 64, False),
        )

        class FakeCatalog:
            sessions = (session,)

            def load_detail(self, _session_id: str, *, cache: bool = True) -> CatalogDetail:
                return CatalogDetail(session, entries, ())

        report = build_activity_report(FakeCatalog())
        self.assertEqual(len(report.days), 1)
        self.assertEqual(report.days[0].key, "2026-10-25")
        self.assertEqual(report.days[0].entries, 2)
        self.assertEqual(report.days[0].entry_refs, (("dst-session", 0), ("dst-session", 1)))

    def test_report_does_not_fill_catalog_detail_cache(self) -> None:
        self.catalog._details.clear()
        build_activity_report(self.catalog)
        self.assertEqual(self.catalog._details, {})


if __name__ == "__main__":
    unittest.main()

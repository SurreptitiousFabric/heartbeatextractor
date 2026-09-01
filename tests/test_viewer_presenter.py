from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from codex_journal.viewer_catalog import CatalogDetail, CatalogEntry, CatalogSession
from codex_journal.viewer_presenter import (
    concise_session_summary,
    present_entry,
    present_timeline,
    safe_inline_markup,
)
from codex_journal.viewer_tags import classify_entry


def session(**changes: object) -> CatalogSession:
    base = CatalogSession(
        session_id="11111111-1111-4111-8111-111111111111",
        parent_session_id=None,
        status="completed",
        started_at_utc="2026-10-25T00:55:00Z",
        ended_at_utc="2026-10-25T01:10:00Z",
        rendered_timezone="Europe/Zurich",
        working_directory="~/src/example",
        repository="Example/project",
        branch="main",
        source_kind="cli",
        source_fingerprint="f" * 64,
        entry_count=2,
        redaction_count=0,
        extraction_error_count=0,
        journal_path=Path("journal/2026/10/25/example.md"),
        provenance_path=Path("journal/2026/10/25/example.provenance.json"),
    )
    return replace(base, **changes)


def entry(index: int, timestamp: str, text: str, *, redacted: bool = False) -> CatalogEntry:
    return CatalogEntry(index, "02:00", text, index + 10, timestamp, str(index) * 64, redacted)


class ViewerPresenterTests(unittest.TestCase):
    def test_deterministic_tags_preserve_multiple_meanings(self) -> None:
        tags = classify_entry("Security test failed; corrected blocker and pushed commit.")
        self.assertEqual(
            tags,
            ("failure", "test", "security", "blocker", "correction", "commit"),
        )

    def test_issue_and_stop_tags_do_not_rewrite_safe_identifiers(self) -> None:
        text = "Stopped before pushing commit abc123 for #35; work remains uncommitted."
        self.assertIn("issue/PR", classify_entry(text))
        self.assertIn("stop", classify_entry(text))
        self.assertIn("#35", text)
        self.assertIn("abc123", text)

    def test_filename_and_bare_commit_hash_are_visually_tagged(self) -> None:
        tags = classify_entry("Updated src/viewer.py at deadbeef without opening a link.")
        self.assertIn("filename", tags)
        self.assertIn("commit", tags)

    def test_dst_fallback_keeps_repeated_minutes_as_distinct_entries(self) -> None:
        first = entry(0, "2026-10-25T00:00:00Z", "First repeated minute.")
        second = entry(1, "2026-10-25T01:00:00Z", "Second repeated minute.")
        shown = present_timeline(CatalogDetail(session(), (first, second), ()))
        self.assertEqual([item.display_time for item in shown], ["02:00", "02:00"])
        self.assertEqual([item.entry.text for item in shown], [first.text, second.text])
        self.assertEqual(len(shown), 2)

    def test_date_groups_come_from_exact_event_timestamp(self) -> None:
        shown = present_entry(entry(0, "2026-08-31T22:30:00Z", "Late update."), session())
        self.assertEqual(shown.local_date, "2026-09-01")
        self.assertEqual(shown.display_time, "00:30")

    def test_redaction_is_an_indicator_without_original_text(self) -> None:
        safe = entry(0, "2026-08-31T13:00:00Z", "Token [REDACTED].", redacted=True)
        shown = present_entry(safe, session())
        self.assertEqual(shown.indicators, ("redacted",))
        self.assertEqual(shown.entry.text, "Token [REDACTED].")

    def test_invalid_timestamp_is_not_inferred(self) -> None:
        shown = present_entry(entry(0, "not-a-time", "Untimed display."), session())
        self.assertEqual(shown.local_date, "Unknown date")
        self.assertEqual(shown.display_time, "02:00")

    def test_session_summary_is_whitespace_normalized_and_bounded(self) -> None:
        summary = concise_session_summary("  Reviewing\n#35   hostile profile contract.  ")
        self.assertEqual(summary, "Reviewing #35 hostile profile contract.")
        bounded = concise_session_summary("word " * 100, limit=30)
        self.assertLessEqual(len(bounded), 30)
        self.assertTrue(bounded.endswith("…"))

    def test_inline_code_markup_escapes_content_before_styling(self) -> None:
        markup = safe_inline_markup("Opened `<unsafe>&` then `src/viewer.py`.")
        self.assertIn("&lt;unsafe&gt;&amp;", markup)
        self.assertIn('font_family="monospace"', markup)
        self.assertNotIn("`", markup)
        self.assertEqual(safe_inline_markup("Unbalanced `value"), "Unbalanced `value")


if __name__ == "__main__":
    unittest.main()

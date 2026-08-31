from __future__ import annotations

import unittest
from pathlib import Path

from codex_journal.viewer_catalog import CatalogDetail, CatalogEntry, CatalogSession
from codex_journal.viewer_compare import compare_details, filter_timeline


def session(session_id: str, entries: int, **changes: object) -> CatalogSession:
    values = dict(
        session_id=session_id,
        parent_session_id=None,
        status="completed",
        started_at_utc="2026-08-31T10:00:00Z",
        ended_at_utc="2026-08-31T10:01:30Z",
        rendered_timezone="Europe/Zurich",
        working_directory="~/src/example",
        repository="Example/project",
        branch="main",
        source_kind="cli",
        source_fingerprint="f" * 64,
        entry_count=entries,
        redaction_count=0,
        extraction_error_count=0,
        journal_path=Path("journal/example.md"),
        provenance_path=Path("journal/example.provenance.json"),
    )
    values.update(changes)
    return CatalogSession(**values)


def entry(index: int, text: str) -> CatalogEntry:
    return CatalogEntry(index, f"12:{index:02d}", text, index, "2026-08-31T10:00:00Z", "a" * 64, False)


def detail(session_id: str, texts: tuple[str, ...], **changes: object) -> CatalogDetail:
    entries = tuple(entry(index, text) for index, text in enumerate(texts))
    return CatalogDetail(session(session_id, len(entries), **changes), entries, ())


class ViewerComparisonTests(unittest.TestCase):
    def test_exact_text_matching_never_pairs_contradictory_replacements(self) -> None:
        left = detail("left", ("Reviewing security.", "Tests failed."))
        right = detail("right", ("Reviewing security.", "Tests passed."))
        report = compare_details(left, right)
        self.assertEqual([row.kind for row in report.timeline], ["unchanged", "left only", "right only"])
        self.assertIsNone(report.timeline[1].right)
        self.assertIsNone(report.timeline[2].left)

    def test_duplicate_exact_entries_remain_in_source_order(self) -> None:
        left = detail("left", ("Same.", "Same.", "Left only."))
        right = detail("right", ("Same.", "Same.", "Right only."))
        report = compare_details(left, right)
        self.assertEqual([row.kind for row in report.timeline[:2]], ["unchanged", "unchanged"])
        self.assertEqual(report.timeline[0].left.index, 0)
        self.assertEqual(report.timeline[1].left.index, 1)

    def test_metadata_duration_counts_tags_errors_and_redactions_are_explicit(self) -> None:
        left = detail("left", ("Security test failed.",), extraction_error_count=2)
        right = detail("right", (), ended_at_utc=None, redaction_count=3)
        report = compare_details(left, right)
        values = {row.label: (row.left, row.right) for row in report.metadata}
        self.assertEqual(values["Duration"], ("00:01:30", "Not completed"))
        self.assertEqual(values["Extraction errors"], ("2", "0"))
        self.assertEqual(values["Redactions"], ("0", "3"))
        self.assertIn("security", report.tags)

    def test_required_tag_filter_is_deterministic(self) -> None:
        report = compare_details(
            detail("left", ("Security finding.", "Tests passed.")),
            detail("right", ("Blocker found.",)),
        )
        filtered = filter_timeline(report.timeline, "security")
        self.assertEqual(len(filtered), 1)
        self.assertIn("security", filtered[0].tags)

    def test_no_entry_and_very_different_sizes_are_supported(self) -> None:
        report = compare_details(detail("empty", ()), detail("many", tuple(f"Entry {i}." for i in range(200))))
        self.assertEqual(len(report.timeline), 200)
        self.assertTrue(all(row.kind == "right only" for row in report.timeline))


if __name__ == "__main__":
    unittest.main()

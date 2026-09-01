from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_journal.artifacts import decode_index, decode_journal, decode_provenance


def journal_body(*metadata_lines: str) -> str:
    required = [
        'session_id: "session-1"',
        "parent_session_id: null",
        'status: "completed"',
        'started_at_utc: "2026-08-31T10:00:00Z"',
        "ended_at_utc: null",
        'rendered_timezone: "Europe/Zurich"',
        "working_directory: null",
        "repository: null",
        "branch: null",
        'source_kind: "cli"',
        f'source_fingerprint: "{"f" * 64}"',
        "timeline_entries: 1",
        "redactions: 0",
        "extraction_errors: 0",
        'generated_by: "codex-journal"',
        "format_version: 1",
    ]
    return "\n".join(["---", *metadata_lines, *required, "---", "", "## Timeline", "", "12:00  Safe entry.", ""])


class GeneratedArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_journal_decoder_returns_versioned_metadata_and_multiple_findings(self) -> None:
        path = self.root / "journal.md"
        path.write_text(journal_body("malformed line", "also malformed"), encoding="utf-8")
        decoded = decode_journal(path)
        self.assertEqual(len(decoded.findings), 2)
        self.assertTrue(all(item.code == "malformed_metadata" for item in decoded.findings))
        assert decoded.metadata is not None
        self.assertEqual(decoded.metadata.format_version, 1)
        self.assertEqual(decoded.metadata.session_id, "session-1")
        self.assertEqual(decoded.timeline[0].text, "Safe entry.")

    def test_journal_metadata_line_provenance_and_index_limits_fail_closed(self) -> None:
        journal = self.root / "journal.md"
        journal.write_text(journal_body(), encoding="utf-8")
        self.assertEqual(decode_journal(journal, max_journal_bytes=1).findings[0].code, "oversized")
        line_limited = decode_journal(journal, max_metadata_line_bytes=8)
        self.assertTrue(any(item.code == "oversized_metadata_line" for item in line_limited.findings))

        provenance = self.root / "journal.provenance.json"
        provenance.write_text(json.dumps({"format_version": 1}), encoding="utf-8")
        self.assertEqual(
            decode_provenance(provenance, max_provenance_bytes=1).findings[0].code,
            "oversized",
        )
        index = self.root / "INDEX.md"
        index.write_text("[journal](journal.md)\n", encoding="utf-8")
        self.assertEqual(decode_index(index, max_index_bytes=1).findings[0].code, "oversized")


if __name__ == "__main__":
    unittest.main()

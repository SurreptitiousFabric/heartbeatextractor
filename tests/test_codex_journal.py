from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codex_journal.artifacts import decode_journal
from codex_journal.compact import compact_candidates
from codex_journal.engine import JournalEngine
from codex_journal.model import Candidate, ExtractionOutcome
from codex_journal.parser import duplicate_session_ids, extract_session
from codex_journal.redact import redact_text, shorten_home
from codex_journal.render import atomic_write
from codex_journal.state import read_all_readonly
from codex_journal.viewer import ViewerUnavailable, load_gtk


FIXTURES = Path(__file__).parent / "fixtures"
TIMELINE = re.compile(r"^\d{2}:\d{2}  (.*)$")


def json_line(value: dict[str, object]) -> str:
    return json.dumps(value, separators=(",", ":")) + "\n"


def session_meta(session_id: str, timestamp: str, cwd: str = "/home/tester/src/project") -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "session_id": session_id,
            "timestamp": timestamp,
            "cwd": cwd,
            "source": "cli",
        },
    }


def lifecycle(kind: str, timestamp: str) -> dict[str, object]:
    return {"timestamp": timestamp, "type": "event_msg", "payload": {"type": kind}}


def commentary(text: str, timestamp: str) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": text}],
        },
    }


class JournalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "journal-repo"
        self.codex = self.base / "codex-state"
        self.sessions = self.codex / "sessions" / "2026" / "08" / "31"
        self.sessions.mkdir(parents=True)
        (self.repo / "journal").mkdir(parents=True)
        (self.repo / "projects").mkdir(parents=True)
        (self.repo / "INDEX.md").write_text("# Codex session journals\n", encoding="utf-8")
        self.engine = JournalEngine(self.repo, self.codex, home=Path("/home/tester"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def fixture(self, name: str, destination_name: str | None = None) -> Path:
        destination = self.sessions / (destination_name or name)
        shutil.copyfile(FIXTURES / name, destination)
        return destination

    def source(self, name: str, records: list[dict[str, object]], extra: bytes = b"") -> Path:
        path = self.sessions / name
        with path.open("wb") as output:
            for record in records:
                output.write(json_line(record).encode("utf-8"))
            output.write(extra)
        return path

    def journal_for(self, session_id: str) -> Path:
        for path in (self.repo / "journal").rglob("*.md"):
            metadata = decode_journal(path).metadata
            if metadata is not None and metadata.session_id == session_id:
                return path
        self.fail(f"journal not found for {session_id}")

    def timeline(self, path: Path) -> list[str]:
        return [match.group(1) for line in path.read_text(encoding="utf-8").splitlines() if (match := TIMELINE.match(line))]

    def generated_snapshot(self) -> dict[str, bytes]:
        paths = [self.repo / "INDEX.md"]
        paths.extend((self.repo / "journal").rglob("*"))
        paths.extend((self.repo / "projects").rglob("*"))
        return {
            path.relative_to(self.repo).as_posix(): path.read_bytes()
            for path in paths
            if path.is_file()
        }


class ExtractionTests(JournalTestCase):
    def test_01_normal_completed_session(self) -> None:
        self.fixture("normal_completed.jsonl")
        result = self.engine.sync(timezone_name="Europe/Zurich")
        self.assertEqual(result.processed, 1)
        journal = self.journal_for("11111111-1111-4111-8111-111111111111")
        decoded = decode_journal(journal)
        self.assertFalse(decoded.findings)
        assert decoded.metadata is not None
        self.assertEqual(decoded.metadata.status, "completed")
        self.assertEqual(decoded.metadata.ended_at_utc, "2026-08-31T13:15:01Z")
        self.assertEqual(self.timeline(journal), ["Reviewing #35 hostile profile contract.", "Found two review blockers."])
        self.assertFalse(self.engine.verify().errors)

    def test_02_active_append_only_session(self) -> None:
        source = self.fixture("active_append.jsonl")
        first = self.engine.sync(timezone_name="Europe/Zurich")
        self.assertEqual(first.rebuilt, 1)
        journal = self.journal_for("22222222-2222-4222-8222-222222222222")
        self.assertEqual(decode_journal(journal).metadata.status, "active")
        with source.open("a", encoding="utf-8") as output:
            output.write(json_line(commentary("Completed append-only validation.", "2026-08-31T10:01:00Z")))
            output.write(json_line(lifecycle("task_complete", "2026-08-31T10:01:01Z")))
        second = self.engine.sync(timezone_name="Europe/Zurich")
        self.assertEqual(second.appended, 1)
        self.assertIn("Completed append-only validation.", self.timeline(journal))
        self.assertEqual(decode_journal(journal).metadata.status, "completed")

    def test_02b_extraction_outcome_is_structured_for_each_mode(self) -> None:
        source_path = self.fixture("active_append.jsonl")
        source = self.engine.discover()[0][0]
        rebuilt = extract_session(source, None, home=Path("/home/tester"))
        self.assertEqual(rebuilt.mode.value, "rebuild")
        unchanged = extract_session(source, rebuilt.cache, home=Path("/home/tester"))
        self.assertEqual(unchanged.mode.value, "unchanged")
        with source_path.open("a", encoding="utf-8") as output:
            output.write(json_line(commentary("Validated the appended suffix.", "2026-08-31T10:01:00Z")))
        appended = extract_session(source, unchanged.cache, home=Path("/home/tester"))
        self.assertEqual(appended.mode.value, "append")

    def test_02c_engine_rejects_an_impossible_extraction_mode(self) -> None:
        self.fixture("active_append.jsonl")
        source = self.engine.discover()[0][0]
        valid = extract_session(source, None, home=Path("/home/tester"))
        impossible = ExtractionOutcome(valid.cache, mock.sentinel.impossible_mode)
        with mock.patch("codex_journal.engine.extract_session", return_value=impossible):
            with self.assertRaises(AssertionError):
                self.engine.sync(timezone_name="Europe/Zurich")
        self.assertEqual(read_all_readonly(self.repo / "state" / "journal.sqlite3"), [])

    def test_03_subagent_activity_and_parent_links(self) -> None:
        self.fixture("normal_completed.jsonl")
        self.fixture("subagent.jsonl")
        self.engine.sync(timezone_name="Europe/Zurich")
        child = self.journal_for("33333333-3333-4333-8333-333333333333")
        parent = self.journal_for("11111111-1111-4111-8111-111111111111")
        metadata = decode_journal(child).metadata
        assert metadata is not None
        self.assertEqual(metadata.parent_session_id, "11111111-1111-4111-8111-111111111111")
        self.assertIn("Parent:", child.read_text(encoding="utf-8"))
        self.assertIn("Child:", parent.read_text(encoding="utf-8"))
        self.assertNotIn("PRIVATE INTER-AGENT", child.read_text(encoding="utf-8"))

    def test_04_repeated_heartbeat_compaction(self) -> None:
        candidates = [
            Candidate(1, "2026-08-31T13:12:00Z", "Reviewing #35 hostile profile contract.", "a" * 64),
            Candidate(2, "2026-08-31T13:12:10Z", "Still reviewing #35 hostile profile contract.", "b" * 64),
            Candidate(3, "2026-08-31T13:12:20Z", "Still reviewing #35 hostile profile contract.", "c" * 64),
            Candidate(4, "2026-08-31T13:14:00Z", "Found two review blockers.", "d" * 64),
        ]
        self.assertEqual(
            [entry.text for entry in compact_candidates(candidates)],
            ["Reviewing #35 hostile profile contract.", "Found two review blockers."],
        )
        contracted = Candidate(5, "2026-08-31T13:14:10Z", "I've found another blocker.", "e" * 64)
        self.assertEqual(compact_candidates([contracted])[0].text, "Found another blocker.")

    def test_05_blocker_retained_between_routine_progress(self) -> None:
        candidates = [
            Candidate(1, "2026-08-31T13:00:00Z", "Checking package binding.", "a" * 64),
            Candidate(2, "2026-08-31T13:00:10Z", "Found a blocker in package binding.", "b" * 64),
            Candidate(3, "2026-08-31T13:00:20Z", "Still checking package binding.", "c" * 64),
        ]
        texts = [entry.text for entry in compact_candidates(candidates)]
        self.assertIn("Found a blocker in package binding.", texts)

    def test_06_failed_test_correction_and_pass_retained(self) -> None:
        candidates = [
            Candidate(1, "2026-08-31T13:00:00Z", "Package-binding test failed because the fixture was unreadable.", "a" * 64),
            Candidate(2, "2026-08-31T13:01:00Z", "Corrected the fixture-permission defect.", "b" * 64),
            Candidate(3, "2026-08-31T13:02:00Z", "Three focused offline contracts passed.", "c" * 64),
        ]
        self.assertEqual(len(compact_candidates(candidates)), 3)

    def test_07_unknown_event_types_fail_closed(self) -> None:
        sid = "77777777-7777-4777-8777-777777777777"
        records = [
            session_meta(sid, "2026-08-31T08:00:00Z"),
            lifecycle("task_started", "2026-08-31T08:00:01Z"),
            {"timestamp": "2026-08-31T08:00:02Z", "type": "mystery", "payload": {"text": "UNKNOWN OUTER SECRET"}},
            {"timestamp": "2026-08-31T08:00:03Z", "type": "response_item", "payload": {"type": "mystery", "text": "UNKNOWN PAYLOAD SECRET"}},
            commentary("Known visible status.", "2026-08-31T08:00:04Z"),
            lifecycle("task_complete", "2026-08-31T08:00:05Z"),
        ]
        self.source("unknown.jsonl", records)
        self.engine.sync(timezone_name="Europe/Zurich")
        body = self.journal_for(sid).read_text(encoding="utf-8")
        self.assertIn("Known visible status.", body)
        self.assertNotIn("UNKNOWN", body)

    def test_08_hidden_reasoning_not_extracted(self) -> None:
        self.fixture("normal_completed.jsonl")
        self.engine.sync(timezone_name="Europe/Zurich")
        body = self.journal_for("11111111-1111-4111-8111-111111111111").read_text(encoding="utf-8")
        self.assertNotIn("HIDDEN REASONING", body)

    def test_09_prompts_tool_output_and_final_not_extracted(self) -> None:
        self.fixture("normal_completed.jsonl")
        self.engine.sync(timezone_name="Europe/Zurich")
        body = self.journal_for("11111111-1111-4111-8111-111111111111").read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE USER PROMPT", body)
        self.assertNotIn("RAW TOOL OUTPUT", body)
        self.assertNotIn("COMPLETE FINAL RESPONSE", body)


class TimeAndRobustnessTests(JournalTestCase):
    def test_10_europe_zurich_conversion(self) -> None:
        self.fixture("normal_completed.jsonl")
        self.engine.sync(timezone_name="Europe/Zurich")
        lines = self.journal_for("11111111-1111-4111-8111-111111111111").read_text(encoding="utf-8")
        self.assertIn("15:12  Reviewing #35", lines)

    def test_11_daylight_saving_fallback(self) -> None:
        sid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        records = [
            session_meta(sid, "2026-10-25T00:00:00Z"),
            lifecycle("task_started", "2026-10-25T00:00:01Z"),
            commentary("Validated before the fallback.", "2026-10-25T00:30:00Z"),
            commentary("Validated after the fallback.", "2026-10-25T01:30:00Z"),
            lifecycle("task_complete", "2026-10-25T01:31:00Z"),
        ]
        october = self.codex / "sessions" / "2026" / "10" / "25"
        october.mkdir(parents=True)
        path = october / "dst.jsonl"
        path.write_text("".join(json_line(record) for record in records), encoding="utf-8")
        self.engine.sync(timezone_name="Europe/Zurich")
        timeline = [line for line in self.journal_for(sid).read_text(encoding="utf-8").splitlines() if TIMELINE.match(line)]
        self.assertEqual([line[:5] for line in timeline], ["02:30", "02:30"])

    def test_12_malformed_and_oversized_records_are_recorded(self) -> None:
        sid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        records = [session_meta(sid, "2026-08-31T08:00:00Z"), lifecycle("task_started", "2026-08-31T08:00:01Z")]
        extra = b"x" * 700 + b"\n{bad json}\n" + json_line(commentary("Visible after errors.", "2026-08-31T08:01:00Z")).encode() + json_line(lifecycle("task_complete", "2026-08-31T08:01:01Z")).encode()
        self.source("damaged.jsonl", records, extra)
        engine = JournalEngine(self.repo, self.codex, home=Path("/home/tester"), max_record_bytes=512)
        result = engine.sync(timezone_name="Europe/Zurich")
        self.assertEqual(result.sessions_with_errors, 1)
        provenance = json.loads(self.journal_for(sid).with_suffix(".provenance.json").read_text(encoding="utf-8"))
        codes = {error["code"] for error in provenance["extraction_errors"]}
        self.assertEqual(codes, {"oversized_record", "malformed_json"})
        self.assertIn("Visible after errors.", self.timeline(self.journal_for(sid)))

    def test_13_source_truncation_or_replacement_rebuilds(self) -> None:
        sid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        initial = [session_meta(sid, "2026-08-31T08:00:00Z"), lifecycle("task_started", "2026-08-31T08:00:01Z")]
        initial.extend(commentary(f"Routine progress {number}.", f"2026-08-31T08:00:{10 + number:02d}Z") for number in range(6))
        source = self.source("replacement.jsonl", initial)
        self.engine.sync(timezone_name="Europe/Zurich")
        replacement = [
            session_meta(sid, "2026-08-31T08:00:00Z"),
            lifecycle("task_started", "2026-08-31T08:00:01Z"),
            commentary("Replacement source validated.", "2026-08-31T08:02:00Z"),
            lifecycle("turn_aborted", "2026-08-31T08:02:01Z"),
        ]
        source.write_text("".join(json_line(record) for record in replacement), encoding="utf-8")
        result = self.engine.sync(timezone_name="Europe/Zurich")
        self.assertEqual(result.rebuilt, 1)
        body = self.journal_for(sid).read_text(encoding="utf-8")
        self.assertIn("Replacement source validated.", body)
        self.assertNotIn("Routine progress", body)

    def test_14_idempotent_second_sync_is_byte_identical(self) -> None:
        self.fixture("normal_completed.jsonl")
        self.engine.sync(timezone_name="Europe/Zurich")
        before = self.generated_snapshot()
        result = self.engine.sync(timezone_name="Europe/Zurich")
        after = self.generated_snapshot()
        self.assertEqual(result.unchanged, 1)
        self.assertEqual(before, after)

    def test_15_atomic_output_replacement(self) -> None:
        target = self.repo / "atomic.md"
        real_replace = os.replace
        with mock.patch("codex_journal.atomic.os.replace", wraps=real_replace) as replacement:
            changed = atomic_write(target, b"new content\n")
        self.assertTrue(changed)
        replacement.assert_called_once()
        self.assertEqual(target.read_bytes(), b"new content\n")
        leftovers = list(target.parent.glob(f".{target.name}.*.tmp"))
        self.assertFalse(leftovers)

    def test_15b_verify_accepts_a_valid_synced_prefix_after_live_append(self) -> None:
        source = self.fixture("active_append.jsonl")
        self.engine.sync(timezone_name="Europe/Zurich")
        with source.open("a", encoding="utf-8") as output:
            output.write(
                json_line(
                    commentary(
                        "This append happened after the fixed sync snapshot.",
                        "2026-08-31T10:01:00Z",
                    )
                )
            )
        verification = self.engine.verify()
        self.assertFalse(verification.errors)
        self.assertTrue(
            any("source appended since last sync" in warning for warning in verification.warnings)
        )

    def test_15c_verify_rejects_changed_or_truncated_synced_prefix(self) -> None:
        source = self.fixture("active_append.jsonl")
        self.engine.sync(timezone_name="Europe/Zurich")
        original = source.read_text(encoding="utf-8")
        source.write_text(original.replace("Checking", "Tracking"), encoding="utf-8")
        self.assertTrue(
            any("source fingerprint mismatch" in error for error in self.engine.verify().errors)
        )

        source.write_text(original[:-1], encoding="utf-8")
        self.assertTrue(
            any("source snapshot unavailable" in error for error in self.engine.verify().errors)
        )


class PrivacyAndIndexTests(JournalTestCase):
    def test_16_secret_redaction_and_environment_dump_omission(self) -> None:
        sid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        records = [
            session_meta(sid, "2026-08-31T08:00:00Z"),
            lifecycle("task_started", "2026-08-31T08:00:01Z"),
            commentary(f"Checking /home/tester/work with Bearer abcdefghijklmnop and API_TOKEN={secret}.", "2026-08-31T08:00:10Z"),
            commentary("FIRST=one\nSECOND=two\nTHIRD=three", "2026-08-31T08:00:20Z"),
            lifecycle("task_complete", "2026-08-31T08:00:30Z"),
        ]
        self.source("secret.jsonl", records)
        self.engine.sync(timezone_name="Europe/Zurich")
        journal = self.journal_for(sid)
        combined = journal.read_text(encoding="utf-8") + journal.with_suffix(".provenance.json").read_text(encoding="utf-8")
        self.assertNotIn(secret, combined)
        self.assertNotIn("abcdefghijklmnop", combined)
        self.assertNotIn("FIRST=one", combined)
        self.assertIn("[REDACTED]", combined)
        self.assertIn("~/work", combined)
        private, count = redact_text("-----BEGIN PRIVATE KEY-----\nvalue\n-----END PRIVATE KEY-----")
        self.assertEqual(private, "[REDACTED PRIVATE KEY]")
        self.assertEqual(count, 1)

    def test_17_home_directory_shortening(self) -> None:
        self.assertEqual(shorten_home("/home/tester", Path("/home/tester")), "~")
        self.assertEqual(shorten_home("/home/tester/src/project", Path("/home/tester")), "~/src/project")
        self.fixture("active_append.jsonl")
        self.engine.sync(timezone_name="Europe/Zurich")
        all_output = "".join(value.decode("utf-8") for value in self.generated_snapshot().values())
        self.assertNotIn("/home/tester", all_output)

    def test_18_duplicate_session_id_detection(self) -> None:
        sid = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        records = [session_meta(sid, "2026-08-31T08:00:00Z"), lifecycle("task_complete", "2026-08-31T08:00:01Z")]
        self.source("duplicate-a.jsonl", records)
        self.source("duplicate-b.jsonl", records)
        sessions, _ = self.engine.discover()
        self.assertIn(sid, duplicate_session_ids(sessions))
        self.assertTrue(any("duplicate source session ID" in error for error in self.engine.verify().errors))

    def test_19_stable_filenames_and_indexes(self) -> None:
        self.fixture("normal_completed.jsonl")
        self.fixture("acceptance.jsonl")
        self.engine.sync(timezone_name="Europe/Zurich")
        paths_before = sorted(path.relative_to(self.repo).as_posix() for path in (self.repo / "journal").rglob("*.md"))
        index_before = (self.repo / "INDEX.md").read_bytes()
        self.engine.sync(timezone_name="Europe/Zurich")
        paths_after = sorted(path.relative_to(self.repo).as_posix() for path in (self.repo / "journal").rglob("*.md"))
        self.assertEqual(paths_before, paths_after)
        self.assertEqual(index_before, (self.repo / "INDEX.md").read_bytes())
        self.assertTrue((self.repo / "projects" / "a-quo.md").is_file())
        self.assertFalse(self.engine.verify().errors)

    def test_19b_same_uuid_prefix_gets_distinct_stable_paths(self) -> None:
        first = "01a048d8-e03f-76f1-b73f-e1f540562b8a"
        second = "01a048d8-df74-7470-955d-ef6dcfa9989c"
        for number, session_id in enumerate((first, second)):
            records = [
                session_meta(session_id, "2026-08-31T08:00:00Z", "/home/tester/src/shared-project"),
                lifecycle("task_started", "2026-08-31T08:00:01Z"),
                commentary(f"Validated shared-prefix session {number}.", "2026-08-31T08:00:10Z"),
                lifecycle("task_complete", "2026-08-31T08:00:11Z"),
            ]
            self.source(f"shared-{number}.jsonl", records)
        self.engine.sync(timezone_name="Europe/Zurich")
        paths = sorted((self.repo / "journal").rglob("*.md"))
        self.assertEqual(len(paths), 2)
        self.assertNotEqual(paths[0].name, paths[1].name)
        self.assertTrue(all("01a048d8-" in path.name for path in paths))
        first_names = [path.name for path in paths]
        self.engine.sync(timezone_name="Europe/Zurich")
        self.assertEqual(first_names, [path.name for path in sorted((self.repo / "journal").rglob("*.md"))])
        self.assertFalse(self.engine.verify().errors)

    def test_19c_verify_detects_missing_cached_session_journal(self) -> None:
        self.fixture("normal_completed.jsonl")
        self.fixture("acceptance.jsonl")
        self.engine.sync(timezone_name="Europe/Zurich")
        missing = self.journal_for("44444444-4444-4444-8444-444444444444")
        missing.unlink()
        missing.with_suffix(".provenance.json").unlink()
        errors = self.engine.verify().errors
        self.assertTrue(any("processing state sessions missing generated journals" in error for error in errors))

    def test_20_acceptance_timeline(self) -> None:
        self.fixture("acceptance.jsonl")
        self.engine.sync(timezone_name="Europe/Zurich")
        journal = self.journal_for("44444444-4444-4444-8444-444444444444")
        lines = [line for line in journal.read_text(encoding="utf-8").splitlines() if TIMELINE.match(line)]
        self.assertEqual(
            lines,
            [
                "15:12  Reviewing #35 hostile profile contract.",
                "15:14  Found two review blockers.",
                "15:17  Package-binding test exposed a fixture-permission defect.",
                "15:20  Found a fail-open path for unconfirmed NEEDED evidence.",
                "15:23  Sub-agent corrected the fail-open path.",
                "15:31  Three focused offline contracts passed.",
            ],
        )


class ViewerFoundationTests(unittest.TestCase):
    def test_view_command_is_registered(self) -> None:
        from codex_journal.cli import build_parser

        args = build_parser().parse_args(["view"])
        self.assertEqual(args.command, "view")

    def test_optional_gtk_import_fails_with_setup_guidance(self) -> None:
        def unavailable(_name: str) -> object:
            raise ModuleNotFoundError("optional dependency absent")

        with self.assertRaisesRegex(ViewerUnavailable, "mise run bootstrap"):
            load_gtk(unavailable)

    def test_optional_gtk_import_requests_supported_versions(self) -> None:
        requested: list[tuple[str, str]] = []
        gi = SimpleNamespace(require_version=lambda name, version: requested.append((name, version)))
        repository = SimpleNamespace(Adw=object(), Gio=object(), Gtk=object())

        def available(name: str) -> object:
            return gi if name == "gi" else repository

        modules = load_gtk(available)
        self.assertEqual(requested, [("Gtk", "4.0"), ("Adw", "1"), ("Pango", "1.0")])
        self.assertEqual(len(modules), 4)


if __name__ == "__main__":
    unittest.main()

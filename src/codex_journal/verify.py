from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .artifacts import DecodedJournal, JournalMetadata, decode_index, decode_journal, decode_provenance
from .model import SessionCache, SourceSession, VerifyResult
from .parser import (
    DEFAULT_MAX_RECORD_BYTES,
    discover_sessions,
    duplicate_session_ids,
    sha256_file,
)
from .state import read_all_readonly


class RepositoryVerifier:
    def __init__(self, repo_root: Path, state_root: Path, max_record_bytes: int) -> None:
        self.repo_root = repo_root
        self.state_root = state_root
        self.max_record_bytes = max_record_bytes
        self.sources: dict[str, SourceSession] = {}
        self.caches: dict[str, SessionCache] = {}
        self.journals: list[Path] = []
        self.seen: dict[str, Path] = {}

    def run(self) -> VerifyResult:
        result = VerifyResult()
        self._verify_source_and_state(result)
        self._verify_journals_and_provenance(result)
        self._verify_indexes(result)
        return result

    def _verify_source_and_state(self, result: VerifyResult) -> None:
        sessions, discovery_errors = discover_sessions(
            self.state_root, max_record_bytes=self.max_record_bytes
        )
        result.warnings.extend(discovery_errors)
        duplicates = duplicate_session_ids(sessions)
        for session_id in sorted(duplicates):
            result.errors.append(f"duplicate source session ID: {session_id}")
        self.sources = {
            session.session_id: session
            for session in sessions
            if session.session_id not in duplicates
        }
        try:
            cached_sessions = read_all_readonly(self.repo_root / "state" / "journal.sqlite3")
        except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
            result.errors.append(f"malformed processing state: {exc}")
            cached_sessions = []
        self.caches = {cache.session_id: cache for cache in cached_sessions}
        self._report_set_difference(
            result,
            self.sources.keys() - self.caches.keys(),
            "source sessions missing from processing state",
        )
        self._report_set_difference(
            result,
            self.caches.keys() - self.sources.keys(),
            "processing state has missing source sessions",
        )
        owners: dict[str, list[str]] = {}
        for cache in cached_sessions:
            if cache.journal_relpath:
                owners.setdefault(cache.journal_relpath, []).append(cache.session_id)
        for path, session_ids in sorted(owners.items()):
            if len(session_ids) > 1:
                result.errors.append(
                    f"processing state journal path collision: {path} "
                    f"sessions={','.join(sorted(session_ids))}"
                )

    def _verify_journals_and_provenance(self, result: VerifyResult) -> None:
        self.journals = sorted((self.repo_root / "journal").rglob("*.md"))
        for journal in self.journals:
            result.journals += 1
            self._verify_journal(journal, result)
        self._report_set_difference(
            result,
            self.caches.keys() - self.seen.keys(),
            "processing state sessions missing generated journals",
        )
        self._report_set_difference(
            result,
            self.seen.keys() - self.caches.keys(),
            "generated journals absent from processing state",
        )

    def _verify_journal(self, journal: Path, result: VerifyResult) -> None:
        relative = journal.relative_to(self.repo_root)
        decoded = decode_journal(journal)
        self._record_findings(relative, decoded, result)
        metadata = decoded.metadata
        if metadata is None:
            return
        self._record_session_owner(metadata.session_id, journal, result)
        cached = self.caches.get(metadata.session_id)
        if cached is None:
            result.errors.append(f"{relative}: session absent from processing state")
        elif cached.journal_relpath != relative.as_posix():
            result.errors.append(f"{relative}: path disagrees with processing state")
        source = self.sources.get(metadata.session_id)
        if source is None:
            result.errors.append(f"{relative}: source session not found")
        elif cached is not None:
            self._verify_source_snapshot(relative, metadata, cached, source, result)
        result.entries += len(decoded.timeline)
        if len(decoded.timeline) != metadata.timeline_entries:
            result.errors.append(f"{relative}: timeline entry count mismatch")
        self._verify_provenance(journal, metadata.session_id, len(decoded.timeline), result)

    def _record_findings(
        self, relative: Path, decoded: DecodedJournal, result: VerifyResult
    ) -> None:
        result.errors.extend(
            f"{relative}: {finding.message}" for finding in decoded.findings
        )

    def _record_session_owner(
        self, session_id: str, journal: Path, result: VerifyResult
    ) -> None:
        if session_id in self.seen:
            result.errors.append(
                f"duplicate generated session ID {session_id}: "
                f"{self.seen[session_id].relative_to(self.repo_root)} and "
                f"{journal.relative_to(self.repo_root)}"
            )
        self.seen[session_id] = journal

    def _verify_source_snapshot(
        self,
        relative: Path,
        metadata: JournalMetadata,
        cached: SessionCache,
        source: SourceSession,
        result: VerifyResult,
    ) -> None:
        fingerprint = metadata.source_fingerprint
        if len(fingerprint) != 64 or cached.source_fingerprint != fingerprint:
            result.errors.append(f"{relative}: source fingerprint disagrees with processing state")
            return
        if cached.source_key != source.source_key:
            result.errors.append(f"{relative}: source location disagrees with processing state")
            return
        if not isinstance(cached.source_size, int) or cached.source_size < 0:
            result.errors.append(f"{relative}: invalid source snapshot size in processing state")
            return
        try:
            current_size = source.path.stat().st_size
            current_fingerprint = sha256_file(source.path, cached.source_size)
        except (OSError, ValueError) as exc:
            result.errors.append(
                f"{relative}: source snapshot unavailable: {type(exc).__name__}"
            )
            return
        if current_fingerprint != fingerprint:
            result.errors.append(f"{relative}: source fingerprint mismatch")
        elif current_size > cached.source_size:
            result.warnings.append(f"{relative}: source appended since last sync")

    def _verify_provenance(
        self, journal: Path, session_id: str, entry_count: int, result: VerifyResult
    ) -> None:
        provenance_path = journal.with_suffix(".provenance.json")
        relative = provenance_path.relative_to(self.repo_root)
        if not provenance_path.is_file():
            result.errors.append(f"{journal.relative_to(self.repo_root)}: missing provenance")
            return
        decoded = decode_provenance(provenance_path)
        result.errors.extend(f"{relative}: {item.message}" for item in decoded.findings)
        provenance = decoded.artifact
        if provenance is None:
            return
        if len(provenance.entries) != entry_count:
            result.errors.append(f"{relative}: provenance entry count mismatch")
        if any(entry.source_session_id != session_id for entry in provenance.entries):
            result.errors.append(f"{relative}: provenance session mismatch")

    def _verify_indexes(self, result: VerifyResult) -> None:
        root_index = self.repo_root / "INDEX.md"
        indexes = [root_index, *sorted((self.repo_root / "projects").glob("*.md"))]
        root_targets: dict[Path, int] = {}
        for index in indexes:
            relative = index.relative_to(self.repo_root)
            if not index.is_file():
                result.errors.append(f"missing index: {relative}")
                continue
            decoded = decode_index(index)
            result.errors.extend(f"{relative}: {item.message}" for item in decoded.findings)
            for target in decoded.links:
                self._verify_index_link(index, target, root_index, root_targets, result)
        for journal in self.journals:
            count = root_targets.get(journal.resolve(), 0)
            if count == 0:
                result.errors.append(
                    f"INDEX.md: journal is not indexed: {journal.relative_to(self.repo_root)}"
                )
            elif count > 1:
                result.errors.append(
                    f"INDEX.md: journal is indexed {count} times: "
                    f"{journal.relative_to(self.repo_root)}"
                )

    def _verify_index_link(
        self,
        index: Path,
        target: str,
        root_index: Path,
        root_targets: dict[Path, int],
        result: VerifyResult,
    ) -> None:
        if "://" in target or target.startswith("#"):
            return
        relative = index.relative_to(self.repo_root)
        resolved = (index.parent / target).resolve()
        try:
            resolved.relative_to(self.repo_root)
        except ValueError:
            result.errors.append(f"{relative}: link escapes repository: {target}")
            return
        if not resolved.is_file():
            result.errors.append(f"{relative}: broken link: {target}")
        elif index == root_index:
            root_targets[resolved] = root_targets.get(resolved, 0) + 1

    @staticmethod
    def _report_set_difference(
        result: VerifyResult, session_ids: set[str], label: str
    ) -> None:
        values = sorted(session_ids)
        if values:
            result.errors.append(
                f"{label}: count={len(values)} ids={','.join(values[:8])}"
            )


def verify_repository(
    repo_root: Path,
    state_root: Path,
    *,
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
) -> VerifyResult:
    return RepositoryVerifier(repo_root, state_root, max_record_bytes).run()

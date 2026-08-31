from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_METADATA_BYTES = 64 * 1024
MAX_METADATA_LINE_BYTES = 16 * 1024
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_PROVENANCE_BYTES = 32 * 1024 * 1024


class CatalogError(RuntimeError):
    """A safe generated-artifact parsing or indexing failure."""


@dataclass(frozen=True)
class CatalogSession:
    session_id: str
    parent_session_id: str | None
    status: str
    started_at_utc: str
    ended_at_utc: str | None
    rendered_timezone: str
    working_directory: str | None
    repository: str | None
    branch: str | None
    source_kind: str
    source_fingerprint: str
    entry_count: int
    redaction_count: int
    extraction_error_count: int
    journal_path: Path
    provenance_path: Path

    @property
    def project(self) -> str:
        if self.repository:
            return self.repository
        if self.working_directory:
            return self.working_directory.rstrip("/").rsplit("/", 1)[-1] or self.working_directory
        return "Unknown project"

    @property
    def local_date(self) -> str:
        parts = self.journal_path.parts
        try:
            index = parts.index("journal")
            return "-".join(parts[index + 1 : index + 4])
        except (ValueError, IndexError):
            return self.started_at_utc[:10]


@dataclass(frozen=True)
class CatalogEntry:
    index: int
    display_time: str
    text: str
    source_event_sequence: int
    original_timestamp_utc: str
    original_text_sha256: str
    redacted: bool


@dataclass(frozen=True)
class CatalogExtractionError:
    sequence: int
    code: str
    detail: str


@dataclass(frozen=True)
class CatalogDetail:
    session: CatalogSession
    entries: tuple[CatalogEntry, ...]
    extraction_errors: tuple[CatalogExtractionError, ...]


@dataclass(frozen=True)
class SearchHit:
    session_id: str
    entry_index: int
    timestamp_utc: str
    text: str
    project: str
    branch: str | None


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _read_front_matter(path: Path) -> dict[str, object]:
    metadata: dict[str, object] = {}
    consumed = 0
    try:
        with path.open("rb") as handle:
            opening = handle.readline(MAX_METADATA_LINE_BYTES + 1)
            consumed += len(opening)
            if opening != b"---\n":
                raise CatalogError("missing opening metadata delimiter")
            while consumed <= MAX_METADATA_BYTES:
                raw = handle.readline(MAX_METADATA_LINE_BYTES + 1)
                consumed += len(raw)
                if not raw:
                    raise CatalogError("missing closing metadata delimiter")
                if len(raw) > MAX_METADATA_LINE_BYTES:
                    raise CatalogError("metadata line exceeds size limit")
                if raw in {b"---\n", b"---"}:
                    return metadata
                try:
                    line = raw.decode("utf-8").rstrip("\n")
                except UnicodeDecodeError as exc:
                    raise CatalogError("metadata is not valid UTF-8") from exc
                if ": " not in line:
                    raise CatalogError("malformed metadata line")
                key, encoded = line.split(": ", 1)
                try:
                    metadata[key] = json.loads(encoded)
                except json.JSONDecodeError as exc:
                    raise CatalogError(f"malformed metadata value: {key}") from exc
    except OSError as exc:
        raise CatalogError(f"cannot read generated journal: {exc.strerror or type(exc).__name__}") from exc
    raise CatalogError("metadata exceeds size limit")


def _required_string(metadata: dict[str, object], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value:
        raise CatalogError(f"missing or invalid metadata: {key}")
    return value


def _optional_string(metadata: dict[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CatalogError(f"invalid metadata: {key}")
    return value


def _nonnegative_int(metadata: dict[str, object], key: str) -> int:
    value = metadata.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CatalogError(f"missing or invalid metadata: {key}")
    return value


def _read_bounded_text(path: Path, limit: int, label: str) -> str:
    try:
        size = path.stat().st_size
        if size > limit:
            raise CatalogError(f"{label} exceeds size limit")
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CatalogError(f"{label} is not valid UTF-8") from exc
    except OSError as exc:
        raise CatalogError(f"cannot read {label}: {exc.strerror or type(exc).__name__}") from exc


class JournalCatalog:
    """Read-only catalog over generated artifacts; it has no rollout-log input."""

    def __init__(
        self,
        repo_root: Path,
        *,
        max_journal_bytes: int = MAX_JOURNAL_BYTES,
        max_provenance_bytes: int = MAX_PROVENANCE_BYTES,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.journal_root = (self.repo_root / "journal").resolve()
        self.max_journal_bytes = max_journal_bytes
        self.max_provenance_bytes = max_provenance_bytes
        self._sessions: dict[str, CatalogSession] = {}
        self._children: dict[str, tuple[str, ...]] = {}
        self._details: dict[str, tuple[str, CatalogDetail]] = {}
        self.diagnostics: tuple[str, ...] = ()

    @property
    def sessions(self) -> tuple[CatalogSession, ...]:
        return tuple(
            sorted(self._sessions.values(), key=lambda item: (item.started_at_utc, item.session_id), reverse=True)
        )

    @property
    def projects(self) -> tuple[str, ...]:
        return tuple(sorted({session.project for session in self._sessions.values()}, key=str.casefold))

    def refresh(self) -> None:
        sessions: dict[str, CatalogSession] = {}
        diagnostics: list[str] = []
        if not self.journal_root.is_dir():
            self._sessions = {}
            self._children = {}
            self._details = {}
            self.diagnostics = ("journal directory is missing",)
            return
        for journal in sorted(self.journal_root.rglob("*.md")):
            relative = _safe_relative(journal, self.repo_root)
            try:
                journal.resolve().relative_to(self.journal_root)
                metadata = _read_front_matter(journal)
                if metadata.get("generated_by") != "codex-journal" or metadata.get("format_version") != 1:
                    raise CatalogError("unsupported generated journal format")
                session_id = _required_string(metadata, "session_id")
                if session_id in sessions:
                    raise CatalogError(f"duplicate generated session ID: {session_id}")
                status = _required_string(metadata, "status")
                if status not in {"active", "completed", "incomplete"}:
                    raise CatalogError("invalid metadata: status")
                provenance = journal.with_suffix(".provenance.json")
                if not provenance.is_file():
                    raise CatalogError("missing provenance companion")
                sessions[session_id] = CatalogSession(
                    session_id=session_id,
                    parent_session_id=_optional_string(metadata, "parent_session_id"),
                    status=status,
                    started_at_utc=_required_string(metadata, "started_at_utc"),
                    ended_at_utc=_optional_string(metadata, "ended_at_utc"),
                    rendered_timezone=_required_string(metadata, "rendered_timezone"),
                    working_directory=_optional_string(metadata, "working_directory"),
                    repository=_optional_string(metadata, "repository"),
                    branch=_optional_string(metadata, "branch"),
                    source_kind=str(metadata.get("source_kind") or "unknown"),
                    source_fingerprint=_required_string(metadata, "source_fingerprint"),
                    entry_count=_nonnegative_int(metadata, "timeline_entries"),
                    redaction_count=_nonnegative_int(metadata, "redactions"),
                    extraction_error_count=_nonnegative_int(metadata, "extraction_errors"),
                    journal_path=journal.resolve(),
                    provenance_path=provenance.resolve(),
                )
            except CatalogError as exc:
                diagnostics.append(f"{relative}: {exc}")
        children: dict[str, list[str]] = {}
        for session in sessions.values():
            if session.parent_session_id and session.parent_session_id in sessions:
                children.setdefault(session.parent_session_id, []).append(session.session_id)
        retained = set(sessions)
        self._details = {key: value for key, value in self._details.items() if key in retained}
        self._sessions = sessions
        self._children = {key: tuple(sorted(value)) for key, value in children.items()}
        self.diagnostics = tuple(diagnostics)

    def get(self, session_id: str) -> CatalogSession | None:
        return self._sessions.get(session_id)

    def children_of(self, session_id: str) -> tuple[CatalogSession, ...]:
        return tuple(self._sessions[child] for child in self._children.get(session_id, ()))

    def parent_of(self, session_id: str) -> CatalogSession | None:
        session = self._sessions.get(session_id)
        return self._sessions.get(session.parent_session_id) if session and session.parent_session_id else None

    def load_detail(self, session_id: str) -> CatalogDetail:
        session = self._sessions.get(session_id)
        if session is None:
            raise CatalogError("session is not present in the generated catalog")
        cached = self._details.get(session_id)
        if cached and cached[0] == session.source_fingerprint:
            return cached[1]
        journal_text = _read_bounded_text(session.journal_path, self.max_journal_bytes, "generated journal")
        provenance_text = _read_bounded_text(
            session.provenance_path, self.max_provenance_bytes, "provenance companion"
        )
        timeline: list[tuple[str, str]] = []
        in_timeline = False
        for line in journal_text.splitlines():
            if line == "## Timeline":
                in_timeline = True
                continue
            if in_timeline and line.startswith("## "):
                break
            if in_timeline and len(line) >= 8 and line[2] == ":" and line[5:7] == "  ":
                timeline.append((line[:5], line[7:]))
        try:
            provenance = json.loads(provenance_text)
        except json.JSONDecodeError as exc:
            raise CatalogError("provenance companion is malformed JSON") from exc
        if not isinstance(provenance, dict):
            raise CatalogError("provenance companion has invalid structure")
        if provenance.get("generated_by") != "codex-journal" or provenance.get("format_version") != 1:
            raise CatalogError("unsupported provenance format")
        if provenance.get("session_id") != session.session_id:
            raise CatalogError("provenance session ID mismatch")
        raw_entries = provenance.get("entries")
        if not isinstance(raw_entries, list) or len(raw_entries) != len(timeline):
            raise CatalogError("timeline and provenance entry counts differ")
        entries: list[CatalogEntry] = []
        for index, (display_time, text) in enumerate(timeline):
            raw = raw_entries[index]
            if not isinstance(raw, dict) or raw.get("normalized_text") != text:
                raise CatalogError("timeline and provenance text differ")
            sequence = raw.get("source_event_sequence")
            timestamp = raw.get("original_timestamp_utc")
            text_hash = raw.get("original_text_sha256")
            redacted = raw.get("redacted")
            if (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or not isinstance(timestamp, str)
                or not isinstance(text_hash, str)
                or not isinstance(redacted, bool)
            ):
                raise CatalogError("provenance entry has invalid fields")
            entries.append(
                CatalogEntry(
                    index=index,
                    display_time=display_time,
                    text=text,
                    source_event_sequence=sequence,
                    original_timestamp_utc=timestamp,
                    original_text_sha256=text_hash,
                    redacted=redacted,
                )
            )
        raw_errors = provenance.get("extraction_errors", [])
        if not isinstance(raw_errors, list):
            raise CatalogError("provenance extraction errors have invalid structure")
        extraction_errors: list[CatalogExtractionError] = []
        for raw in raw_errors:
            if not isinstance(raw, dict):
                raise CatalogError("provenance extraction error has invalid fields")
            sequence = raw.get("sequence")
            code = raw.get("code")
            detail = raw.get("detail", "")
            if not isinstance(sequence, int) or not isinstance(code, str) or not isinstance(detail, str):
                raise CatalogError("provenance extraction error has invalid fields")
            extraction_errors.append(CatalogExtractionError(sequence, code, detail))
        detail = CatalogDetail(session, tuple(entries), tuple(extraction_errors))
        self._details[session_id] = (session.source_fingerprint, detail)
        return detail


class JournalSearchIndex:
    """Ignored, rebuildable FTS index containing sanitized generated text only."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        try:
            self.connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS journal_search USING fts5(
                    session_id UNINDEXED,
                    entry_index UNINDEXED,
                    timestamp_utc UNINDEXED,
                    text,
                    project,
                    branch,
                    status UNINDEXED,
                    source_kind UNINDEXED,
                    tokenize = 'unicode61'
                )
                """
            )
        except sqlite3.Error as exc:
            self.connection.close()
            raise CatalogError("SQLite FTS5 is unavailable for viewer search") from exc

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> JournalSearchIndex:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def rebuild(self, catalog: JournalCatalog) -> int:
        rows: list[tuple[Any, ...]] = []
        for session in catalog.sessions:
            detail = catalog.load_detail(session.session_id)
            rows.extend(
                (
                    session.session_id,
                    entry.index,
                    entry.original_timestamp_utc,
                    entry.text,
                    session.project,
                    session.branch or "",
                    session.status,
                    session.source_kind,
                )
                for entry in detail.entries
            )
        with self.connection:
            self.connection.execute("DELETE FROM journal_search")
            self.connection.executemany(
                """
                INSERT INTO journal_search(
                    session_id, entry_index, timestamp_utc, text, project, branch, status, source_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def search(self, query: str, *, limit: int = 100) -> tuple[SearchHit, ...]:
        cleaned = " ".join(query.split())
        if not cleaned:
            return ()
        phrase = '"' + cleaned.replace('"', '""') + '"'
        try:
            rows = self.connection.execute(
                """
                SELECT session_id, entry_index, timestamp_utc, text, project, branch
                FROM journal_search
                WHERE journal_search MATCH ?
                ORDER BY timestamp_utc DESC, session_id, entry_index
                LIMIT ?
                """,
                (phrase, max(1, min(limit, 1000))),
            ).fetchall()
        except sqlite3.Error as exc:
            raise CatalogError("viewer search query failed safely") from exc
        return tuple(SearchHit(row[0], int(row[1]), row[2], row[3], row[4], row[5] or None) for row in rows)

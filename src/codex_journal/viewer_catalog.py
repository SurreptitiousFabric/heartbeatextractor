from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from .artifacts import (
    MAX_JOURNAL_BYTES,
    MAX_PROVENANCE_BYTES,
    DecodedJournal,
    DecodedProvenance,
    decode_journal,
    decode_provenance,
)
from .viewer_tags import TAGS, classify_entry


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


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _require_journal(decoded: DecodedJournal) -> DecodedJournal:
    if decoded.findings:
        raise CatalogError(decoded.findings[0].message)
    if decoded.metadata is None:
        raise CatalogError("generated journal metadata is unavailable")
    return decoded


def _require_provenance(decoded: DecodedProvenance) -> DecodedProvenance:
    if decoded.findings:
        raise CatalogError(decoded.findings[0].message)
    if decoded.artifact is None:
        raise CatalogError("provenance companion is unavailable")
    return decoded


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
                decoded = _require_journal(decode_journal(journal))
                metadata = decoded.metadata
                assert metadata is not None
                session_id = metadata.session_id
                if session_id in sessions:
                    raise CatalogError(f"duplicate generated session ID: {session_id}")
                provenance = journal.with_suffix(".provenance.json")
                if not provenance.is_file():
                    raise CatalogError("missing provenance companion")
                sessions[session_id] = CatalogSession(
                    session_id=session_id,
                    parent_session_id=metadata.parent_session_id,
                    status=metadata.status,
                    started_at_utc=metadata.started_at_utc,
                    ended_at_utc=metadata.ended_at_utc,
                    rendered_timezone=metadata.rendered_timezone,
                    working_directory=metadata.working_directory,
                    repository=metadata.repository,
                    branch=metadata.branch,
                    source_kind=metadata.source_kind,
                    source_fingerprint=metadata.source_fingerprint,
                    entry_count=metadata.timeline_entries,
                    redaction_count=metadata.redactions,
                    extraction_error_count=metadata.extraction_errors,
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

    def load_detail(self, session_id: str, *, cache: bool = True) -> CatalogDetail:
        session = self._sessions.get(session_id)
        if session is None:
            raise CatalogError("session is not present in the generated catalog")
        cached = self._details.get(session_id) if cache else None
        if cached and cached[0] == session.source_fingerprint:
            return cached[1]
        journal = _require_journal(
            decode_journal(session.journal_path, max_journal_bytes=self.max_journal_bytes)
        )
        decoded_provenance = _require_provenance(
            decode_provenance(
                session.provenance_path,
                max_provenance_bytes=self.max_provenance_bytes,
            )
        )
        provenance = decoded_provenance.artifact
        assert provenance is not None
        if provenance.session_id != session.session_id:
            raise CatalogError("provenance session ID mismatch")
        if len(provenance.entries) != len(journal.timeline):
            raise CatalogError("timeline and provenance entry counts differ")
        entries: list[CatalogEntry] = []
        for index, (timeline, source) in enumerate(zip(journal.timeline, provenance.entries)):
            if source.normalized_text != timeline.text:
                raise CatalogError("timeline and provenance text differ")
            entries.append(
                CatalogEntry(
                    index=index,
                    display_time=timeline.display_time,
                    text=timeline.text,
                    source_event_sequence=source.source_event_sequence,
                    original_timestamp_utc=source.original_timestamp_utc,
                    original_text_sha256=source.original_text_sha256,
                    redacted=source.redacted,
                )
            )
        extraction_errors = [
            CatalogExtractionError(error.sequence, error.code, error.detail)
            for error in provenance.extraction_errors
        ]
        detail = CatalogDetail(session, tuple(entries), tuple(extraction_errors))
        if cache:
            self._details[session_id] = (session.source_fingerprint, detail)
        return detail


class JournalSearchIndex:
    """Ignored, rebuildable FTS index containing sanitized generated text only."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        if path.is_symlink():
            raise CatalogError("refusing symbolic-link viewer search state")
        try:
            self.connection = sqlite3.connect(path)
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS viewer_index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            version = self.connection.execute(
                "SELECT value FROM viewer_index_meta WHERE key = 'schema_version'"
            ).fetchone()
            if version != ("3",):
                self.connection.execute("DROP TABLE IF EXISTS journal_search")
                self.connection.execute(
                    "INSERT OR REPLACE INTO viewer_index_meta(key, value) VALUES ('schema_version', '3')"
                )
            self.connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS journal_search USING fts5(
                    session_id UNINDEXED,
                    entry_index UNINDEXED,
                    timestamp_utc UNINDEXED,
                    text,
                    tags,
                    tokenize = 'unicode61'
                )
                """
            )
        except sqlite3.Error as exc:
            if hasattr(self, "connection"):
                self.connection.close()
            raise CatalogError("viewer search state is unavailable or malformed") from exc

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> JournalSearchIndex:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def rebuild(self, catalog: JournalCatalog) -> int:
        count = 0
        with self.connection:
            self.connection.execute("DELETE FROM journal_search")
            for session in catalog.sessions:
                detail = catalog.load_detail(session.session_id, cache=False)
                rows = [
                    (
                        session.session_id,
                        entry.index,
                        entry.original_timestamp_utc,
                        entry.text,
                        " ".join(classify_entry(entry.text)),
                    )
                    for entry in detail.entries
                ]
                if rows:
                    self.connection.executemany(
                        """
                        INSERT INTO journal_search(
                            session_id, entry_index, timestamp_utc, text, tags
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                    count += len(rows)
        return count

    def session_summaries(self) -> dict[str, str]:
        """Return each session's first sanitized journal entry."""

        try:
            rows = self.connection.execute(
                """
                SELECT session_id, text
                FROM journal_search
                WHERE entry_index = 0
                ORDER BY session_id
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise CatalogError("viewer session summaries are unavailable") from exc
        return {str(session_id): str(text) for session_id, text in rows}

    def search(
        self,
        query: str,
        *,
        tags: tuple[str, ...] = (),
        limit: int = 100,
    ) -> tuple[SearchHit, ...]:
        cleaned = " ".join(query.split())
        if any(tag not in TAGS for tag in tags):
            raise CatalogError("unknown deterministic search tag")
        if not cleaned and not tags:
            return ()
        clauses: list[str] = []
        parameters: list[object] = []
        if cleaned:
            clauses.append("journal_search MATCH ?")
            parameters.append('"' + cleaned.replace('"', '""') + '"')
        for tag in tags:
            clauses.append("(' ' || tags || ' ') LIKE ?")
            parameters.append(f"% {tag} %")
        where = " AND ".join(clauses) if clauses else "1 = 1"
        parameters.append(max(1, min(limit, 1000)))
        try:
            rows = self.connection.execute(
                f"""
                SELECT session_id, entry_index, timestamp_utc, text
                FROM journal_search
                WHERE {where}
                ORDER BY timestamp_utc DESC, session_id, entry_index
                LIMIT ?
                """,  # nosec B608: predicates are selected from constants above
                parameters,
            ).fetchall()
        except sqlite3.Error as exc:
            raise CatalogError("viewer search query failed safely") from exc
        return tuple(
            SearchHit(
                row[0],
                int(row[1]),
                row[2],
                row[3],
            )
            for row in rows
        )

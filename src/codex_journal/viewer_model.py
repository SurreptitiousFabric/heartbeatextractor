from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from .viewer_catalog import CatalogSession, JournalCatalog, SearchHit


ALL = "All"


@dataclass(frozen=True)
class BrowserFilters:
    project: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    branch: str | None = None
    status: str | None = None
    source_kind: str | None = None
    redacted_only: bool = False
    extraction_errors_only: bool = False
    bookmarked_only: bool = False
    tag: str | None = None


@dataclass(frozen=True)
class BrowserCounts:
    visible: int
    total: int
    projects: int
    warnings: int


class SessionBrowserModel:
    """Deterministic filtering and selection for the native viewer."""

    def __init__(self, catalog: JournalCatalog) -> None:
        self.catalog = catalog
        self.filters = BrowserFilters()
        self.selected_session_id: str | None = None
        self._search_matches: dict[str, int] | None = None
        self._bookmarked_session_ids: frozenset[str] = frozenset()

    @property
    def sessions(self) -> tuple[CatalogSession, ...]:
        filters = self.filters
        return tuple(
            session
            for session in self.catalog.sessions
            if (filters.project is None or session.project == filters.project)
            and (filters.date_from is None or session.local_date >= filters.date_from)
            and (filters.date_to is None or session.local_date <= filters.date_to)
            and (filters.branch is None or session.branch == filters.branch)
            and (filters.status is None or session.status == filters.status)
            and (filters.source_kind is None or session.source_kind == filters.source_kind)
            and (not filters.redacted_only or session.redaction_count > 0)
            and (not filters.extraction_errors_only or session.extraction_error_count > 0)
            and (not filters.bookmarked_only or session.session_id in self._bookmarked_session_ids)
            and (self._search_matches is None or session.session_id in self._search_matches)
        )

    @property
    def selected(self) -> CatalogSession | None:
        if self.selected_session_id is None:
            return None
        return self.catalog.get(self.selected_session_id)

    @property
    def projects(self) -> tuple[str, ...]:
        return self.catalog.projects

    @property
    def dates(self) -> tuple[str, ...]:
        return tuple(sorted({session.local_date for session in self.catalog.sessions}, reverse=True))

    @property
    def branches(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {session.branch for session in self.catalog.sessions if session.branch},
                key=str.casefold,
            )
        )

    @property
    def statuses(self) -> tuple[str, ...]:
        preferred = ("active", "incomplete", "completed")
        available = {session.status for session in self.catalog.sessions}
        return tuple(status for status in preferred if status in available)

    @property
    def source_kinds(self) -> tuple[str, ...]:
        return tuple(sorted({session.source_kind for session in self.catalog.sessions}, key=str.casefold))

    @property
    def counts(self) -> BrowserCounts:
        visible = self.sessions
        return BrowserCounts(
            visible=len(visible),
            total=len(self.catalog.sessions),
            projects=len(self.catalog.projects),
            warnings=sum(
                session.redaction_count + session.extraction_error_count
                for session in visible
            )
            + len(self.catalog.diagnostics),
        )

    def set_filter(self, field: str, value: str | bool | None) -> tuple[CatalogSession, ...]:
        if field not in BrowserFilters.__dataclass_fields__:
            raise ValueError(f"unknown browser filter: {field}")
        normalized = value if value and value != ALL else (False if isinstance(value, bool) else None)
        self.filters = replace(self.filters, **{field: normalized})
        visible = self.sessions
        if self.selected_session_id not in {session.session_id for session in visible}:
            self.selected_session_id = visible[0].session_id if visible else None
        return visible

    def set_search_hits(
        self, hits: tuple[SearchHit, ...], *, active: bool
    ) -> tuple[CatalogSession, ...]:
        matches: dict[str, int] = {}
        for hit in hits:
            matches.setdefault(hit.session_id, hit.entry_index)
        self._search_matches = matches if active else None
        visible = self.sessions
        if self.selected_session_id not in {session.session_id for session in visible}:
            self.selected_session_id = visible[0].session_id if visible else None
        return visible

    def set_bookmarked_session_ids(self, session_ids: frozenset[str]) -> tuple[CatalogSession, ...]:
        self._bookmarked_session_ids = session_ids
        visible = self.sessions
        if self.selected_session_id not in {session.session_id for session in visible}:
            self.selected_session_id = visible[0].session_id if visible else None
        return visible

    def matching_entry(self, session_id: str) -> int | None:
        return self._search_matches.get(session_id) if self._search_matches is not None else None

    def select(self, session_id: str | None) -> CatalogSession | None:
        if session_id is None:
            self.selected_session_id = None
            return None
        session = self.catalog.get(session_id)
        if session is None or session not in self.sessions:
            raise ValueError("session is not visible in the current browser")
        self.selected_session_id = session_id
        return session

    def select_first(self) -> CatalogSession | None:
        sessions = self.sessions
        return self.select(sessions[0].session_id) if sessions else self.select(None)


def display_start(session: CatalogSession) -> str:
    """Return the stable date/time label carried by generated metadata."""

    try:
        parsed = datetime.fromisoformat(session.started_at_utc.replace("Z", "+00:00"))
        return f"{session.local_date}  {parsed:%H:%M} UTC"
    except ValueError:
        return f"{session.local_date}  time unavailable"


def session_badges(session: CatalogSession) -> tuple[str, ...]:
    badges: list[str] = [session.status]
    if session.entry_count == 0:
        badges.append("no heartbeats")
    if session.extraction_error_count:
        badges.append(f"{session.extraction_error_count} extraction error(s)")
    if session.redaction_count:
        badges.append(f"{session.redaction_count} redaction(s)")
    if session.source_kind == "subagent":
        badges.append("sub-agent")
    return tuple(badges)

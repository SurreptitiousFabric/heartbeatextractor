from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from .viewer_catalog import CatalogSession, JournalCatalog


ALL = "All"


@dataclass(frozen=True)
class BrowserFilters:
    project: str | None = None
    local_date: str | None = None
    branch: str | None = None
    status: str | None = None


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

    @property
    def sessions(self) -> tuple[CatalogSession, ...]:
        filters = self.filters
        return tuple(
            session
            for session in self.catalog.sessions
            if (filters.project is None or session.project == filters.project)
            and (filters.local_date is None or session.local_date == filters.local_date)
            and (filters.branch is None or session.branch == filters.branch)
            and (filters.status is None or session.status == filters.status)
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

    def set_filter(self, field: str, value: str | None) -> tuple[CatalogSession, ...]:
        if field not in BrowserFilters.__dataclass_fields__:
            raise ValueError(f"unknown browser filter: {field}")
        normalized = value if value and value != ALL else None
        self.filters = replace(self.filters, **{field: normalized})
        visible = self.sessions
        if self.selected_session_id not in {session.session_id for session in visible}:
            self.selected_session_id = visible[0].session_id if visible else None
        return visible

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

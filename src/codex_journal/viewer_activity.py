from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .viewer_catalog import JournalCatalog
from .viewer_presenter import present_entry
from .viewer_tags import TAGS


@dataclass(frozen=True)
class ActivityBucket:
    key: str
    start_date: str
    end_date: str
    session_ids: tuple[str, ...]
    session_statuses: tuple[tuple[str, str], ...]
    session_projects: tuple[tuple[str, str], ...]
    entry_refs: tuple[tuple[str, int], ...]
    statuses: tuple[tuple[str, int], ...]
    projects: tuple[tuple[str, int], ...]
    tags: tuple[tuple[str, int], ...]

    @property
    def entries(self) -> int:
        return len(self.entry_refs)


@dataclass(frozen=True)
class ProjectCalendar:
    project: str
    days: tuple[ActivityBucket, ...]


@dataclass(frozen=True)
class ActivityReport:
    days: tuple[ActivityBucket, ...]
    weeks: tuple[ActivityBucket, ...]
    projects: tuple[ProjectCalendar, ...]


@dataclass
class _Accumulator:
    session_ids: set[str] = field(default_factory=set)
    entry_refs: list[tuple[str, int]] = field(default_factory=list)
    statuses_by_session: dict[str, str] = field(default_factory=dict)
    projects_by_session: dict[str, str] = field(default_factory=dict)
    tags: dict[str, int] = field(default_factory=dict)

    def add_session(self, session_id: str, status: str, project: str) -> None:
        self.session_ids.add(session_id)
        self.statuses_by_session[session_id] = status
        self.projects_by_session[session_id] = project

    def add_entry(self, session_id: str, index: int, tags: tuple[str, ...]) -> None:
        self.entry_refs.append((session_id, index))
        for tag in tags:
            self.tags[tag] = self.tags.get(tag, 0) + 1


def build_activity_report(catalog: JournalCatalog) -> ActivityReport:
    days: dict[str, _Accumulator] = {}
    project_days: dict[str, dict[str, _Accumulator]] = {}
    for session in catalog.sessions:
        session_day = days.setdefault(session.local_date, _Accumulator())
        session_day.add_session(session.session_id, session.status, session.project)
        project_session_day = project_days.setdefault(session.project, {}).setdefault(
            session.local_date, _Accumulator()
        )
        project_session_day.add_session(session.session_id, session.status, session.project)
        detail = catalog.load_detail(session.session_id, cache=False)
        for entry in detail.entries:
            presented = present_entry(entry, session)
            day_key = presented.local_date
            day = days.setdefault(day_key, _Accumulator())
            day.add_session(session.session_id, session.status, session.project)
            day.add_entry(session.session_id, entry.index, presented.tags)
            project_day = project_days.setdefault(session.project, {}).setdefault(
                day_key, _Accumulator()
            )
            project_day.add_session(session.session_id, session.status, session.project)
            project_day.add_entry(session.session_id, entry.index, presented.tags)
    daily = tuple(
        _freeze_day(key, accumulator)
        for key, accumulator in sorted(days.items(), reverse=True)
    )
    weekly = _build_weeks(daily)
    projects = tuple(
        ProjectCalendar(
            project,
            tuple(
                _freeze_day(key, accumulator)
                for key, accumulator in sorted(values.items(), reverse=True)
            ),
        )
        for project, values in sorted(project_days.items(), key=lambda item: item[0].casefold())
    )
    return ActivityReport(daily, weekly, projects)


def fill_daily_range(
    report: ActivityReport, start_date: str, end_date: str
) -> tuple[ActivityBucket, ...]:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start:
        raise ValueError("activity range ends before it starts")
    existing = {bucket.key: bucket for bucket in report.days}
    result: list[ActivityBucket] = []
    current = start
    while current <= end:
        key = current.isoformat()
        result.append(existing.get(key, _empty_bucket(key, key, key)))
        current += timedelta(days=1)
    return tuple(result)


def _freeze_day(key: str, accumulator: _Accumulator) -> ActivityBucket:
    return _freeze(key, key, key, accumulator)


def _freeze(key: str, start: str, end: str, accumulator: _Accumulator) -> ActivityBucket:
    statuses: dict[str, int] = {}
    projects: dict[str, int] = {}
    for status in accumulator.statuses_by_session.values():
        statuses[status] = statuses.get(status, 0) + 1
    for project in accumulator.projects_by_session.values():
        projects[project] = projects.get(project, 0) + 1
    return ActivityBucket(
        key,
        start,
        end,
        tuple(sorted(accumulator.session_ids)),
        tuple(sorted(accumulator.statuses_by_session.items())),
        tuple(sorted(accumulator.projects_by_session.items())),
        tuple(accumulator.entry_refs),
        tuple(sorted(statuses.items())),
        tuple(sorted(projects.items(), key=lambda item: item[0].casefold())),
        tuple((tag, accumulator.tags.get(tag, 0)) for tag in TAGS if accumulator.tags.get(tag)),
    )


def _build_weeks(days: tuple[ActivityBucket, ...]) -> tuple[ActivityBucket, ...]:
    weeks: dict[str, _Accumulator] = {}
    bounds: dict[str, tuple[str, str]] = {}
    for day in days:
        parsed = date.fromisoformat(day.key)
        year, week, _weekday = parsed.isocalendar()
        key = f"{year}-W{week:02d}"
        start = parsed - timedelta(days=parsed.weekday())
        end = start + timedelta(days=6)
        bounds[key] = (start.isoformat(), end.isoformat())
        accumulator = weeks.setdefault(key, _Accumulator())
        statuses = dict(day.session_statuses)
        projects = dict(day.session_projects)
        for session_id in day.session_ids:
            accumulator.add_session(
                session_id,
                statuses.get(session_id, "incomplete"),
                projects.get(session_id, "Unknown project"),
            )
        accumulator.entry_refs.extend(day.entry_refs)
        for tag, count in day.tags:
            accumulator.tags[tag] = accumulator.tags.get(tag, 0) + count
    return tuple(
        _freeze(key, bounds[key][0], bounds[key][1], accumulator)
        for key, accumulator in sorted(weeks.items(), reverse=True)
    )


def _empty_bucket(key: str, start: str, end: str) -> ActivityBucket:
    return ActivityBucket(key, start, end, (), (), (), (), (), (), ())

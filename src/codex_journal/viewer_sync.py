from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .atomic import atomic_replace
from .viewer_catalog import JournalCatalog, JournalSearchIndex


@dataclass(frozen=True)
class SessionSnapshot:
    source_fingerprint: str
    entry_count: int
    status: str
    extraction_error_count: int


@dataclass(frozen=True)
class CatalogSnapshot:
    sessions: dict[str, SessionSnapshot]

    @classmethod
    def from_catalog(cls, catalog: JournalCatalog) -> CatalogSnapshot:
        return cls(
            {
                session.session_id: SessionSnapshot(
                    session.source_fingerprint,
                    session.entry_count,
                    session.status,
                    session.extraction_error_count,
                )
                for session in catalog.sessions
            }
        )


@dataclass(frozen=True)
class ChangeSummary:
    new_sessions: int = 0
    changed_sessions: int = 0
    new_entries: int = 0
    lifecycle_changes: int = 0
    new_extraction_errors: int = 0

    @property
    def changed(self) -> bool:
        return any(
            (
                self.new_sessions,
                self.changed_sessions,
                self.new_entries,
                self.lifecycle_changes,
                self.new_extraction_errors,
            )
        )

    def describe(self) -> str:
        if not self.changed:
            return "No generated journal changes."
        return (
            f"{self.new_sessions} new session(s), {self.changed_sessions} changed session(s), "
            f"{self.new_entries} new entry/entries, {self.lifecycle_changes} lifecycle change(s), "
            f"{self.new_extraction_errors} new extraction error(s)."
        )


def compare_snapshots(before: CatalogSnapshot, after: CatalogSnapshot) -> ChangeSummary:
    new_ids = after.sessions.keys() - before.sessions.keys()
    common = after.sessions.keys() & before.sessions.keys()
    changed = {
        session_id
        for session_id in common
        if after.sessions[session_id].source_fingerprint
        != before.sessions[session_id].source_fingerprint
    }
    return ChangeSummary(
        new_sessions=len(new_ids),
        changed_sessions=len(changed),
        new_entries=sum(after.sessions[item].entry_count for item in new_ids)
        + sum(
            max(0, after.sessions[item].entry_count - before.sessions[item].entry_count)
            for item in common
        ),
        lifecycle_changes=sum(
            after.sessions[item].status != before.sessions[item].status for item in common
        ),
        new_extraction_errors=sum(
            after.sessions[item].extraction_error_count for item in new_ids
        )
        + sum(
            max(
                0,
                after.sessions[item].extraction_error_count
                - before.sessions[item].extraction_error_count,
            )
            for item in common
        ),
    )


def rebuild_search_index_atomic(catalog: JournalCatalog, destination: Path) -> int:
    """Build local search state beside its destination, then atomically replace it."""

    def rebuild(temporary: Path) -> int:
        with JournalSearchIndex(temporary) as index:
            return index.rebuild(catalog)

    return atomic_replace(destination, rebuild)

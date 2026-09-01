from __future__ import annotations

import os
from pathlib import Path
from typing import assert_never

from .model import ExtractionMode, SessionCache, SourceSession, SyncResult, VerifyResult
from .parser import (
    DEFAULT_MAX_RECORD_BYTES,
    discover_sessions,
    duplicate_session_ids,
    extract_session,
)
from .redact import shorten_home
from .render import journal_relative_path, render_indexes, render_journal, resolve_timezone
from .state import StateStore
from .verify import verify_repository


class JournalEngine:
    def __init__(
        self,
        repo_root: Path,
        state_root: Path,
        *,
        home: Path | None = None,
        max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
    ):
        self.repo_root = repo_root.resolve()
        self.state_root = state_root.resolve()
        self.home = (home or Path.home()).resolve()
        self.max_record_bytes = max_record_bytes
        self.state_path = self.repo_root / "state" / "journal.sqlite3"

    def discover(self) -> tuple[list[SourceSession], list[str]]:
        return discover_sessions(self.state_root, max_record_bytes=self.max_record_bytes)

    def sync(
        self,
        *,
        session_id: str | None = None,
        timezone_name: str | None = None,
        force_rebuild: bool = False,
    ) -> SyncResult:
        zone, zone_name = resolve_timezone(timezone_name)
        sessions, discovery_errors = self.discover()
        result = SyncResult(discovered=len(sessions), errors=list(discovery_errors))
        duplicates = duplicate_session_ids(sessions)
        if session_id:
            selected = [session for session in sessions if session.session_id == session_id]
            if not selected:
                result.errors.append(f"session not found: {session_id}")
                return result
            if len(selected) > 1:
                result.errors.append(f"duplicate session ID: {session_id}")
                return result
        else:
            selected = [session for session in sessions if session.session_id not in duplicates]
            for duplicate_id in sorted(duplicates):
                result.errors.append(f"duplicate session ID skipped: {duplicate_id}")

        selected_ids = {session.session_id for session in selected}
        with StateStore(self.state_path) as store:
            processed: dict[str, SessionCache] = {}
            for source in selected:
                previous = store.get(source.session_id)
                if force_rebuild:
                    previous = None
                outcome = extract_session(
                    source,
                    previous,
                    home=self.home,
                    force_rebuild=force_rebuild,
                    max_record_bytes=self.max_record_bytes,
                )
                cache = outcome.cache
                processed[cache.session_id] = cache
                result.processed += 1
                match outcome.mode:
                    case ExtractionMode.UNCHANGED:
                        result.unchanged += 1
                    case ExtractionMode.APPEND:
                        result.appended += 1
                    case ExtractionMode.REBUILD:
                        result.rebuilt += 1
                    case impossible:
                        assert_never(impossible)
                store.save(cache)

            all_caches = {cache.session_id: cache for cache in store.all()}
            all_caches.update(processed)
            relation_paths: dict[str, Path] = {}
            for cache_id, cache in all_caches.items():
                target_zone = zone if cache_id in selected_ids else resolve_timezone(cache.rendered_timezone)[0]
                relation_paths[cache_id] = journal_relative_path(cache, target_zone)
            path_owners: dict[Path, list[str]] = {}
            for cache_id, path in relation_paths.items():
                path_owners.setdefault(path, []).append(cache_id)
            path_collision_found = False
            for path, owner_ids in sorted(path_owners.items(), key=lambda item: item[0].as_posix()):
                if len(owner_ids) > 1:
                    path_collision_found = True
                    result.errors.append(
                        f"journal path collision: {path.as_posix()} sessions={','.join(sorted(owner_ids))}"
                    )
            if path_collision_found:
                return result
            children: dict[str, list[str]] = {}
            for cache in all_caches.values():
                if cache.parent_session_id:
                    children.setdefault(cache.parent_session_id, []).append(cache.session_id)

            for cache_id in selected_ids:
                cache = all_caches[cache_id]
                target, entries, markdown_changed, provenance_changed = render_journal(
                    self.repo_root,
                    cache,
                    zone,
                    zone_name,
                    relation_paths,
                    children,
                    home=self.home,
                )
                store.save(cache)
                if markdown_changed:
                    result.written_paths.append(target)
                provenance = target.with_suffix(".provenance.json")
                if provenance_changed:
                    result.written_paths.append(provenance)
                if not entries:
                    result.no_heartbeats += 1
                if cache.status != "completed":
                    result.active_or_incomplete += 1
                if cache.errors:
                    result.sessions_with_errors += 1

            index_paths = render_indexes(self.repo_root, store.all(), home=self.home)
            result.written_paths.extend(path for path in index_paths if path not in result.written_paths)
        return result

    def rebuild(self, session_id: str, timezone_name: str | None = None) -> SyncResult:
        return self.sync(session_id=session_id, timezone_name=timezone_name, force_rebuild=True)

    def verify(self) -> VerifyResult:
        return verify_repository(
            self.repo_root,
            self.state_root,
            max_record_bytes=self.max_record_bytes,
        )


def default_state_root() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def format_discovery_line(session: SourceSession, home: Path) -> str:
    repository = session.repository or "—"
    branch = session.branch or "—"
    cwd = shorten_home(session.working_directory, home) or "—"
    return (
        f"{session.started_at_utc}  {session.session_id}  source={session.source_kind}  "
        f"cwd={cwd}  repository={repository}  branch={branch}"
    )

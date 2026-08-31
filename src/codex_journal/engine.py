from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .model import SessionCache, SourceSession, SyncResult, VerifyResult
from .parser import (
    DEFAULT_MAX_RECORD_BYTES,
    discover_sessions,
    duplicate_session_ids,
    extract_session,
    sha256_file,
)
from .redact import shorten_home
from .render import (
    count_timeline_entries,
    journal_relative_path,
    parse_front_matter,
    render_indexes,
    render_journal,
    resolve_timezone,
)
from .state import StateStore


LINK_RE = re.compile(r"\]\(([^)]+)\)")
REQUIRED_METADATA = {
    "session_id",
    "status",
    "started_at_utc",
    "rendered_timezone",
    "source_fingerprint",
    "generated_by",
    "format_version",
}


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
                cache, mode = extract_session(
                    source,
                    previous,
                    home=self.home,
                    force_rebuild=force_rebuild,
                    max_record_bytes=self.max_record_bytes,
                )
                store.save(cache)
                processed[cache.session_id] = cache
                result.processed += 1
                if mode == "unchanged":
                    result.unchanged += 1
                elif mode == "append":
                    result.appended += 1
                else:
                    result.rebuilt += 1

            all_caches = {cache.session_id: cache for cache in store.all()}
            all_caches.update(processed)
            relation_paths: dict[str, Path] = {}
            for cache_id, cache in all_caches.items():
                target_zone = zone if cache_id in selected_ids else resolve_timezone(cache.rendered_timezone)[0]
                relation_paths[cache_id] = journal_relative_path(cache, target_zone)
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
        result = VerifyResult()
        sessions, discovery_errors = self.discover()
        result.warnings.extend(discovery_errors)
        duplicates = duplicate_session_ids(sessions)
        for session_id in sorted(duplicates):
            result.errors.append(f"duplicate source session ID: {session_id}")
        source_by_id = {session.session_id: session for session in sessions if session.session_id not in duplicates}
        seen: dict[str, Path] = {}
        journals = sorted((self.repo_root / "journal").rglob("*.md"))
        for journal in journals:
            result.journals += 1
            metadata, metadata_errors = parse_front_matter(journal)
            result.errors.extend(f"{journal.relative_to(self.repo_root)}: {error}" for error in metadata_errors)
            missing = REQUIRED_METADATA - metadata.keys()
            if missing:
                result.errors.append(f"{journal.relative_to(self.repo_root)}: missing metadata {sorted(missing)}")
                continue
            session_id = metadata.get("session_id")
            if not isinstance(session_id, str):
                result.errors.append(f"{journal.relative_to(self.repo_root)}: invalid session_id")
                continue
            if session_id in seen:
                result.errors.append(
                    f"duplicate generated session ID {session_id}: {seen[session_id].relative_to(self.repo_root)} and {journal.relative_to(self.repo_root)}"
                )
            seen[session_id] = journal
            if metadata.get("generated_by") != "codex-journal" or metadata.get("format_version") != 1:
                result.errors.append(f"{journal.relative_to(self.repo_root)}: unsupported generator or format")
            source = source_by_id.get(session_id)
            if source is None:
                result.errors.append(f"{journal.relative_to(self.repo_root)}: source session not found")
            else:
                fingerprint = sha256_file(source.path)
                if fingerprint != metadata.get("source_fingerprint"):
                    result.errors.append(f"{journal.relative_to(self.repo_root)}: source fingerprint mismatch")
            entry_count = count_timeline_entries(journal)
            result.entries += entry_count
            if entry_count != metadata.get("timeline_entries"):
                result.errors.append(f"{journal.relative_to(self.repo_root)}: timeline entry count mismatch")
            provenance_path = journal.with_suffix(".provenance.json")
            if not provenance_path.is_file():
                result.errors.append(f"{journal.relative_to(self.repo_root)}: missing provenance")
                continue
            try:
                provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                result.errors.append(f"{provenance_path.relative_to(self.repo_root)}: malformed provenance: {exc}")
                continue
            provenance_entries = provenance.get("entries") if isinstance(provenance, dict) else None
            if not isinstance(provenance_entries, list) or len(provenance_entries) != entry_count:
                result.errors.append(f"{provenance_path.relative_to(self.repo_root)}: provenance entry count mismatch")
            elif any(entry.get("source_session_id") != session_id for entry in provenance_entries if isinstance(entry, dict)):
                result.errors.append(f"{provenance_path.relative_to(self.repo_root)}: provenance session mismatch")

        root_index_targets: set[Path] = set()
        for index in [self.repo_root / "INDEX.md", *sorted((self.repo_root / "projects").glob("*.md"))]:
            if not index.is_file():
                result.errors.append(f"missing index: {index.relative_to(self.repo_root)}")
                continue
            try:
                body = index.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                result.errors.append(f"{index.relative_to(self.repo_root)}: cannot read: {exc}")
                continue
            for target in LINK_RE.findall(body):
                if "://" in target or target.startswith("#"):
                    continue
                resolved = (index.parent / target).resolve()
                try:
                    resolved.relative_to(self.repo_root)
                except ValueError:
                    result.errors.append(f"{index.relative_to(self.repo_root)}: link escapes repository: {target}")
                    continue
                if not resolved.is_file():
                    result.errors.append(f"{index.relative_to(self.repo_root)}: broken link: {target}")
                elif index == self.repo_root / "INDEX.md":
                    root_index_targets.add(resolved)
        for journal in journals:
            if journal.resolve() not in root_index_targets:
                result.errors.append(f"INDEX.md: journal is not indexed: {journal.relative_to(self.repo_root)}")
        return result


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

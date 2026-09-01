from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from .model import (
    Candidate,
    ExtractionError,
    ExtractionMode,
    ExtractionOutcome,
    SessionCache,
    SourceSession,
)
from .redact import redact_text


DEFAULT_MAX_RECORD_BYTES = 4 * 1024 * 1024
LIFECYCLE_TYPES = {"task_started", "task_complete", "turn_aborted"}


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def sha256_file(path: Path, limit: int | None = None) -> str:
    digest = hashlib.sha256()
    remaining = limit
    with path.open("rb") as source:
        while remaining is None or remaining > 0:
            size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            block = source.read(size)
            if not block:
                break
            digest.update(block)
            if remaining is not None:
                remaining -= len(block)
    if remaining not in (None, 0):
        raise ValueError(f"source shorter than requested prefix: {path}")
    return digest.hexdigest()


def _drain_oversized_line(source: Any, chunk_size: int, end_offset: int | None) -> bool:
    while end_offset is None or source.tell() < end_offset:
        remaining = chunk_size if end_offset is None else min(chunk_size, end_offset - source.tell())
        if remaining <= 0:
            return False
        block = source.readline(remaining)
        if not block:
            return False
        if block.endswith(b"\n"):
            return True
    return False


def iter_complete_lines(
    path: Path,
    *,
    start_offset: int = 0,
    start_sequence: int = 0,
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
    end_offset: int | None = None,
) -> Iterator[tuple[int, int, int, bytes | None, str | None]]:
    """Yield complete bounded lines and errors without retaining oversized data."""

    with path.open("rb") as source:
        source.seek(start_offset)
        sequence = start_sequence
        while True:
            begin = source.tell()
            if end_offset is not None and begin >= end_offset:
                return
            read_limit = max_record_bytes + 1
            if end_offset is not None:
                read_limit = min(read_limit, end_offset - begin)
            raw = source.readline(read_limit)
            if not raw:
                return
            if len(raw) > max_record_bytes:
                if not raw.endswith(b"\n"):
                    if not _drain_oversized_line(source, max_record_bytes + 1, end_offset):
                        return
                yield sequence, begin, source.tell(), None, "oversized_record"
                sequence += 1
                continue
            if not raw.endswith(b"\n"):
                return
            yield sequence, begin, source.tell(), raw, None
            sequence += 1


def repository_name(repository_url: Any) -> str | None:
    if not isinstance(repository_url, str) or not repository_url.strip():
        return None
    value = repository_url.strip()
    if value.startswith("git@") and ":" in value:
        path = value.split(":", 1)[1]
    else:
        parsed = urlparse(value)
        path = parsed.path if parsed.scheme else value
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[0] if parts else None


def _metadata_from_payload(path: Path, state_root: Path, payload: dict[str, Any]) -> SourceSession | None:
    session_id = payload.get("id")
    started = payload.get("timestamp")
    if not isinstance(session_id, str) or parse_timestamp(started) is None:
        return None
    source = payload.get("source")
    parent_id = None
    source_kind = source if isinstance(source, str) else "unknown"
    if isinstance(source, dict):
        spawn = source.get("subagent", {}).get("thread_spawn", {})
        if isinstance(spawn, dict):
            candidate = spawn.get("parent_thread_id")
            if isinstance(candidate, str):
                parent_id = candidate
            source_kind = "subagent"
    git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
    branch = git.get("branch") if isinstance(git.get("branch"), str) else None
    repository = repository_name(git.get("repository_url"))
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    try:
        source_key = path.relative_to(state_root).as_posix()
    except ValueError:
        source_key = path.name
    return SourceSession(
        path=path,
        source_key=source_key,
        session_id=session_id,
        started_at_utc=started,
        working_directory=cwd,
        repository=repository,
        branch=branch,
        parent_session_id=parent_id,
        source_kind=source_kind,
    )


def read_session_metadata(
    path: Path,
    state_root: Path,
    *,
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
) -> tuple[SourceSession | None, list[str]]:
    errors: list[str] = []
    for sequence, _begin, _end, raw, line_error in iter_complete_lines(
        path, max_record_bytes=max_record_bytes
    ):
        if sequence >= 100:
            break
        if line_error:
            errors.append(f"record {sequence}: {line_error}")
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            errors.append(f"record {sequence}: malformed_json")
            continue
        if not isinstance(record, dict):
            errors.append(f"record {sequence}: non_object_json")
            continue
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            errors.append(f"record {sequence}: malformed_session_meta")
            continue
        metadata = _metadata_from_payload(path, state_root, payload)
        if metadata is None:
            errors.append(f"record {sequence}: invalid_session_meta")
        return metadata, errors
    errors.append("no valid session_meta in first 100 records")
    return None, errors


def discover_sessions(
    state_root: Path,
    *,
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
) -> tuple[list[SourceSession], list[str]]:
    sessions_root = state_root / "sessions"
    if not sessions_root.is_dir():
        return [], [f"session directory not found beneath {state_root}"]
    sessions: list[SourceSession] = []
    errors: list[str] = []
    for path in sorted(sessions_root.rglob("*.jsonl")):
        metadata, metadata_errors = read_session_metadata(
            path, state_root, max_record_bytes=max_record_bytes
        )
        if metadata is not None:
            sessions.append(metadata)
        for error in metadata_errors:
            errors.append(f"{path.name}: {error}")
    sessions.sort(key=lambda item: (parse_timestamp(item.started_at_utc), item.session_id), reverse=True)
    return sessions, errors


def duplicate_session_ids(sessions: list[SourceSession]) -> dict[str, list[SourceSession]]:
    by_id: dict[str, list[SourceSession]] = {}
    for session in sessions:
        by_id.setdefault(session.session_id, []).append(session)
    return {session_id: values for session_id, values in by_id.items() if len(values) > 1}


def _fresh_cache(source: SourceSession) -> SessionCache:
    return SessionCache(
        source_key=source.source_key,
        session_id=source.session_id,
        started_at_utc=source.started_at_utc,
        working_directory=source.working_directory,
        repository=source.repository,
        branch=source.branch,
        parent_session_id=source.parent_session_id,
        source_kind=source.source_kind,
    )


def _copy_source_metadata(cache: SessionCache, source: SourceSession) -> None:
    cache.source_key = source.source_key
    cache.started_at_utc = source.started_at_utc
    cache.working_directory = source.working_directory
    cache.repository = source.repository
    cache.branch = source.branch
    cache.parent_session_id = source.parent_session_id
    cache.source_kind = source.source_kind


def extract_session(
    source: SourceSession,
    previous: SessionCache | None,
    *,
    home: Path,
    force_rebuild: bool = False,
    max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
) -> ExtractionOutcome:
    """Extract a fixed source snapshot and describe how its cache was obtained."""

    source_size = source.path.stat().st_size
    # Work against a fixed byte snapshot. If the active file grows during this
    # call, the new suffix remains for the next append pass.
    fingerprint = sha256_file(source.path, source_size)
    mode = ExtractionMode.REBUILD
    cache: SessionCache
    start_offset = 0
    start_sequence = 0

    if previous is not None and not force_rebuild:
        if (
            previous.source_key == source.source_key
            and previous.source_size == source_size
            and previous.source_fingerprint == fingerprint
        ):
            _copy_source_metadata(previous, source)
            return ExtractionOutcome(previous, ExtractionMode.UNCHANGED)
        append_ok = (
            previous.source_key == source.source_key
            and source_size > previous.source_size
            and source_size >= previous.processed_offset
        )
        if append_ok:
            try:
                append_ok = sha256_file(source.path, previous.processed_offset) == previous.processed_prefix_sha256
            except ValueError:
                append_ok = False
        if append_ok:
            cache = previous
            _copy_source_metadata(cache, source)
            start_offset = cache.processed_offset
            start_sequence = cache.next_sequence
            mode = ExtractionMode.APPEND
        else:
            cache = _fresh_cache(source)
    else:
        cache = _fresh_cache(source)

    processed_offset = start_offset
    next_sequence = start_sequence
    for sequence, _begin, end, raw, line_error in iter_complete_lines(
        source.path,
        start_offset=start_offset,
        start_sequence=start_sequence,
        max_record_bytes=max_record_bytes,
        end_offset=source_size,
    ):
        processed_offset = end
        next_sequence = sequence + 1
        if line_error:
            cache.errors.append(ExtractionError(sequence, line_error))
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            cache.errors.append(ExtractionError(sequence, "malformed_json"))
            continue
        if not isinstance(record, dict):
            cache.errors.append(ExtractionError(sequence, "non_object_json"))
            continue
        record_type = record.get("type")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        timestamp = record.get("timestamp")

        if record_type == "event_msg" and payload.get("type") in LIFECYCLE_TYPES:
            if parse_timestamp(timestamp) is None:
                cache.errors.append(ExtractionError(sequence, "untimed_lifecycle_event"))
            else:
                cache.lifecycle_type = payload["type"]
                cache.lifecycle_timestamp_utc = timestamp

        if not (
            record_type == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "assistant"
            and payload.get("phase") == "commentary"
        ):
            continue
        if parse_timestamp(timestamp) is None:
            cache.errors.append(ExtractionError(sequence, "untimed_visible_event"))
            continue
        content = payload.get("content")
        if not isinstance(content, list):
            cache.errors.append(ExtractionError(sequence, "malformed_visible_content"))
            continue
        found_text = False
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "output_text":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                cache.errors.append(ExtractionError(sequence, "non_text_visible_content"))
                continue
            found_text = True
            original_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            safe_text, replacements = redact_text(text, home)
            cache.redaction_count += replacements
            if safe_text is None:
                cache.errors.append(ExtractionError(sequence, "environment_dump_omitted"))
                continue
            cache.candidates.append(
                Candidate(
                    sequence=sequence,
                    timestamp_utc=timestamp,
                    text=safe_text,
                    original_text_sha256=original_hash,
                    redacted=replacements > 0,
                )
            )
        if not found_text:
            cache.errors.append(ExtractionError(sequence, "visible_message_without_output_text"))

    cache.processed_offset = processed_offset
    cache.next_sequence = next_sequence
    cache.processed_prefix_sha256 = sha256_file(source.path, processed_offset)
    cache.source_size = source_size
    cache.source_fingerprint = fingerprint
    return ExtractionOutcome(cache, mode)


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown-project"


def project_slug(cache: SessionCache) -> str:
    if cache.repository:
        return safe_slug(cache.repository.rsplit("/", 1)[-1])
    if cache.working_directory:
        return safe_slug(Path(cache.working_directory).name)
    return "unknown-project"


def project_title(cache: SessionCache) -> str:
    slug = project_slug(cache)
    return " ".join(word.upper() if len(word) <= 2 else word.capitalize() for word in slug.split("-"))

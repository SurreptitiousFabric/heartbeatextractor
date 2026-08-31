from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .compact import compact_candidates
from .model import JournalEntry, SessionCache
from .parser import parse_timestamp, project_slug, project_title
from .redact import shorten_home


TIMELINE_RE = re.compile(r"^\d{2}:\d{2}  ")


def resolve_timezone(name: str | None) -> tuple[tzinfo, str]:
    if name:
        try:
            return ZoneInfo(name), name
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {name}") from exc
    environment_name = os.environ.get("TZ")
    candidates = [environment_name] if environment_name else []
    try:
        localtime = Path("/etc/localtime").resolve()
        marker = "/zoneinfo/"
        if marker in localtime.as_posix():
            candidates.append(localtime.as_posix().split(marker, 1)[1])
    except OSError:
        pass
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate), candidate
        except ZoneInfoNotFoundError:
            continue
    local = datetime.now().astimezone().tzinfo or timezone.utc
    key = getattr(local, "key", None)
    if key:
        return local, key
    # A fixed-offset fallback is serializable and can be resolved again as UTC.
    return timezone.utc, "UTC"


def atomic_write(path: Path, data: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == data:
        return False
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return True


def yaml_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def journal_relative_path(cache: SessionCache, zone: tzinfo) -> Path:
    started = parse_timestamp(cache.started_at_utc)
    if started is None:
        raise ValueError(f"invalid start timestamp for {cache.session_id}")
    local = started.astimezone(zone)
    compact_id = re.sub(r"[^A-Za-z0-9]", "", cache.session_id).lower()
    prefix = compact_id[:8] or "session"
    digest = hashlib.sha256(cache.session_id.encode("utf-8")).hexdigest()[:12]
    short_id = f"{prefix}-{digest}"
    filename = f"{local:%H%M}-{project_slug(cache)}-{short_id}.md"
    return Path("journal") / f"{local:%Y}" / f"{local:%m}" / f"{local:%d}" / filename


def _relative_link(from_path: Path, to_path: Path) -> str:
    return os.path.relpath(to_path, from_path.parent).replace(os.sep, "/")


def render_journal(
    repo_root: Path,
    cache: SessionCache,
    zone: tzinfo,
    timezone_name: str,
    relation_paths: dict[str, Path],
    children: dict[str, list[str]],
    *,
    home: Path,
) -> tuple[Path, list[JournalEntry], bool, bool]:
    entries = compact_candidates(cache.candidates)
    relative = journal_relative_path(cache, zone)
    target = repo_root / relative
    started = parse_timestamp(cache.started_at_utc)
    assert started is not None
    local_started = started.astimezone(zone)
    metadata = [
        ("session_id", cache.session_id),
        ("parent_session_id", cache.parent_session_id),
        ("status", cache.status),
        ("started_at_utc", cache.started_at_utc),
        ("ended_at_utc", cache.ended_at_utc),
        ("rendered_timezone", timezone_name),
        ("working_directory", shorten_home(cache.working_directory, home)),
        ("repository", cache.repository),
        ("branch", cache.branch),
        ("source_kind", cache.source_kind),
        ("source_fingerprint", cache.source_fingerprint),
        ("timeline_entries", len(entries)),
        ("redactions", cache.redaction_count),
        ("extraction_errors", len(cache.errors)),
        ("generated_by", "codex-journal"),
        ("format_version", 1),
    ]
    lines = ["---", *(f"{key}: {yaml_scalar(value)}" for key, value in metadata), "---", ""]
    lines.extend(
        [
            f"# {project_title(cache)} — {local_started.day} {local_started:%B %Y}",
            "",
            "## Timeline",
            "",
        ]
    )
    if entries:
        for entry in entries:
            event_time = parse_timestamp(entry.timestamp_utc)
            assert event_time is not None
            lines.append(f"{event_time.astimezone(zone):%H:%M}  {entry.text}")
    else:
        lines.append("No user-visible heartbeat entries were found in this session.")

    related: list[tuple[str, str, Path]] = []
    if cache.parent_session_id in relation_paths:
        parent_path = relation_paths[cache.parent_session_id]
        related.append(("Parent", cache.parent_session_id, parent_path))
    for child_id in sorted(children.get(cache.session_id, [])):
        if child_id in relation_paths:
            related.append(("Child", child_id, relation_paths[child_id]))
    if related:
        lines.extend(["", "## Related sessions", ""])
        for label, session_id, path in related:
            lines.append(f"- {label}: [{session_id}]({_relative_link(relative, path)})")
    lines.append("")
    markdown_changed = atomic_write(target, "\n".join(lines).encode("utf-8"))

    provenance = {
        "format_version": 1,
        "generated_by": "codex-journal",
        "session_id": cache.session_id,
        "source_fingerprint": cache.source_fingerprint,
        "redaction_count": cache.redaction_count,
        "extraction_errors": [error.to_dict() for error in cache.errors],
        "entries": [
            {
                "source_session_id": cache.session_id,
                "source_event_sequence": entry.sequence,
                "original_timestamp_utc": entry.timestamp_utc,
                "original_text_sha256": entry.original_text_sha256,
                "normalized_text": entry.text,
                "redacted": entry.redacted,
            }
            for entry in entries
        ],
    }
    provenance_path = target.with_suffix(".provenance.json")
    provenance_changed = atomic_write(
        provenance_path,
        (json.dumps(provenance, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
    )
    cache.journal_relpath = relative.as_posix()
    cache.rendered_timezone = timezone_name
    cache.entry_count = len(entries)
    return target, entries, markdown_changed, provenance_changed


def render_indexes(repo_root: Path, caches: list[SessionCache], *, home: Path | None = None) -> list[Path]:
    available = [cache for cache in caches if cache.journal_relpath and (repo_root / cache.journal_relpath).is_file()]
    available.sort(key=lambda cache: (parse_timestamp(cache.started_at_utc), cache.session_id), reverse=True)
    root_lines = ["# Codex session journals", ""]
    if not available:
        root_lines.append("No sessions have been synchronized yet.")
    else:
        root_lines.extend(
            [
                "| Date and time | Project | Branch | Status | Entries | Journal |",
                "| --- | --- | --- | --- | ---: | --- |",
            ]
        )
        for cache in available:
            zone, _ = resolve_timezone(cache.rendered_timezone)
            start = parse_timestamp(cache.started_at_utc)
            assert start is not None
            local = start.astimezone(zone)
            project = cache.repository or shorten_home(cache.working_directory, home) or "Unknown"
            link = cache.journal_relpath
            root_lines.append(
                f"| {local:%Y-%m-%d %H:%M} | {project} | {cache.branch or '—'} | {cache.status} | {cache.entry_count} | [journal]({link}) |"
            )
    root_lines.append("")
    written = [repo_root / "INDEX.md"]
    atomic_write(written[0], "\n".join(root_lines).encode("utf-8"))

    grouped: dict[str, list[SessionCache]] = {}
    for cache in available:
        grouped.setdefault(project_slug(cache), []).append(cache)
    for slug, values in sorted(grouped.items()):
        path = repo_root / "projects" / f"{slug}.md"
        lines = [f"# {project_title(values[0])}", ""]
        for cache in values:
            zone, _ = resolve_timezone(cache.rendered_timezone)
            start = parse_timestamp(cache.started_at_utc)
            assert start is not None
            local = start.astimezone(zone)
            link = _relative_link(Path("projects") / path.name, Path(cache.journal_relpath or ""))
            branch = f" · {cache.branch}" if cache.branch else ""
            lines.append(
                f"- {local:%Y-%m-%d %H:%M} · {cache.status}{branch} · {cache.entry_count} entries · [journal]({link})"
            )
        lines.append("")
        atomic_write(path, "\n".join(lines).encode("utf-8"))
        written.append(path)
    return written


def parse_front_matter(path: Path) -> tuple[dict[str, object], list[str]]:
    errors: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return {}, [f"cannot read: {exc}"]
    if not lines or lines[0] != "---":
        return {}, ["missing opening metadata delimiter"]
    metadata: dict[str, object] = {}
    for line in lines[1:]:
        if line == "---":
            return metadata, errors
        if ": " not in line:
            errors.append(f"malformed metadata line: {line!r}")
            continue
        key, raw = line.split(": ", 1)
        try:
            metadata[key] = json.loads(raw)
        except json.JSONDecodeError:
            errors.append(f"malformed metadata value for {key}")
    errors.append("missing closing metadata delimiter")
    return metadata, errors


def count_timeline_entries(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if TIMELINE_RE.match(line))

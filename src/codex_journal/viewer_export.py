from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .atomic import atomic_write_bytes
from .viewer_activity import ActivityReport
from .viewer_annotations import AnnotationStore, AnnotationTarget
from .viewer_catalog import CatalogDetail, CatalogEntry
from .viewer_compare import ComparisonReport


PRIVACY_WARNING = (
    "Generated journals may still contain private project information. "
    "Review this export before any remote upload."
)
MAX_EXPORT_BYTES = 16 * 1024 * 1024
MAX_PREVIEW_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ExportSession:
    session_id: str
    project: str
    branch: str | None
    status: str
    started_at_utc: str
    ended_at_utc: str | None
    rendered_timezone: str
    entry_count: int
    redaction_count: int
    extraction_error_count: int


@dataclass(frozen=True)
class ExportEntry:
    session_id: str
    entry_index: int
    source_event_sequence: int
    display_time: str
    original_timestamp_utc: str
    text: str
    redacted: bool


@dataclass(frozen=True)
class ExportComparisonRow:
    kind: str
    left_session_id: str | None
    left_entry_index: int | None
    right_session_id: str | None
    right_entry_index: int | None


@dataclass(frozen=True)
class ExportActivityBucket:
    key: str
    start_date: str
    end_date: str
    session_ids: tuple[str, ...]
    entry_refs: tuple[tuple[str, int], ...]
    statuses: tuple[tuple[str, int], ...]
    projects: tuple[tuple[str, int], ...]
    tags: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class ExportNote:
    session_id: str
    source_event_sequence: int
    text: str
    updated_at_utc: str


@dataclass(frozen=True)
class ExportDocument:
    scope: str
    sessions: tuple[ExportSession, ...]
    entries: tuple[ExportEntry, ...] = ()
    comparison: tuple[ExportComparisonRow, ...] = ()
    activity: tuple[ExportActivityBucket, ...] = ()
    notes: tuple[ExportNote, ...] = ()
    privacy_warning: str = PRIVACY_WARNING


def selected_entries_document(
    detail: CatalogDetail,
    indexes: set[int],
    *,
    inclusive_range: bool = False,
) -> ExportDocument:
    if not indexes:
        raise ValueError("Select at least one generated timeline entry.")
    if inclusive_range:
        indexes = set(range(min(indexes), max(indexes) + 1))
        scope = "selected time range"
    else:
        scope = "selected entries"
    entries = tuple(_entry(detail.session.session_id, item) for item in detail.entries if item.index in indexes)
    if not entries:
        raise ValueError("The selected generated entries are unavailable.")
    return ExportDocument(scope, (_session(detail),), entries)


def comparison_document(report: ComparisonReport) -> ExportDocument:
    entries_by_ref: dict[tuple[str, int], ExportEntry] = {}
    rows: list[ExportComparisonRow] = []
    for row in report.timeline:
        left_ref = None
        right_ref = None
        if row.left is not None:
            left_ref = (report.left.session.session_id, row.left.index)
            entries_by_ref[left_ref] = _entry(report.left.session.session_id, row.left)
        if row.right is not None:
            right_ref = (report.right.session.session_id, row.right.index)
            entries_by_ref[right_ref] = _entry(report.right.session.session_id, row.right)
        rows.append(
            ExportComparisonRow(
                row.kind,
                left_ref[0] if left_ref else None,
                left_ref[1] if left_ref else None,
                right_ref[0] if right_ref else None,
                right_ref[1] if right_ref else None,
            )
        )
    return ExportDocument(
        "session comparison",
        (_session(report.left), _session(report.right)),
        tuple(entries_by_ref[key] for key in sorted(entries_by_ref)),
        tuple(rows),
    )


def activity_document(report: ActivityReport, sessions: tuple[CatalogDetail, ...] = ()) -> ExportDocument:
    exported_sessions = tuple(_session(detail) for detail in sessions)
    activity = tuple(
        ExportActivityBucket(
            bucket.key,
            bucket.start_date,
            bucket.end_date,
            bucket.session_ids,
            bucket.entry_refs,
            bucket.statuses,
            bucket.projects,
            bucket.tags,
        )
        for bucket in report.days
    )
    return ExportDocument("activity view", exported_sessions, activity=activity)


def include_private_notes(document: ExportDocument, store: AnnotationStore) -> ExportDocument:
    allowed_sessions = {session.session_id for session in document.sessions}
    if document.activity:
        allowed_sessions.update(
            session_id for bucket in document.activity for session_id in bucket.session_ids
        )
    allowed_sequences = {
        (entry.session_id, entry.source_event_sequence) for entry in document.entries
    }
    notes = tuple(
        ExportNote(
            note.target.session_id,
            note.target.event_sequence,
            note.text,
            note.updated_at_utc,
        )
        for note in store.list_notes()
        if note.target.session_id in allowed_sessions
        and (
            note.target.event_sequence == -1
            or (note.target.session_id, note.target.event_sequence) in allowed_sequences
        )
    )
    return replace(document, notes=notes)


def render_export(document: ExportDocument, format_name: str) -> bytes:
    if format_name == "json":
        payload = {"format_version": 1, "generated_by": "codex-journal", **asdict(document)}
        encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    elif format_name == "markdown":
        encoded = _render_markdown(document).encode("utf-8")
    else:
        raise ValueError("Export format must be markdown or json.")
    if len(encoded) > MAX_EXPORT_BYTES:
        raise ValueError("Export exceeds the 16 MiB safety limit.")
    return encoded


def render_preview(document: ExportDocument) -> str:
    lines = [
        PRIVACY_WARNING,
        "",
        f"Scope: {document.scope}",
        f"Sessions ({len(document.sessions)}):",
        *(f"- {session.session_id} · {session.project} · {session.status}" for session in document.sessions),
        f"Entries ({len(document.entries)}):",
        *(
            f"- {entry.session_id} / entry {entry.entry_index} / sequence {entry.source_event_sequence}"
            for entry in document.entries
        ),
        f"Comparison rows: {len(document.comparison)}",
        f"Activity buckets: {len(document.activity)}",
        *(
            f"- activity {bucket.key}: sessions={','.join(bucket.session_ids)}; "
            f"entries={','.join(f'{session_id}:{index}' for session_id, index in bucket.entry_refs)}"
            for bucket in document.activity
        ),
        f"Private notes ({len(document.notes)}):",
        *(
            f"- {note.session_id} / sequence {note.source_event_sequence}"
            for note in document.notes
        ),
        "Metadata per session: project, branch, status, start/end UTC, rendered timezone, "
        "entry/redaction/extraction-error counts.",
    ]
    preview = "\n".join(lines)
    if len(preview.encode("utf-8")) > MAX_PREVIEW_BYTES:
        raise ValueError("Export preview exceeds the 4 MiB safety limit.")
    return preview


def write_export_atomic(destination: Path, content: bytes, *, overwrite: bool = False) -> None:
    if not destination.is_absolute():
        raise ValueError("Choose an absolute export destination.")
    parent = destination.parent.resolve(strict=True)
    target = parent / destination.name
    if target.is_symlink():
        raise ValueError("Refusing to replace a symbolic-link export target.")
    if target.exists() and not overwrite:
        raise FileExistsError("Export destination already exists.")
    atomic_write_bytes(target, content)


def _session(detail: CatalogDetail) -> ExportSession:
    session = detail.session
    return ExportSession(
        session.session_id,
        session.project,
        session.branch,
        session.status,
        session.started_at_utc,
        session.ended_at_utc,
        session.rendered_timezone,
        session.entry_count,
        session.redaction_count,
        session.extraction_error_count,
    )


def _entry(session_id: str, entry: CatalogEntry) -> ExportEntry:
    return ExportEntry(
        session_id,
        entry.index,
        entry.source_event_sequence,
        entry.display_time,
        entry.original_timestamp_utc,
        entry.text,
        entry.redacted,
    )


def _render_markdown(document: ExportDocument) -> str:
    lines = ["# Heartbeat Extractor export", "", f"> **Private-information warning:** {PRIVACY_WARNING}", "", f"Scope: {document.scope}", ""]
    for session in document.sessions:
        lines.extend(
            [
                f"## {_markdown_escape(session.project)}",
                "",
                f"- Session: `{session.session_id}`",
                f"- Status: {_markdown_escape(session.status)}",
                f"- Branch: {_markdown_escape(session.branch or 'Not recorded')}",
                f"- Started UTC: {session.started_at_utc}",
                f"- Ended UTC: {session.ended_at_utc or 'Not recorded'}",
                f"- Rendered timezone: {session.rendered_timezone}",
                f"- Entries: {session.entry_count}",
                f"- Redactions: {session.redaction_count}",
                f"- Extraction errors: {session.extraction_error_count}",
                "",
            ]
        )
    if document.entries:
        lines.extend(["## Selected generated entries", ""])
        lines.extend(
            f"- {entry.display_time}  {_markdown_escape(entry.text)}  "
            f"(`{entry.session_id}` / sequence {entry.source_event_sequence})"
            for entry in document.entries
        )
        lines.append("")
    if document.comparison:
        lines.extend(["## Exact comparison rows", ""])
        lines.extend(
            f"- {row.kind}: left={row.left_session_id or '—'}:{row.left_entry_index if row.left_entry_index is not None else '—'}; "
            f"right={row.right_session_id or '—'}:{row.right_entry_index if row.right_entry_index is not None else '—'}"
            for row in document.comparison
        )
        lines.append("")
    if document.activity:
        lines.extend(["## Daily activity", ""])
        lines.extend(
            f"- {bucket.key}: {len(bucket.session_ids)} session(s), {len(bucket.entry_refs)} visible entries; "
            f"statuses={dict(bucket.statuses)}; tags={dict(bucket.tags)}"
            for bucket in document.activity
        )
        lines.append("")
    if document.notes:
        lines.extend(["## Explicitly included private notes", ""])
        lines.extend(
            f"### {note.session_id} / sequence {note.source_event_sequence}\n\n"
            + "\n".join(f"> {_markdown_escape(line)}" for line in note.text.splitlines())
            + "\n"
            for note in document.notes
        )
    return "\n".join(lines).rstrip() + "\n"


def _markdown_escape(text: str) -> str:
    escaped = text.replace("\\", "\\\\")
    for character in ("`", "*", "_", "{", "}", "[", "]", "<", ">", "#", "|"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped

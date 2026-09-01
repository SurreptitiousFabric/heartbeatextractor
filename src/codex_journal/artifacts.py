from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_METADATA_BYTES = 64 * 1024
MAX_METADATA_LINE_BYTES = 16 * 1024
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_PROVENANCE_BYTES = 32 * 1024 * 1024
MAX_INDEX_BYTES = 8 * 1024 * 1024
TIMELINE_RE = re.compile(r"^\d{2}:\d{2}  ")
LINK_RE = re.compile(r"\]\(([^)]+)\)")


@dataclass(frozen=True)
class ArtifactFinding:
    code: str
    message: str


@dataclass(frozen=True)
class JournalMetadata:
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
    timeline_entries: int
    redactions: int
    extraction_errors: int
    generated_by: str
    format_version: int


@dataclass(frozen=True)
class TimelineLine:
    display_time: str
    text: str


@dataclass(frozen=True)
class DecodedJournal:
    metadata: JournalMetadata | None
    timeline: tuple[TimelineLine, ...]
    findings: tuple[ArtifactFinding, ...]


@dataclass(frozen=True)
class ProvenanceEntry:
    source_session_id: str
    source_event_sequence: int
    original_timestamp_utc: str
    original_text_sha256: str
    normalized_text: str
    redacted: bool


@dataclass(frozen=True)
class ProvenanceExtractionError:
    sequence: int
    code: str
    detail: str


@dataclass(frozen=True)
class ProvenanceArtifact:
    session_id: str
    source_fingerprint: str
    entries: tuple[ProvenanceEntry, ...]
    extraction_errors: tuple[ProvenanceExtractionError, ...]
    generated_by: str
    format_version: int


@dataclass(frozen=True)
class DecodedProvenance:
    artifact: ProvenanceArtifact | None
    findings: tuple[ArtifactFinding, ...]


@dataclass(frozen=True)
class DecodedIndex:
    links: tuple[str, ...]
    findings: tuple[ArtifactFinding, ...]


def _finding(code: str, message: str) -> ArtifactFinding:
    return ArtifactFinding(code, message)


def _read_bounded(path: Path, limit: int, label: str) -> tuple[bytes | None, list[ArtifactFinding]]:
    try:
        size = path.stat().st_size
        if size > limit:
            return None, [_finding("oversized", f"{label} exceeds size limit")]
        with path.open("rb") as handle:
            content = handle.read(limit + 1)
    except OSError as exc:
        detail = exc.strerror or type(exc).__name__
        return None, [_finding("unreadable", f"cannot read {label}: {detail}")]
    if len(content) > limit:
        return None, [_finding("oversized", f"{label} exceeds size limit")]
    return content, []


def _required_string(
    raw: dict[str, object], key: str, findings: list[ArtifactFinding]
) -> str | None:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        findings.append(_finding("invalid_metadata", f"missing or invalid metadata: {key}"))
        return None
    return value


def _optional_string(
    raw: dict[str, object], key: str, findings: list[ArtifactFinding]
) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        findings.append(_finding("invalid_metadata", f"invalid metadata: {key}"))
        return None
    return value


def _nonnegative_int(
    raw: dict[str, object], key: str, findings: list[ArtifactFinding]
) -> int | None:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        findings.append(_finding("invalid_metadata", f"missing or invalid metadata: {key}"))
        return None
    return value


def _decode_metadata(
    raw: dict[str, object], findings: list[ArtifactFinding]
) -> JournalMetadata | None:
    session_id = _required_string(raw, "session_id", findings)
    status = _required_string(raw, "status", findings)
    started = _required_string(raw, "started_at_utc", findings)
    rendered_timezone = _required_string(raw, "rendered_timezone", findings)
    fingerprint = _required_string(raw, "source_fingerprint", findings)
    generated_by = _required_string(raw, "generated_by", findings)
    timeline_entries = _nonnegative_int(raw, "timeline_entries", findings)
    redactions = _nonnegative_int(raw, "redactions", findings)
    extraction_errors = _nonnegative_int(raw, "extraction_errors", findings)
    format_version = raw.get("format_version")
    if generated_by != "codex-journal" or format_version != 1:
        findings.append(_finding("unsupported_format", "unsupported generated journal format"))
    if status is not None and status not in {"active", "completed", "incomplete"}:
        findings.append(_finding("invalid_metadata", "invalid metadata: status"))
    parent = _optional_string(raw, "parent_session_id", findings)
    ended = _optional_string(raw, "ended_at_utc", findings)
    working_directory = _optional_string(raw, "working_directory", findings)
    repository = _optional_string(raw, "repository", findings)
    branch = _optional_string(raw, "branch", findings)
    source_kind = raw.get("source_kind", "unknown")
    if not isinstance(source_kind, str):
        findings.append(_finding("invalid_metadata", "invalid metadata: source_kind"))
    if findings:
        return None
    assert all(
        value is not None
        for value in (
            session_id,
            status,
            started,
            rendered_timezone,
            fingerprint,
            generated_by,
            timeline_entries,
            redactions,
            extraction_errors,
        )
    )
    assert isinstance(format_version, int) and isinstance(source_kind, str)
    return JournalMetadata(
        session_id,
        parent,
        status,
        started,
        ended,
        rendered_timezone,
        working_directory,
        repository,
        branch,
        source_kind,
        fingerprint,
        timeline_entries,
        redactions,
        extraction_errors,
        generated_by,
        format_version,
    )


def decode_journal(
    path: Path,
    *,
    max_journal_bytes: int = MAX_JOURNAL_BYTES,
    max_metadata_bytes: int = MAX_METADATA_BYTES,
    max_metadata_line_bytes: int = MAX_METADATA_LINE_BYTES,
) -> DecodedJournal:
    content, findings = _read_bounded(path, max_journal_bytes, "generated journal")
    if content is None:
        return DecodedJournal(None, (), tuple(findings))
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].rstrip(b"\r\n") != b"---":
        findings.append(_finding("missing_metadata", "missing opening metadata delimiter"))
        return DecodedJournal(None, (), tuple(findings))
    raw_metadata: dict[str, object] = {}
    consumed = len(lines[0])
    closing_index: int | None = None
    for index, raw_line in enumerate(lines[1:], 1):
        consumed += len(raw_line)
        if consumed > max_metadata_bytes:
            findings.append(_finding("oversized_metadata", "metadata exceeds size limit"))
            break
        if len(raw_line) > max_metadata_line_bytes:
            findings.append(_finding("oversized_metadata_line", "metadata line exceeds size limit"))
            continue
        stripped = raw_line.rstrip(b"\r\n")
        if stripped == b"---":
            closing_index = index
            break
        try:
            line = stripped.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(_finding("invalid_metadata_utf8", "metadata is not valid UTF-8"))
            continue
        if ": " not in line:
            findings.append(_finding("malformed_metadata", f"malformed metadata line: {line!r}"))
            continue
        key, encoded = line.split(": ", 1)
        try:
            raw_metadata[key] = json.loads(encoded)
        except json.JSONDecodeError:
            findings.append(_finding("malformed_metadata", f"malformed metadata value for {key}"))
    if closing_index is None:
        findings.append(_finding("missing_metadata", "missing closing metadata delimiter"))
        return DecodedJournal(None, (), tuple(findings))

    timeline: list[TimelineLine] = []
    in_timeline = False
    for raw_line in lines[closing_index + 1 :]:
        try:
            line = raw_line.rstrip(b"\r\n").decode("utf-8")
        except UnicodeDecodeError:
            findings.append(_finding("invalid_journal_utf8", "generated journal is not valid UTF-8"))
            continue
        if line == "## Timeline":
            in_timeline = True
            continue
        if in_timeline and line.startswith("## "):
            break
        if in_timeline and TIMELINE_RE.match(line):
            timeline.append(TimelineLine(line[:5], line[7:]))
    metadata_findings: list[ArtifactFinding] = []
    metadata = _decode_metadata(raw_metadata, metadata_findings)
    findings.extend(metadata_findings)
    return DecodedJournal(metadata, tuple(timeline), tuple(findings))


def _provenance_entry(
    value: object, index: int, findings: list[ArtifactFinding]
) -> ProvenanceEntry | None:
    if not isinstance(value, dict):
        findings.append(_finding("invalid_provenance", f"provenance entry {index} has invalid fields"))
        return None
    fields = (
        value.get("source_session_id"),
        value.get("source_event_sequence"),
        value.get("original_timestamp_utc"),
        value.get("original_text_sha256"),
        value.get("normalized_text"),
        value.get("redacted"),
    )
    if not (
        isinstance(fields[0], str)
        and isinstance(fields[1], int)
        and not isinstance(fields[1], bool)
        and isinstance(fields[2], str)
        and isinstance(fields[3], str)
        and isinstance(fields[4], str)
        and isinstance(fields[5], bool)
    ):
        findings.append(_finding("invalid_provenance", f"provenance entry {index} has invalid fields"))
        return None
    return ProvenanceEntry(*fields)


def _provenance_error(
    value: object, index: int, findings: list[ArtifactFinding]
) -> ProvenanceExtractionError | None:
    if not isinstance(value, dict):
        findings.append(_finding("invalid_provenance", f"provenance extraction error {index} has invalid fields"))
        return None
    sequence = value.get("sequence")
    code = value.get("code")
    detail = value.get("detail", "")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or not isinstance(code, str)
        or not isinstance(detail, str)
    ):
        findings.append(_finding("invalid_provenance", f"provenance extraction error {index} has invalid fields"))
        return None
    return ProvenanceExtractionError(sequence, code, detail)


def decode_provenance(
    path: Path, *, max_provenance_bytes: int = MAX_PROVENANCE_BYTES
) -> DecodedProvenance:
    content, findings = _read_bounded(path, max_provenance_bytes, "provenance companion")
    if content is None:
        return DecodedProvenance(None, tuple(findings))
    try:
        raw = json.loads(content.decode("utf-8"))
    except UnicodeDecodeError:
        return DecodedProvenance(None, (_finding("invalid_provenance_utf8", "provenance companion is not valid UTF-8"),))
    except json.JSONDecodeError:
        return DecodedProvenance(None, (_finding("malformed_provenance", "provenance companion is malformed JSON"),))
    if not isinstance(raw, dict):
        return DecodedProvenance(None, (_finding("invalid_provenance", "provenance companion has invalid structure"),))
    generated_by = raw.get("generated_by")
    format_version = raw.get("format_version")
    if generated_by != "codex-journal" or format_version != 1:
        findings.append(_finding("unsupported_provenance", "unsupported provenance format"))
    session_id = raw.get("session_id")
    fingerprint = raw.get("source_fingerprint")
    if not isinstance(session_id, str) or not isinstance(fingerprint, str):
        findings.append(_finding("invalid_provenance", "provenance companion has invalid identity"))
    raw_entries = raw.get("entries")
    entries: list[ProvenanceEntry] = []
    if not isinstance(raw_entries, list):
        findings.append(_finding("invalid_provenance", "provenance entries have invalid structure"))
    else:
        for index, value in enumerate(raw_entries):
            entry = _provenance_entry(value, index, findings)
            if entry is not None:
                entries.append(entry)
    raw_errors = raw.get("extraction_errors", [])
    extraction_errors: list[ProvenanceExtractionError] = []
    if not isinstance(raw_errors, list):
        findings.append(_finding("invalid_provenance", "provenance extraction errors have invalid structure"))
    else:
        for index, value in enumerate(raw_errors):
            error = _provenance_error(value, index, findings)
            if error is not None:
                extraction_errors.append(error)
    if findings:
        return DecodedProvenance(None, tuple(findings))
    assert isinstance(session_id, str) and isinstance(fingerprint, str)
    assert isinstance(generated_by, str) and isinstance(format_version, int)
    return DecodedProvenance(
        ProvenanceArtifact(
            session_id,
            fingerprint,
            tuple(entries),
            tuple(extraction_errors),
            generated_by,
            format_version,
        ),
        (),
    )


def decode_index(path: Path, *, max_index_bytes: int = MAX_INDEX_BYTES) -> DecodedIndex:
    content, findings = _read_bounded(path, max_index_bytes, "generated index")
    if content is None:
        return DecodedIndex((), tuple(findings))
    try:
        body = content.decode("utf-8")
    except UnicodeDecodeError:
        return DecodedIndex((), (_finding("invalid_index_utf8", "generated index is not valid UTF-8"),))
    return DecodedIndex(tuple(LINK_RE.findall(body)), ())

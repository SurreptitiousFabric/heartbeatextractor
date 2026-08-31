from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher

from .viewer_catalog import CatalogDetail, CatalogEntry, CatalogSession
from .viewer_tags import classify_entry


MAX_COMPARISON_ENTRIES = 10_000


@dataclass(frozen=True)
class MetadataComparison:
    label: str
    left: str
    right: str


@dataclass(frozen=True)
class TimelineComparison:
    kind: str
    left: CatalogEntry | None
    right: CatalogEntry | None
    tags: tuple[str, ...]


@dataclass(frozen=True)
class ComparisonReport:
    left: CatalogDetail
    right: CatalogDetail
    metadata: tuple[MetadataComparison, ...]
    timeline: tuple[TimelineComparison, ...]
    tags: tuple[str, ...]


def compare_details(left: CatalogDetail, right: CatalogDetail) -> ComparisonReport:
    if len(left.entries) > MAX_COMPARISON_ENTRIES or len(right.entries) > MAX_COMPARISON_ENTRIES:
        raise ValueError("Comparison exceeds the 10,000-entry per-session safety limit.")
    timeline = _compare_timeline(left.entries, right.entries)
    tags = tuple(sorted({tag for row in timeline for tag in row.tags}, key=str.casefold))
    metadata = (
        _metadata("Session", left.session.session_id, right.session.session_id),
        _metadata("Project", left.session.project, right.session.project),
        _metadata("Branch", left.session.branch, right.session.branch),
        _metadata("Status", left.session.status, right.session.status),
        _metadata("Duration", _duration(left.session), _duration(right.session)),
        _metadata("Timeline entries", left.session.entry_count, right.session.entry_count),
        _metadata("Tags", _detail_tags(left), _detail_tags(right)),
        _metadata(
            "Extraction errors",
            left.session.extraction_error_count,
            right.session.extraction_error_count,
        ),
        _metadata("Redactions", left.session.redaction_count, right.session.redaction_count),
    )
    return ComparisonReport(left, right, metadata, timeline, tags)


def filter_timeline(
    timeline: tuple[TimelineComparison, ...], tag: str | None
) -> tuple[TimelineComparison, ...]:
    return timeline if not tag else tuple(row for row in timeline if tag in row.tags)


def _metadata(label: str, left: object, right: object) -> MetadataComparison:
    return MetadataComparison(label, _display(left), _display(right))


def _display(value: object) -> str:
    if value is None or value == "":
        return "Not recorded"
    if isinstance(value, tuple):
        return ", ".join(value) if value else "None"
    return str(value)


def _duration(session: CatalogSession) -> str:
    if not session.ended_at_utc:
        return "Not completed"
    try:
        started = datetime.fromisoformat(session.started_at_utc.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(session.ended_at_utc.replace("Z", "+00:00"))
        seconds = max(0, int((ended - started).total_seconds()))
    except ValueError:
        return "Invalid stored timestamp"
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _detail_tags(detail: CatalogDetail) -> tuple[str, ...]:
    return tuple(
        sorted(
            {tag for entry in detail.entries for tag in classify_entry(entry.text)},
            key=str.casefold,
        )
    )


def _compare_timeline(
    left: tuple[CatalogEntry, ...], right: tuple[CatalogEntry, ...]
) -> tuple[TimelineComparison, ...]:
    matcher = SequenceMatcher(
        None,
        [entry.text for entry in left],
        [entry.text for entry in right],
        autojunk=False,
    )
    rows: list[TimelineComparison] = []
    for operation, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if operation == "equal":
            for left_entry, right_entry in zip(
                left[left_start:left_end], right[right_start:right_end], strict=True
            ):
                rows.append(
                    TimelineComparison(
                        "unchanged",
                        left_entry,
                        right_entry,
                        classify_entry(left_entry.text),
                    )
                )
            continue
        if operation in {"delete", "replace"}:
            for entry in left[left_start:left_end]:
                rows.append(TimelineComparison("left only", entry, None, classify_entry(entry.text)))
        if operation in {"insert", "replace"}:
            for entry in right[right_start:right_end]:
                rows.append(TimelineComparison("right only", None, entry, classify_entry(entry.text)))
    return tuple(rows)

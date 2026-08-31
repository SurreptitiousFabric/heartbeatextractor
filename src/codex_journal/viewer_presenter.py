from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .viewer_catalog import CatalogDetail, CatalogEntry, CatalogSession


TAG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("failure", re.compile(r"\b(?:fail(?:ed|ure)?|error|defect|broken)\b", re.IGNORECASE)),
    ("test", re.compile(r"\b(?:test(?:s|ed|ing)?|validation|contract(?:s)?|pass(?:ed|ing)?)\b", re.IGNORECASE)),
    ("security", re.compile(r"\b(?:security|credential|secret|token|permission|fail-open|vulnerab\w*)\b", re.IGNORECASE)),
    ("blocker", re.compile(r"\b(?:blocker(?:s)?|blocked|objection(?:s)?)\b", re.IGNORECASE)),
    ("correction", re.compile(r"\b(?:correct(?:ed|ion)?|fix(?:ed)?|withdrawn|retracted)\b", re.IGNORECASE)),
    ("commit", re.compile(r"\b(?:commit(?:ted)?|push(?:ed)?|pull request|issue update|pr update)\b", re.IGNORECASE)),
)


@dataclass(frozen=True)
class PresentedEntry:
    entry: CatalogEntry
    local_date: str
    date_label: str
    display_time: str
    tags: tuple[str, ...]
    indicators: tuple[str, ...]


def classify_entry(text: str) -> tuple[str, ...]:
    return tuple(label for label, pattern in TAG_PATTERNS if pattern.search(text))


def present_entry(entry: CatalogEntry, session: CatalogSession) -> PresentedEntry:
    local_date = "Unknown date"
    date_label = "Unknown date"
    display_time = entry.display_time
    try:
        timezone = ZoneInfo(session.rendered_timezone)
        timestamp = datetime.fromisoformat(entry.original_timestamp_utc.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("timestamp has no timezone")
        local = timestamp.astimezone(timezone)
        local_date = local.date().isoformat()
        date_label = local.strftime("%A, %d %B %Y")
        display_time = local.strftime("%H:%M")
    except (ValueError, ZoneInfoNotFoundError):
        pass
    indicators = ("redacted",) if entry.redacted else ()
    return PresentedEntry(
        entry=entry,
        local_date=local_date,
        date_label=date_label,
        display_time=display_time,
        tags=classify_entry(entry.text),
        indicators=indicators,
    )


def present_timeline(detail: CatalogDetail) -> tuple[PresentedEntry, ...]:
    return tuple(present_entry(entry, detail.session) for entry in detail.entries)

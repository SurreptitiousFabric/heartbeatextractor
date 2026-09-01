from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .viewer_catalog import CatalogDetail, CatalogEntry, CatalogSession
from .viewer_tags import classify_entry


MAX_SESSION_SUMMARY_CHARS = 120
_INLINE_CODE = re.compile(r"`([^`\n]+)`")


@dataclass(frozen=True)
class PresentedEntry:
    entry: CatalogEntry
    local_date: str
    date_label: str
    display_time: str
    tags: tuple[str, ...]
    indicators: tuple[str, ...]


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


def concise_session_summary(text: str, limit: int = MAX_SESSION_SUMMARY_CHARS) -> str:
    """Bound one already-sanitized timeline entry for session-list display."""

    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    shortened = cleaned[: max(1, limit - 1)].rstrip()
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return f"{shortened}…"


def safe_inline_markup(text: str) -> str:
    """Render balanced inline-code spans after escaping every source character."""

    if text.count("`") % 2 or "\n" in text:
        return html.escape(text)
    parts: list[str] = []
    cursor = 0
    for match in _INLINE_CODE.finditer(text):
        parts.append(html.escape(text[cursor : match.start()]))
        parts.append(f'<span font_family="monospace">{html.escape(match.group(1))}</span>')
        cursor = match.end()
    parts.append(html.escape(text[cursor:]))
    return "".join(parts)

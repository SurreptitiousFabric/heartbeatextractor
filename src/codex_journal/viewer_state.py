from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


MAX_VIEWER_STATE_BYTES = 256 * 1024
@dataclass(frozen=True)
class ViewerState:
    selected_session_id: str | None = None
    filters: dict[str, str | bool | None] = field(default_factory=dict)
    window_width: int = 1180
    window_height: int = 760
    content_visible: bool = False
    timeline_entry_index: int = 0
    last_sync_at: str | None = None
    last_sync_summary: str | None = None


def _bounded_dimension(value: object, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 480 <= value <= 8192:
        return value
    return default


def _strict_bool(value: object) -> bool:
    return value if isinstance(value, bool) else False


def _clean_filters(value: object) -> dict[str, str | bool | None]:
    allowed = {
        "project",
        "date_from",
        "date_to",
        "branch",
        "status",
        "source_kind",
        "redacted_only",
        "extraction_errors_only",
        "bookmarked_only",
        "tag",
    }
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, str | bool | None] = {}
    for key, item in value.items():
        if key in allowed and (
            item is None
            or isinstance(item, bool)
            or (isinstance(item, str) and len(item.encode("utf-8")) <= 1024)
        ):
            cleaned[key] = item
    return cleaned


class ViewerStateStore:
    """Ignored local UI state; never stores journal text or raw source data."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> ViewerState:
        if not self.path.is_file():
            return ViewerState()
        try:
            if self.path.is_symlink():
                return ViewerState()
            if self.path.stat().st_size > MAX_VIEWER_STATE_BYTES:
                return ViewerState()
            with self.path.open("rb") as handle:
                raw = handle.read(MAX_VIEWER_STATE_BYTES + 1)
            if len(raw) > MAX_VIEWER_STATE_BYTES:
                return ViewerState()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return ViewerState()
        if not isinstance(payload, dict) or payload.get("format_version") != 1:
            return ViewerState()
        session_id = payload.get("selected_session_id")
        if not isinstance(session_id, str) or len(session_id) > 256:
            session_id = None
        entry_index = payload.get("timeline_entry_index")
        if not isinstance(entry_index, int) or isinstance(entry_index, bool) or entry_index < 0:
            entry_index = 0
        return ViewerState(
            selected_session_id=session_id,
            filters=_clean_filters(payload.get("filters")),
            window_width=_bounded_dimension(payload.get("window_width"), 1180),
            window_height=_bounded_dimension(payload.get("window_height"), 760),
            content_visible=_strict_bool(payload.get("content_visible", False)),
            timeline_entry_index=entry_index,
            last_sync_at=(
                payload.get("last_sync_at")
                if isinstance(payload.get("last_sync_at"), str)
                and len(payload["last_sync_at"]) <= 128
                else None
            ),
            last_sync_summary=(
                payload.get("last_sync_summary")
                if isinstance(payload.get("last_sync_summary"), str)
                and len(payload["last_sync_summary"]) <= 2048
                else None
            ),
        )

    def save(self, state: ViewerState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("refusing to replace symbolic-link viewer state")
        payload: dict[str, Any] = {"format_version": 1, **asdict(state)}
        encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
        if len(encoded) > MAX_VIEWER_STATE_BYTES:
            raise ValueError("viewer state exceeds size limit")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.path.parent, prefix=f".{self.path.name}.", delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

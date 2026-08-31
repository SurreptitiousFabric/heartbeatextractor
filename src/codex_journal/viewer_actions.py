from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .viewer_catalog import CatalogEntry


class ProjectPathError(ValueError):
    """The generated working-directory metadata is not safe to open."""


@dataclass(frozen=True)
class CopyPayload:
    text: str
    entry_count: int


def resolve_project_directory(recorded: str | None, *, home: Path) -> Path:
    if not recorded or "\x00" in recorded:
        raise ProjectPathError("No local project directory was recorded.")
    if recorded == "~":
        candidate = home
    elif recorded.startswith("~/"):
        candidate = home / recorded[2:]
    else:
        candidate = Path(recorded)
    if not candidate.is_absolute():
        raise ProjectPathError("The recorded project directory is not absolute.")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(home.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ProjectPathError(
            "The recorded project directory is missing or outside the user home."
        ) from exc
    if not resolved.is_dir():
        raise ProjectPathError("The recorded project path is not a directory.")
    return resolved


def project_directory_uri(recorded: str | None, *, home: Path) -> str:
    return resolve_project_directory(recorded, home=home).as_uri()


def copy_one_entry(entry: CatalogEntry) -> CopyPayload:
    return _copy_entries((entry,))


def copy_selected_range(
    entries: tuple[CatalogEntry, ...], selected_indexes: set[int]
) -> CopyPayload:
    if not selected_indexes:
        raise ValueError("No timeline entries are selected.")
    by_index = {entry.index: entry for entry in entries}
    start = min(selected_indexes)
    end = max(selected_indexes)
    selected = tuple(by_index[index] for index in range(start, end + 1) if index in by_index)
    if not selected:
        raise ValueError("The selected timeline range is unavailable.")
    return _copy_entries(selected)


def _copy_entries(entries: tuple[CatalogEntry, ...]) -> CopyPayload:
    count = len(entries)
    heading = f"{count} sanitized journal entr{'y' if count == 1 else 'ies'}"
    lines = [heading, *(f"{entry.display_time}  {entry.text}" for entry in entries)]
    return CopyPayload("\n".join(lines) + "\n", count)

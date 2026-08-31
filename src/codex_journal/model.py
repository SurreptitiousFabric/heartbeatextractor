from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceSession:
    path: Path
    source_key: str
    session_id: str
    started_at_utc: str
    working_directory: str | None
    repository: str | None
    branch: str | None
    parent_session_id: str | None
    source_kind: str


@dataclass
class Candidate:
    sequence: int
    timestamp_utc: str
    text: str
    original_text_sha256: str
    redacted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Candidate":
        return cls(**value)


@dataclass
class JournalEntry:
    sequence: int
    timestamp_utc: str
    text: str
    original_text_sha256: str
    redacted: bool


@dataclass
class ExtractionError:
    sequence: int
    code: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExtractionError":
        return cls(**value)


@dataclass
class SessionCache:
    source_key: str
    session_id: str
    started_at_utc: str
    working_directory: str | None
    repository: str | None
    branch: str | None
    parent_session_id: str | None
    source_kind: str
    candidates: list[Candidate] = field(default_factory=list)
    errors: list[ExtractionError] = field(default_factory=list)
    lifecycle_type: str | None = None
    lifecycle_timestamp_utc: str | None = None
    processed_offset: int = 0
    processed_prefix_sha256: str = ""
    next_sequence: int = 0
    source_size: int = 0
    source_fingerprint: str = ""
    redaction_count: int = 0
    journal_relpath: str | None = None
    rendered_timezone: str | None = None
    entry_count: int = 0

    @property
    def status(self) -> str:
        return {
            "task_started": "active",
            "task_complete": "completed",
            "turn_aborted": "incomplete",
        }.get(self.lifecycle_type, "incomplete")

    @property
    def ended_at_utc(self) -> str | None:
        if self.lifecycle_type in {"task_complete", "turn_aborted"}:
            return self.lifecycle_timestamp_utc
        return None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["candidates"] = [item.to_dict() for item in self.candidates]
        value["errors"] = [item.to_dict() for item in self.errors]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SessionCache":
        copied = dict(value)
        copied["candidates"] = [Candidate.from_dict(v) for v in copied.get("candidates", [])]
        copied["errors"] = [ExtractionError.from_dict(v) for v in copied.get("errors", [])]
        return cls(**copied)


@dataclass
class SyncResult:
    discovered: int = 0
    processed: int = 0
    unchanged: int = 0
    rebuilt: int = 0
    appended: int = 0
    no_heartbeats: int = 0
    active_or_incomplete: int = 0
    sessions_with_errors: int = 0
    written_paths: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class VerifyResult:
    journals: int = 0
    entries: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

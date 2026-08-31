from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


MAX_NOTE_BYTES = 64 * 1024
PREFERENCE_KEYS = {"theme", "sync_on_launch", "periodic_sync"}


@dataclass(frozen=True)
class AnnotationTarget:
    session_id: str
    event_sequence: int = -1

    @property
    def scope(self) -> str:
        return "session" if self.event_sequence == -1 else "entry"


@dataclass(frozen=True)
class Bookmark:
    target: AnnotationTarget
    created_at_utc: str


@dataclass(frozen=True)
class PrivateNote:
    target: AnnotationTarget
    text: str
    updated_at_utc: str


class AnnotationStore:
    """Private ignored state, strictly separate from generated artifacts."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        if path.is_symlink():
            raise ValueError("refusing symbolic-link annotation state")
        try:
            self.connection = sqlite3.connect(path)
            self.connection.execute("PRAGMA foreign_keys = ON")
            with self.connection:
                self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS bookmarks (
                    session_id TEXT NOT NULL,
                    event_sequence INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    PRIMARY KEY(session_id, event_sequence)
                );
                CREATE TABLE IF NOT EXISTS private_notes (
                    session_id TEXT NOT NULL,
                    event_sequence INTEGER NOT NULL,
                    note_text TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY(session_id, event_sequence)
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    preference_key TEXT PRIMARY KEY,
                    preference_value TEXT NOT NULL
                );
                """
                )
        except sqlite3.Error as exc:
            if hasattr(self, "connection"):
                self.connection.close()
            raise ValueError("private annotation database is malformed") from exc

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> AnnotationStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def is_bookmarked(self, target: AnnotationTarget) -> bool:
        _validate_target(target)
        row = self.connection.execute(
            "SELECT 1 FROM bookmarks WHERE session_id = ? AND event_sequence = ?",
            (target.session_id, target.event_sequence),
        ).fetchone()
        return row is not None

    def set_bookmarked(self, target: AnnotationTarget, bookmarked: bool) -> bool:
        _validate_target(target)
        with self.connection:
            if bookmarked:
                self.connection.execute(
                    "INSERT OR IGNORE INTO bookmarks VALUES (?, ?, ?)",
                    (target.session_id, target.event_sequence, _now()),
                )
            else:
                self.connection.execute(
                    "DELETE FROM bookmarks WHERE session_id = ? AND event_sequence = ?",
                    (target.session_id, target.event_sequence),
                )
        return self.is_bookmarked(target)

    def toggle_bookmark(self, target: AnnotationTarget) -> bool:
        return self.set_bookmarked(target, not self.is_bookmarked(target))

    def list_bookmarks(self) -> tuple[Bookmark, ...]:
        rows = self.connection.execute(
            "SELECT session_id, event_sequence, created_at_utc FROM bookmarks "
            "ORDER BY created_at_utc DESC, session_id, event_sequence"
        ).fetchall()
        return tuple(
            Bookmark(AnnotationTarget(row[0], int(row[1])), row[2]) for row in rows
        )

    def bookmarked_session_ids(self) -> frozenset[str]:
        return frozenset(row[0] for row in self.connection.execute("SELECT DISTINCT session_id FROM bookmarks"))

    def get_note(self, target: AnnotationTarget) -> PrivateNote | None:
        _validate_target(target)
        row = self.connection.execute(
            "SELECT note_text, updated_at_utc FROM private_notes "
            "WHERE session_id = ? AND event_sequence = ?",
            (target.session_id, target.event_sequence),
        ).fetchone()
        return PrivateNote(target, row[0], row[1]) if row else None

    def save_note(self, target: AnnotationTarget, text: str) -> PrivateNote:
        _validate_target(target)
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            raise ValueError("A private note cannot be empty.")
        if "\x00" in normalized or len(normalized.encode("utf-8")) > MAX_NOTE_BYTES:
            raise ValueError("The private note is invalid or exceeds 64 KiB.")
        updated = _now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO private_notes VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id, event_sequence) DO UPDATE SET "
                "note_text = excluded.note_text, updated_at_utc = excluded.updated_at_utc",
                (target.session_id, target.event_sequence, normalized, updated),
            )
        return PrivateNote(target, normalized, updated)

    def delete_note(self, target: AnnotationTarget) -> bool:
        _validate_target(target)
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM private_notes WHERE session_id = ? AND event_sequence = ?",
                (target.session_id, target.event_sequence),
            )
        return cursor.rowcount > 0

    def list_notes(self) -> tuple[PrivateNote, ...]:
        rows = self.connection.execute(
            "SELECT session_id, event_sequence, note_text, updated_at_utc FROM private_notes "
            "ORDER BY updated_at_utc DESC, session_id, event_sequence"
        ).fetchall()
        return tuple(
            PrivateNote(AnnotationTarget(row[0], int(row[1])), row[2], row[3]) for row in rows
        )

    def get_preference(self, key: str, default: str) -> str:
        if key not in PREFERENCE_KEYS:
            raise ValueError("Unknown private preference.")
        row = self.connection.execute(
            "SELECT preference_value FROM preferences WHERE preference_key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_preference(self, key: str, value: str) -> None:
        if key not in PREFERENCE_KEYS or len(value.encode("utf-8")) > 128:
            raise ValueError("Invalid private preference.")
        with self.connection:
            self.connection.execute(
                "INSERT INTO preferences VALUES (?, ?) ON CONFLICT(preference_key) "
                "DO UPDATE SET preference_value = excluded.preference_value",
                (key, value),
            )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _validate_target(target: AnnotationTarget) -> None:
    if (
        not target.session_id
        or len(target.session_id.encode("utf-8")) > 256
        or target.event_sequence < -1
    ):
        raise ValueError("invalid private annotation target")

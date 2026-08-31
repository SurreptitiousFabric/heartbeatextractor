from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .model import SessionCache


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                source_key TEXT NOT NULL,
                data_json TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def get(self, session_id: str) -> SessionCache | None:
        row = self.connection.execute(
            "SELECT data_json FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return SessionCache.from_dict(json.loads(row[0]))

    def save(self, cache: SessionCache) -> None:
        data = json.dumps(cache.to_dict(), sort_keys=True, separators=(",", ":"))
        self.connection.execute(
            """
            INSERT INTO sessions(session_id, source_key, data_json)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                source_key = excluded.source_key,
                data_json = excluded.data_json
            """,
            (cache.session_id, cache.source_key, data),
        )
        self.connection.commit()

    def delete(self, session_id: str) -> None:
        self.connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        self.connection.commit()

    def all(self) -> list[SessionCache]:
        rows = self.connection.execute("SELECT data_json FROM sessions").fetchall()
        return [SessionCache.from_dict(json.loads(row[0])) for row in rows]


def read_all_readonly(path: Path) -> list[SessionCache]:
    """Read existing state without creating or modifying the database."""

    if not path.is_file():
        return []
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT data_json FROM sessions").fetchall()
    finally:
        connection.close()
    return [SessionCache.from_dict(json.loads(row[0])) for row in rows]

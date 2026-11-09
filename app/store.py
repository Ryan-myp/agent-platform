from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class ProjectStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        if str(database_path) != ":memory:":
            database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )"""
            )

    def save(self, project: dict[str, Any]) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO projects (id, created_at, updated_at, payload)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     updated_at = excluded.updated_at,
                     payload = excluded.payload""",
                (
                    project["id"],
                    project["created_at"],
                    project["updated_at"],
                    json.dumps(project, ensure_ascii=False),
                ),
            )
        return project

    def get(self, project_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM projects ORDER BY updated_at DESC"
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def append_chat_message(self, project_id: str, message: dict[str, Any]) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO chat_messages (id, project_id, created_at, payload) VALUES (?, ?, ?, ?)",
                (
                    message["id"],
                    project_id,
                    message["created_at"],
                    json.dumps(message, ensure_ascii=False),
                ),
            )
        return message

    def chat_history(self, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM chat_messages WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [json.loads(row["payload"]) for row in reversed(rows)]

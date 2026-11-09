from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class VideoJobConflict(RuntimeError):
    pass


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
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_messages_project_created_id "
                "ON chat_messages(project_id, created_at DESC, id DESC)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS chat_attachments (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_attachments_project_created "
                "ON chat_attachments(project_id, created_at)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS video_jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    shot_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_poll_at TEXT,
                    payload TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_video_jobs_status_poll "
                "ON video_jobs(status, next_poll_at, created_at)"
            )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_video_jobs_active_shot
                   ON video_jobs(project_id, shot_id)
                   WHERE status IN ('queued', 'submitting', 'polling')"""
            )

    @staticmethod
    def _save_project_record(connection: sqlite3.Connection, project: dict[str, Any]) -> None:
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

    def save(self, project: dict[str, Any]) -> dict[str, Any]:
        with self._connection() as connection:
            self._save_project_record(connection, project)
        return project

    def enqueue_video_job(self, project: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """SELECT id FROM video_jobs
                   WHERE project_id = ? AND shot_id = ?
                     AND status IN ('queued', 'submitting', 'polling')
                   LIMIT 1""",
                (job["project_id"], job["shot_id"]),
            ).fetchone()
            if active:
                raise VideoJobConflict("这个镜头已有正在处理的视频任务。")
            self._save_project_record(connection, project)
            try:
                connection.execute(
                    """INSERT INTO video_jobs
                       (id, project_id, shot_id, status, created_at, updated_at, next_poll_at, payload)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        job["id"], job["project_id"], job["shot_id"], job["status"],
                        job["created_at"], job["updated_at"], job.get("next_poll_at"),
                        json.dumps(job, ensure_ascii=False),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise VideoJobConflict("这个镜头已有正在处理的视频任务。") from exc
        return job

    def get_video_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM video_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def latest_video_job(self, project_id: str, shot_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT payload FROM video_jobs
                   WHERE project_id = ? AND shot_id = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (project_id, shot_id),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_video_jobs(self, statuses: set[str] | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if statuses:
                placeholders = ", ".join("?" for _ in statuses)
                rows = connection.execute(
                    f"SELECT payload FROM video_jobs WHERE status IN ({placeholders}) ORDER BY created_at",
                    tuple(sorted(statuses)),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload FROM video_jobs ORDER BY created_at"
                ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def claim_next_video_job(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM video_jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            job = json.loads(row["payload"])
            job.update({
                "status": "submitting",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "next_poll_at": None,
            })
            connection.execute(
                """UPDATE video_jobs SET status = ?, updated_at = ?, next_poll_at = NULL, payload = ?
                   WHERE id = ? AND status = 'queued'""",
                (job["status"], job["updated_at"], json.dumps(job, ensure_ascii=False), job["id"]),
            )
        return job

    def next_polling_video_job(self, now: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT payload FROM video_jobs
                   WHERE status = 'polling' AND (next_poll_at IS NULL OR next_poll_at <= ?)
                   ORDER BY COALESCE(next_poll_at, created_at), created_at LIMIT 1""",
                (now,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def update_video_job(
        self,
        job_id: str,
        updates: dict[str, Any],
        *,
        project: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM video_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"找不到视频任务 {job_id}。")
            job = json.loads(row["payload"])
            job.update(updates)
            job["updated_at"] = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """UPDATE video_jobs
                   SET status = ?, updated_at = ?, next_poll_at = ?, payload = ?
                   WHERE id = ?""",
                (
                    job["status"], job["updated_at"], job.get("next_poll_at"),
                    json.dumps(job, ensure_ascii=False), job_id,
                ),
            )
            if project is not None:
                self._save_project_record(connection, project)
        return job

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

    def save_chat_attachment(self, attachment: dict[str, Any]) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO chat_attachments (id, project_id, created_at, payload) VALUES (?, ?, ?, ?)",
                (
                    attachment["id"],
                    attachment["project_id"],
                    attachment["created_at"],
                    json.dumps(attachment, ensure_ascii=False),
                ),
            )
        return attachment

    def get_chat_attachment(self, attachment_id: str, project_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM chat_attachments WHERE id = ? AND project_id = ?",
                (attachment_id, project_id),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def delete_chat_attachment(self, attachment_id: str, project_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM chat_attachments WHERE id = ? AND project_id = ?",
                (attachment_id, project_id),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "DELETE FROM chat_attachments WHERE id = ? AND project_id = ?",
                (attachment_id, project_id),
            )
        return json.loads(row["payload"])

    def chat_history(self, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT payload FROM chat_messages
                   WHERE project_id = ?
                   ORDER BY created_at DESC, id DESC LIMIT ?""",
                (project_id, limit),
            ).fetchall()
        return [json.loads(row["payload"]) for row in reversed(rows)]

    def chat_history_page(
        self,
        project_id: str,
        *,
        limit: int = 10,
        before_id: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("每页聊天记录数量必须在 1–100 之间。")
        with self._connection() as connection:
            if before_id:
                cursor = connection.execute(
                    "SELECT created_at, id FROM chat_messages WHERE project_id = ? AND id = ?",
                    (project_id, before_id),
                ).fetchone()
                if cursor is None:
                    raise ValueError("聊天记录游标无效，请重新打开对话。")
                rows = connection.execute(
                    """SELECT id, payload FROM chat_messages
                       WHERE project_id = ?
                         AND (created_at < ? OR (created_at = ? AND id < ?))
                       ORDER BY created_at DESC, id DESC LIMIT ?""",
                    (project_id, cursor["created_at"], cursor["created_at"], cursor["id"], limit + 1),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT id, payload FROM chat_messages
                       WHERE project_id = ?
                       ORDER BY created_at DESC, id DESC LIMIT ?""",
                    (project_id, limit + 1),
                ).fetchall()
        has_more = len(rows) > limit
        messages = [json.loads(row["payload"]) for row in reversed(rows[:limit])]
        return {
            "messages": messages,
            "has_more": has_more,
            "next_cursor": messages[0]["id"] if has_more and messages else None,
        }

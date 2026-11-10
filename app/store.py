from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class VideoJobConflict(RuntimeError):
    pass


class SeriesEpisodeConflict(RuntimeError):
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
                """CREATE TABLE IF NOT EXISTS series (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS series_assets (
                    id TEXT PRIMARY KEY,
                    series_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_series_assets_series_created "
                "ON series_assets(series_id, created_at)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS series_episodes (
                    series_id TEXT NOT NULL,
                    episode_number INTEGER NOT NULL,
                    project_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(series_id, episode_number)
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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS usage_events (
                    id TEXT PRIMARY KEY,
                    project_id TEXT,
                    created_at TEXT NOT NULL,
                    modality TEXT NOT NULL,
                    model_id TEXT,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_events_created ON usage_events(created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_events_project_created ON usage_events(project_id, created_at DESC)"
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

    def import_project_bundle(
        self,
        project: dict[str, Any],
        *,
        messages: list[dict[str, Any]],
        attachments: list[dict[str, Any]],
        series: dict[str, Any] | None = None,
        series_assets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Insert a fully remapped project bundle without overwriting existing records."""
        series_assets = series_assets or []
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if series:
                connection.execute(
                    "INSERT INTO series (id, created_at, updated_at, payload) VALUES (?, ?, ?, ?)",
                    (series["id"], series["created_at"], series["updated_at"], json.dumps(series, ensure_ascii=False)),
                )
                for asset in series_assets:
                    connection.execute(
                        "INSERT INTO series_assets (id, series_id, created_at, updated_at, payload) VALUES (?, ?, ?, ?, ?)",
                        (asset["id"], asset["series_id"], asset["created_at"], asset["updated_at"], json.dumps(asset, ensure_ascii=False)),
                    )
            connection.execute(
                "INSERT INTO projects (id, created_at, updated_at, payload) VALUES (?, ?, ?, ?)",
                (project["id"], project["created_at"], project["updated_at"], json.dumps(project, ensure_ascii=False)),
            )
            if series and project.get("series_id"):
                episode_number = project.get("episode_number")
                if episode_number is None:
                    row = connection.execute(
                        "SELECT COALESCE(MAX(episode_number), 0) + 1 AS next_number FROM series_episodes WHERE series_id = ?",
                        (project["series_id"],),
                    ).fetchone()
                    episode_number = int(row["next_number"])
                    project["episode_number"] = episode_number
                    self._save_project_record(connection, project)
                connection.execute(
                    "INSERT INTO series_episodes (series_id, episode_number, project_id, created_at) VALUES (?, ?, ?, ?)",
                    (project["series_id"], episode_number, project["id"], project["created_at"]),
                )
            for message in messages:
                connection.execute(
                    "INSERT INTO chat_messages (id, project_id, created_at, payload) VALUES (?, ?, ?, ?)",
                    (message["id"], project["id"], message["created_at"], json.dumps(message, ensure_ascii=False)),
                )
            for attachment in attachments:
                connection.execute(
                    "INSERT INTO chat_attachments (id, project_id, created_at, payload) VALUES (?, ?, ?, ?)",
                    (attachment["id"], project["id"], attachment["created_at"], json.dumps(attachment, ensure_ascii=False)),
                )
        return project

    def import_series_bundle(
        self,
        series: dict[str, Any],
        assets: list[dict[str, Any]],
        episodes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Insert a complete remapped season atomically without overwriting existing rows."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO series (id, created_at, updated_at, payload) VALUES (?, ?, ?, ?)",
                (series["id"], series["created_at"], series["updated_at"], json.dumps(series, ensure_ascii=False)),
            )
            for asset in assets:
                connection.execute(
                    "INSERT INTO series_assets (id, series_id, created_at, updated_at, payload) VALUES (?, ?, ?, ?, ?)",
                    (asset["id"], asset["series_id"], asset["created_at"], asset["updated_at"], json.dumps(asset, ensure_ascii=False)),
                )
            for record in episodes:
                project = record["project"]
                connection.execute(
                    "INSERT INTO projects (id, created_at, updated_at, payload) VALUES (?, ?, ?, ?)",
                    (project["id"], project["created_at"], project["updated_at"], json.dumps(project, ensure_ascii=False)),
                )
                connection.execute(
                    "INSERT INTO series_episodes (series_id, episode_number, project_id, created_at) VALUES (?, ?, ?, ?)",
                    (project["series_id"], project["episode_number"], project["id"], project["created_at"]),
                )
                for message in record["messages"]:
                    connection.execute(
                        "INSERT INTO chat_messages (id, project_id, created_at, payload) VALUES (?, ?, ?, ?)",
                        (message["id"], project["id"], message["created_at"], json.dumps(message, ensure_ascii=False)),
                    )
                for attachment in record["attachments"]:
                    connection.execute(
                        "INSERT INTO chat_attachments (id, project_id, created_at, payload) VALUES (?, ?, ?, ?)",
                        (attachment["id"], project["id"], attachment["created_at"], json.dumps(attachment, ensure_ascii=False)),
                    )
        return {"series": series, "assets": assets, "episodes": episodes}

    def save_series(self, series: dict[str, Any]) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO series (id, created_at, updated_at, payload)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     updated_at = excluded.updated_at, payload = excluded.payload""",
                (series["id"], series["created_at"], series["updated_at"], json.dumps(series, ensure_ascii=False)),
            )
        return series

    def get_series(self, series_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT payload FROM series WHERE id = ?", (series_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_series(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT payload FROM series ORDER BY updated_at DESC, id").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_series_asset(self, asset: dict[str, Any]) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO series_assets (id, series_id, created_at, updated_at, payload)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     updated_at = excluded.updated_at, payload = excluded.payload""",
                (
                    asset["id"], asset["series_id"], asset["created_at"], asset["updated_at"],
                    json.dumps(asset, ensure_ascii=False),
                ),
            )
        return asset

    def get_series_asset(self, series_id: str, asset_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM series_assets WHERE series_id = ? AND id = ?",
                (series_id, asset_id),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_series_assets(self, series_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM series_assets WHERE series_id = ? ORDER BY created_at, id",
                (series_id,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_series_episode(
        self, project: dict[str, Any], series_id: str, episode_number: int | None = None
    ) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if episode_number is None:
                row = connection.execute(
                    "SELECT COALESCE(MAX(episode_number), 0) + 1 AS next_number "
                    "FROM series_episodes WHERE series_id = ?",
                    (series_id,),
                ).fetchone()
                episode_number = int(row["next_number"])
            project["series_id"] = series_id
            project["episode_number"] = episode_number
            try:
                connection.execute(
                    "INSERT INTO series_episodes (series_id, episode_number, project_id, created_at) VALUES (?, ?, ?, ?)",
                    (series_id, episode_number, project["id"], project["created_at"]),
                )
            except sqlite3.IntegrityError as error:
                raise SeriesEpisodeConflict("该系列已存在这个集数，请选择其他集数。") from error
            self._save_project_record(connection, project)
        return project

    def list_series_episodes(self, series_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT projects.payload FROM series_episodes
                   JOIN projects ON projects.id = series_episodes.project_id
                   WHERE series_episodes.series_id = ?
                   ORDER BY series_episodes.episode_number, series_episodes.project_id""",
                (series_id,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

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

    def cancel_queued_video_job(
        self, job_id: str, updates: dict[str, Any], project: dict[str, Any]
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM video_jobs WHERE id = ? AND status = 'queued'", (job_id,)
            ).fetchone()
            if not row:
                return None
            job = json.loads(row["payload"])
            job.update(updates)
            job["status"] = "cancelled"
            job["updated_at"] = datetime.now(timezone.utc).isoformat()
            job["next_poll_at"] = None
            connection.execute(
                "UPDATE video_jobs SET status = ?, updated_at = ?, next_poll_at = NULL, payload = ? WHERE id = ? AND status = 'queued'",
                (job["status"], job["updated_at"], json.dumps(job, ensure_ascii=False), job_id),
            )
            self._save_project_record(connection, project)
        return job

    def resume_video_job(
        self, job_id: str, updates: dict[str, Any], project: dict[str, Any]
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM video_jobs WHERE id = ? AND status IN ('timed_out', 'stalled')",
                (job_id,),
            ).fetchone()
            if not row:
                return None
            job = json.loads(row["payload"])
            active = connection.execute(
                """SELECT id FROM video_jobs WHERE project_id = ? AND shot_id = ?
                   AND id != ? AND status IN ('queued', 'submitting', 'polling') LIMIT 1""",
                (job["project_id"], job["shot_id"], job_id),
            ).fetchone()
            if active:
                raise VideoJobConflict("这个镜头已有正在处理的视频任务，无法恢复查询。")
            job.update(updates)
            job["status"] = "polling"
            job["updated_at"] = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "UPDATE video_jobs SET status = ?, updated_at = ?, next_poll_at = ?, payload = ? WHERE id = ?",
                (job["status"], job["updated_at"], job.get("next_poll_at"), json.dumps(job, ensure_ascii=False), job_id),
            )
            self._save_project_record(connection, project)
        return job

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

    def list_chat_attachments(self, project_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if project_id:
                rows = connection.execute(
                    "SELECT payload FROM chat_attachments WHERE project_id = ? ORDER BY created_at",
                    (project_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload FROM chat_attachments ORDER BY created_at"
                ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def list_unreferenced_chat_attachments(self, project_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if project_id:
                rows = connection.execute(
                    "SELECT id, payload FROM chat_attachments WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
                message_rows = connection.execute(
                    "SELECT payload FROM chat_messages WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
            else:
                rows = connection.execute("SELECT id, payload FROM chat_attachments").fetchall()
                message_rows = connection.execute("SELECT payload FROM chat_messages").fetchall()
        referenced_ids = set()
        for row in message_rows:
            message = json.loads(row["payload"])
            referenced_ids.update(
                str(item["id"])
                for item in message.get("attachments", [])
                if isinstance(item, dict) and item.get("id")
            )
        return [json.loads(row["payload"]) for row in rows if row["id"] not in referenced_ids]

    def delete_unreferenced_chat_attachments(
        self, project_id: str | None = None, attachment_ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if project_id:
                rows = connection.execute(
                    "SELECT id, payload FROM chat_attachments WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
                message_rows = connection.execute(
                    "SELECT payload FROM chat_messages WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
            else:
                rows = connection.execute("SELECT id, payload FROM chat_attachments").fetchall()
                message_rows = connection.execute("SELECT payload FROM chat_messages").fetchall()
            referenced_ids = set()
            for row in message_rows:
                message = json.loads(row["payload"])
                referenced_ids.update(
                    str(item["id"])
                    for item in message.get("attachments", [])
                    if isinstance(item, dict) and item.get("id")
                )
            removed = []
            for row in rows:
                if row["id"] in referenced_ids or (attachment_ids is not None and row["id"] not in attachment_ids):
                    continue
                removed.append(json.loads(row["payload"]))
                connection.execute("DELETE FROM chat_attachments WHERE id = ?", (row["id"],))
        return removed

    def record_usage_event(self, event: dict[str, Any]) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO usage_events (id, project_id, created_at, modality, model_id, operation, status, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     project_id = excluded.project_id,
                     modality = excluded.modality,
                     model_id = excluded.model_id,
                     operation = excluded.operation,
                     status = excluded.status,
                     payload = excluded.payload""",
                (
                    event["id"], event.get("project_id"), event["created_at"],
                    event["modality"], event.get("model_id"), event["operation"],
                    event["status"], json.dumps(event, ensure_ascii=False),
                ),
            )
        return event

    def list_usage_events(self, *, limit: int = 500, project_id: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if project_id:
                rows = connection.execute(
                    "SELECT payload FROM usage_events WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                    (project_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload FROM usage_events ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def count_usage_events_since(self, since: str, *, modalities: set[str]) -> int:
        if not modalities:
            return 0
        placeholders = ", ".join("?" for _ in modalities)
        with self._connection() as connection:
            row = connection.execute(
                f"""SELECT COUNT(*) AS total FROM usage_events
                    WHERE created_at >= ? AND modality IN ({placeholders})
                      AND status NOT IN ('blocked_limit', 'cancelled')""",
                (since, *sorted(modalities)),
            ).fetchone()
        return int(row["total"])

    def media_storage_references(self) -> set[str]:
        pattern = re.compile(r"(?:^|/)media/([^/?#]+)")
        references: set[str] = set()

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
            elif isinstance(value, str):
                references.update(match.group(1) for match in pattern.finditer(value))

        with self._connection() as connection:
            for table in ("projects", "chat_messages", "chat_attachments"):
                rows = connection.execute(f"SELECT payload FROM {table}").fetchall()
                for row in rows:
                    collect(json.loads(row["payload"]))
        return references

    def list_chat_messages(self, project_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM chat_messages WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

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

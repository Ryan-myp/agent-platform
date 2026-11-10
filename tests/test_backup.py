from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services.backup import (
    BackupError,
    create_backup_archive,
    prune_backup_archives,
    restore_backup_archive,
    verify_backup_archive,
)
from app.store import ProjectStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_backup_round_trip_includes_projects_messages_jobs_usage_and_media_but_not_credentials(tmp_path):
    data_directory = tmp_path / "data"
    database_path = data_directory / "agent-platform.db"
    media_directory = data_directory / "media"
    media_directory.mkdir(parents=True)
    (media_directory / "attachment.txt").write_text("attachment body", encoding="utf-8")
    nested_media = media_directory / "renders" / "shot.mp4"
    nested_media.parent.mkdir()
    nested_media.write_bytes(b"fake video bytes")
    credentials_path = data_directory / "model-credentials.json"
    credentials_path.write_text('{"agnes-video":"must-not-be-backed-up"}', encoding="utf-8")

    store = ProjectStore(database_path)
    now = _now()
    project = {
        "id": "project-1",
        "title": "Backup test project",
        "mode": "chat",
        "created_at": now,
        "updated_at": now,
        "workflow": {},
    }
    store.save(project)
    store.append_chat_message(
        project["id"],
        {"id": "message-1", "created_at": now, "role": "user", "text": "hello", "attachments": [{"id": "attachment-1"}]},
    )
    store.save_chat_attachment({
        "id": "attachment-1",
        "project_id": project["id"],
        "created_at": now,
        "filename": "attachment.txt",
        "storage_key": "/media/attachment.txt",
        "mime_type": "text/plain",
        "size": 15,
    })
    store.record_usage_event({
        "id": "usage-1",
        "project_id": project["id"],
        "created_at": now,
        "modality": "text",
        "model_id": "fake-model",
        "operation": "chat",
        "status": "completed",
    })
    job = {
        "id": "video-job-1",
        "project_id": project["id"],
        "shot_id": "S01",
        "status": "polling",
        "created_at": now,
        "updated_at": now,
        "next_poll_at": None,
    }
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO video_jobs (id, project_id, shot_id, status, created_at, updated_at, next_poll_at, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job["id"], job["project_id"], job["shot_id"], job["status"],
                job["created_at"], job["updated_at"], job["next_poll_at"],
                json.dumps(job, ensure_ascii=False),
            ),
        )

    archive_path = tmp_path / "export.zip"
    created = create_backup_archive(database_path, media_directory, archive_path)
    assert created["file_count"] == 3
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert names == {"manifest.json", "agent-platform.db", "media/attachment.txt", "media/renders/shot.mp4"}
        assert b"must-not-be-backed-up" not in archive.read("agent-platform.db")
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["credentials_included"] is False
        assert all("credential" not in item["path"].lower() for item in manifest["files"])

    restored_directory = tmp_path / "restored"
    restored = restore_backup_archive(archive_path, restored_directory)
    assert restored["file_count"] == 3
    assert (restored_directory / "media" / "attachment.txt").read_text(encoding="utf-8") == "attachment body"
    assert (restored_directory / "media" / "renders" / "shot.mp4").read_bytes() == b"fake video bytes"
    assert not (restored_directory / "model-credentials.json").exists()

    restored_store = ProjectStore(restored_directory / "agent-platform.db")
    assert restored_store.get("project-1")["title"] == "Backup test project"
    assert restored_store.chat_history("project-1")[0]["text"] == "hello"
    assert restored_store.get_chat_attachment("attachment-1", "project-1")["filename"] == "attachment.txt"
    assert restored_store.list_usage_events(limit=10)[0]["id"] == "usage-1"
    assert restored_store.get_video_job("video-job-1")["shot_id"] == "S01"


def test_backup_verify_and_retention_prune_are_safe_by_default(tmp_path):
    database_path = tmp_path / "data" / "agent-platform.db"
    store = ProjectStore(database_path)
    now = _now()
    store.save({"id": "verify-project", "created_at": now, "updated_at": now, "workflow": {}})

    archive_path = tmp_path / "agent-platform-backup-20250101T000000000000Z.zip"
    create_backup_archive(database_path, database_path.parent / "media", archive_path)
    verified = verify_backup_archive(archive_path)
    assert verified["valid"] is True
    assert verified["file_count"] == 1
    assert verified["total_bytes"] > 0

    backup_directory = tmp_path / "rotation"
    backup_directory.mkdir()
    names = [
        "agent-platform-backup-20250101T000000000000Z.zip",
        "agent-platform-backup-20250102T000000000000Z.zip",
        "agent-platform-backup-20250103T000000000000Z.zip",
    ]
    for name in names:
        (backup_directory / name).write_bytes(b"placeholder")
    unrelated = backup_directory / "manual-backup.zip"
    unrelated.write_bytes(b"keep me")

    preview = prune_backup_archives(backup_directory, keep=2)
    assert preview["dry_run"] is True
    assert [Path(path).name for path in preview["would_remove"]] == [names[0]]
    assert (backup_directory / names[0]).exists()
    assert unrelated.exists()

    result = prune_backup_archives(backup_directory, keep=2, dry_run=False)
    assert [Path(path).name for path in result["removed"]] == [names[0]]
    assert not (backup_directory / names[0]).exists()
    assert unrelated.exists()


def test_restore_rejects_path_traversal_and_never_overwrites_existing_target(tmp_path):
    malicious_archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious_archive, "w") as archive:
        archive.writestr("../escape.txt", "should not be written")
    target = tmp_path / "restored"
    with pytest.raises(BackupError, match="不安全的文件路径"):
        restore_backup_archive(malicious_archive, target)
    assert not target.exists()
    assert not (tmp_path / "escape.txt").exists()

    existing_archive = tmp_path / "existing-target.zip"
    with zipfile.ZipFile(existing_archive, "w") as archive:
        archive.writestr("not-a-real-backup", "")
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(BackupError, match="尚不存在的新目录"):
        restore_backup_archive(existing_archive, target)
    assert sentinel.read_text(encoding="utf-8") == "preserve"

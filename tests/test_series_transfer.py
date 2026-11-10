from __future__ import annotations

import base64
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
import zipfile

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.providers.catalog import ModelCatalog

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "models.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client(tmp_path: Path) -> TestClient:
    catalog = ModelCatalog.load(CATALOG_PATH)
    settings = Settings(
        database_path=tmp_path / "series-transfer.db",
        model_catalog_path=CATALOG_PATH,
        model_catalog=catalog,
        api_keys={profile.api_key_env: "test-key" for profile in catalog.profiles.values()},
    )
    return TestClient(create_app(settings=settings), client=("127.0.0.1", 54321))


def _episode_payload(series_id: str, number: int) -> dict:
    return {
        "track": "drama",
        "business": "失踪事件的调查与追踪",
        "product": "雾港来信",
        "audience": "悬疑短剧观众",
        "platform": "竖屏短剧",
        "style": "冷峻、克制",
        "duration_seconds": 60,
        "series_id": series_id,
        "episode_number": number,
    }


def test_series_bundle_round_trip_copies_assets_episodes_chat_and_media_without_resuming_tasks(tmp_path):
    client = _client(tmp_path)
    headers = {"Origin": "http://testserver"}
    series = client.post("/api/series", json={
        "title": "雾港来信",
        "logline": "失踪的姐姐留下线索。",
        "visual_style": "雨夜与低饱和冷色。",
        "notes": "每集保留新的悬念。",
    }, headers=headers).json()
    asset_response = client.post(f"/api/series/{series['id']}/assets", json={
        "kind": "character", "name": "林澄", "description": "寻找姐姐的妹妹。",
        "visual_prompt": "深色雨衣，短发。", "continuity_notes": "始终佩戴旧表。",
    }, headers=headers)
    assert asset_response.status_code == 201
    asset_id = asset_response.json()["id"]
    reference_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/UNkAAAAASUVORK5CYII="
    )
    uploaded_asset = client.post(
        f"/api/series/{series['id']}/assets/{asset_id}/media",
        files=[("files", ("character-reference.png", reference_png, "image/png"))],
        headers=headers,
    )
    assert uploaded_asset.status_code == 200, uploaded_asset.text
    source_asset_media_url = uploaded_asset.json()["media"][0]["asset_url"]

    workflow = client.app.state.workflow
    store = workflow.store
    asset_store = client.app.state.assets
    media_url = asset_store.save(b"episode reference bytes", ".png")
    media_key = Path(urlsplit(media_url).path).name
    episodes = []
    for number in (1, 2):
        project = workflow.create_project(_episode_payload(series["id"], number))
        project["workflow"]["script"]["reference_image"] = media_url
        if number == 2:
            project["workflow"]["video"]["shots"] = [{"shot_id": "S01", "video": {
                "status": "queued", "job_id": "local-queue-id", "provider_task_id": "remote-task-secret",
            }}]
        store.save(project)
        attachment_id = str(uuid4())
        attachment = {
            "id": attachment_id, "project_id": project["id"], "created_at": _now(),
            "filename": "reference.png", "mime_type": "image/png", "size": len(b"episode reference bytes"),
            "storage_key": media_key, "url": media_url,
        }
        store.save_chat_attachment(attachment)
        store.append_chat_message(project["id"], {
            "id": str(uuid4()), "project_id": project["id"], "created_at": _now(),
            "role": "user", "text": "请参考这张图。",
            "attachments": [{"id": attachment_id, "filename": attachment["filename"], "url": media_url}],
        })
        episodes.append(project)

    exported = client.get(f"/api/series/{series['id']}/export", headers=headers)
    assert exported.status_code == 200
    imported_response = client.post("/api/series/import", files={
        "file": ("series.zip", exported.content, "application/zip"),
    }, headers=headers)
    assert imported_response.status_code == 201
    imported = imported_response.json()
    imported_series = imported["series"]
    assert imported_series["id"] != series["id"]
    assert imported_series["title"].endswith("（导入副本）")
    assert len(imported["episodes"]) == 2
    assert {item["episode_number"] for item in imported["episodes"]} == {1, 2}
    assert all(item["id"] not in {episode["id"] for episode in episodes} for item in imported["episodes"])
    detail = client.get(f"/api/series/{imported_series['id']}").json()
    assert len(detail["assets"]) == 1
    assert detail["assets"][0]["series_id"] == imported_series["id"]
    assert len(detail["episodes"]) == 2
    imported_asset_media_url = detail["assets"][0]["media"][0]["asset_url"]
    assert imported_asset_media_url != source_asset_media_url
    assert (asset_store.directory / Path(urlsplit(imported_asset_media_url).path).name).read_bytes() == reference_png

    imported_episode = next(item for item in imported["episodes"] if item["episode_number"] == 2)
    video = imported_episode["workflow"]["video"]["shots"][0]["video"]
    assert video["status"] == "interrupted"
    assert "job_id" not in video and "provider_task_id" not in video
    assert "remote-task-secret" not in str(imported)
    copied_url = imported_episode["workflow"]["script"]["reference_image"]
    assert copied_url != media_url
    assert (asset_store.directory / Path(urlsplit(copied_url).path).name).read_bytes() == b"episode reference bytes"
    imported_messages = store.list_chat_messages(imported_episode["id"])
    imported_attachments = store.list_chat_attachments(imported_episode["id"])
    assert len(imported_messages) == len(imported_attachments) == 1
    assert imported_messages[0]["attachments"][0]["id"] == imported_attachments[0]["id"]
    assert imported_attachments[0]["storage_key"] != media_key


def _marketing_track_bundle(bundle_bytes: bytes) -> bytes:
    files = {}
    with zipfile.ZipFile(io.BytesIO(bundle_bytes), "r") as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    project_name = "episodes/0001/project.json"
    project = json.loads(files[project_name])
    project["track"] = "marketing"
    files[project_name] = json.dumps(project, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    manifest = json.loads(files["manifest.json"])
    record = next(item for item in manifest["files"] if item["path"] == project_name)
    record["size"] = len(files[project_name])
    record["sha256"] = hashlib.sha256(files[project_name]).hexdigest()
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return output.getvalue()


def test_series_transfer_rejects_cross_origin_and_invalid_archives_without_partial_import(tmp_path):
    client = _client(tmp_path)
    headers = {"Origin": "http://testserver"}
    series = client.post("/api/series", json={"title": "安全测试系列"}, headers=headers).json()
    project = client.app.state.workflow.create_project(_episode_payload(series["id"], 1))
    archive = client.get(f"/api/series/{series['id']}/export", headers=headers)
    assert archive.status_code == 200

    cross_origin = client.post("/api/series/import", files={
        "file": ("series.zip", archive.content, "application/zip"),
    }, headers={"Origin": "https://attacker.example"})
    assert cross_origin.status_code == 403

    invalid = io.BytesIO()
    with zipfile.ZipFile(invalid, "w") as package:
        package.writestr("../outside.txt", b"not safe")
    before_series = len(client.get("/api/series").json())
    rejected = client.post("/api/series/import", files={
        "file": ("invalid.zip", invalid.getvalue(), "application/zip"),
    }, headers=headers)
    assert rejected.status_code == 400
    assert len(client.get("/api/series").json()) == before_series
    assert len(client.get("/api/projects").json()) == 1
    assert client.get(f"/api/projects/{project['id']}").status_code == 200

    invalid_episode = _marketing_track_bundle(archive.content)
    invalid_track = client.post("/api/series/import", files={
        "file": ("marketing-episode.zip", invalid_episode, "application/zip"),
    }, headers=headers)
    assert invalid_track.status_code == 400
    assert len(client.get("/api/series").json()) == before_series
    assert len(client.get("/api/projects").json()) == 1

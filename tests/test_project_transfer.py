from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.providers.catalog import ModelCatalog
from app.providers.media import AssetStore
from app.services.project_transfer import ProjectTransferError, ProjectTransferService
from app.store import ProjectStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project(project_id: str = "project-original") -> dict:
    now = _now()
    return {
        "id": project_id,
        "title": "Harbor story",
        "mode": "workflow",
        "track": "drama",
        "created_at": now,
        "updated_at": now,
        "archived": False,
        "series_id": "series-original",
        "episode_number": 3,
        "workflow": {},
    }


def _store_with_series_and_media(tmp_path: Path):
    store = ProjectStore(tmp_path / "agent-platform.db")
    assets = AssetStore(tmp_path / "media")
    media_url = assets.save(b"image bytes", ".png")
    media_key = Path(urlsplit(media_url).path).name
    now = _now()
    store.save_series({
        "id": "series-original",
        "title": "Harbor series",
        "logline": "A mystery by the harbor.",
        "visual_style": "Rainy, restrained.",
        "notes": "Maintain continuity.",
        "archived": False,
        "created_at": now,
        "updated_at": now,
    })
    store.save_series_asset({
        "id": "series-asset-original",
        "series_id": "series-original",
        "kind": "character",
        "name": "Lin",
        "description": "Investigator.",
        "visual_prompt": "Dark raincoat.",
        "continuity_notes": "Wears an old watch.",
        "archived": False,
        "created_at": now,
        "updated_at": now,
    })
    project = _project()
    project["workflow"] = {
        "storyboard": {"reference_image": media_url},
        "video": {"shots": [{"shot_id": "S01", "video": {
            "status": "queued", "job_id": "local-job-id", "task_id": "provider-task-id",
        }}]},
    }
    store.save(project)
    attachment = {
        "id": "attachment-original",
        "project_id": project["id"],
        "created_at": now,
        "filename": "reference.png",
        "mime_type": "image/png",
        "size": 11,
        "storage_key": media_key,
        "url": media_url,
    }
    store.save_chat_attachment(attachment)
    store.append_chat_message(project["id"], {
        "id": "message-original",
        "project_id": project["id"],
        "created_at": now,
        "role": "user",
        "text": "Use this reference.",
        "attachments": [{"id": attachment["id"], "filename": attachment["filename"], "url": media_url}],
    })
    store.record_usage_event({
        "id": "usage-original",
        "project_id": project["id"],
        "created_at": now,
        "modality": "image",
        "model_id": "fake-image",
        "operation": "generate",
        "status": "succeeded",
    })
    return store, assets, project, media_url


def test_project_bundle_round_trip_remaps_records_and_media_without_resuming_tasks(tmp_path):
    store, assets, original, media_url = _store_with_series_and_media(tmp_path)
    original["storyboard_canvas"] = {
        "version": 1,
        "updated_at": _now(),
        "cards": [{
            "id": "canvas-card-1", "shot_id": "S01", "title": "打开旧仓库",
            "duration_seconds": 6, "purpose": "建立悬念", "visual_description": "林澄推开仓库门。",
            "camera": "缓慢推近", "voiceover": "", "screen_text": "雾港", "transition": "切入",
            "continuity": "雨夜。",
        }],
    }
    store.save(original)
    service = ProjectTransferService(store, assets)
    archive_path = tmp_path / "project.zip"
    store.add_project_review({
        "id": "review-original", "project_id": original["id"], "target_type": "script",
        "target_id": "", "target_label": "脚本", "target_revision": "a" * 64,
        "decision": "comment", "category": "continuity", "note": "核对人物称谓。",
        "timecode": None, "created_at": _now(),
    })

    service.create_export(original["id"], archive_path)
    with archive_path.open("rb") as package:
        result = service.import_bundle(package)
    imported = result["project"]

    assert imported["id"] != original["id"]
    assert imported["title"].endswith("（导入副本）")
    assert imported["series_id"] != original["series_id"]
    assert imported["episode_number"] == original["episode_number"]
    assert imported["storyboard_canvas"]["cards"][0]["visual_description"] == "林澄推开仓库门。"
    assert imported["storyboard_canvas"]["cards"][0]["id"] == "canvas-card-1"
    assert "其他分集" in result["warnings"][0]
    assert "未恢复供应商任务" in result["warnings"][1]
    video = imported["workflow"]["video"]["shots"][0]["video"]
    assert video["status"] == "interrupted"
    assert "job_id" not in video and "task_id" not in video
    assert "provider-task-id" not in json.dumps(imported)

    imported_media_url = imported["workflow"]["storyboard"]["reference_image"]
    assert imported_media_url != media_url
    imported_media_path = assets.directory / Path(urlsplit(imported_media_url).path).name
    assert imported_media_path.read_bytes() == b"image bytes"

    messages = store.list_chat_messages(imported["id"])
    attachments = store.list_chat_attachments(imported["id"])
    assert len(messages) == len(attachments) == 1
    assert messages[0]["id"] != "message-original"
    assert messages[0]["attachments"][0]["id"] == attachments[0]["id"]
    assert messages[0]["attachments"][0]["url"] == attachments[0]["url"]
    assert attachments[0]["id"] != "attachment-original"
    assert attachments[0]["storage_key"] != Path(urlsplit(media_url).path).name
    assert store.get_series(imported["series_id"])["title"].endswith("（导入副本）")
    assert store.list_series_episodes(imported["series_id"])[0]["id"] == imported["id"]
    imported_reviews = store.list_project_reviews(imported["id"])
    assert len(imported_reviews) == 1
    assert imported_reviews[0]["id"] != "review-original"
    assert imported_reviews[0]["project_id"] == imported["id"]
    assert imported_reviews[0]["note"] == "核对人物称谓。"
    assert store.list_usage_events(project_id=imported["id"]) == []


def test_project_import_rejects_tampered_archive_without_partial_records_or_files(tmp_path):
    store, assets, project, _ = _store_with_series_and_media(tmp_path)
    service = ProjectTransferService(store, assets)
    source = tmp_path / "source.zip"
    tampered = tmp_path / "tampered.zip"
    service.create_export(project["id"], source)

    with zipfile.ZipFile(source) as original:
        members = {name: original.read(name) for name in original.namelist()}
    project_data = json.loads(members["project.json"])
    project_data["title"] = "tampered"
    members["project.json"] = json.dumps(project_data).encode("utf-8")
    with zipfile.ZipFile(tampered, "w") as output:
        for name, content in members.items():
            output.writestr(name, content)

    before = {path.name for path in assets.directory.iterdir()}
    with tampered.open("rb") as package:
        with pytest.raises(ProjectTransferError, match="损坏|校验失败"):
            service.import_bundle(package)
    assert {item["id"] for item in store.list()} == {project["id"]}
    assert {path.name for path in assets.directory.iterdir()} == before


def test_project_import_accepts_legacy_bundle_without_review_file(tmp_path):
    store = ProjectStore(tmp_path / "legacy.db")
    assets = AssetStore(tmp_path / "legacy-media")
    project = _project("legacy-project")
    project["series_id"] = None
    project["episode_number"] = None
    store.save(project)
    service = ProjectTransferService(store, assets)
    source = tmp_path / "current.zip"
    service.create_export(project["id"], source)
    with zipfile.ZipFile(source) as archive:
        members = {name: archive.read(name) for name in archive.namelist() if name != "project-reviews.json"}
    manifest = json.loads(members["manifest.json"])
    manifest["files"] = [item for item in manifest["files"] if item["path"] != "project-reviews.json"]
    members["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    legacy = io.BytesIO()
    with zipfile.ZipFile(legacy, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)

    legacy.seek(0)
    imported = service.import_bundle(legacy)["project"]
    assert store.list_project_reviews(imported["id"]) == []


def test_project_import_rejects_malformed_review_records_without_partial_import(tmp_path):
    store, assets, project, _ = _store_with_series_and_media(tmp_path)
    store.add_project_review({
        "id": "review-original", "project_id": project["id"], "target_type": "script",
        "target_id": "", "target_label": "脚本", "target_revision": "a" * 64,
        "decision": "comment", "category": "general", "note": "请核对。",
        "timecode": None, "created_at": _now(),
    })
    service = ProjectTransferService(store, assets)
    source = tmp_path / "valid.zip"
    service.create_export(project["id"], source)
    with zipfile.ZipFile(source) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    reviews = json.loads(members["project-reviews.json"])
    reviews[0]["target_type"] = []
    members["project-reviews.json"] = json.dumps(reviews, ensure_ascii=False).encode("utf-8")
    manifest = json.loads(members["manifest.json"])
    record = next(item for item in manifest["files"] if item["path"] == "project-reviews.json")
    record["size"] = len(members["project-reviews.json"])
    record["sha256"] = hashlib.sha256(members["project-reviews.json"]).hexdigest()
    members["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    malformed = io.BytesIO()
    with zipfile.ZipFile(malformed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)

    malformed.seek(0)
    with pytest.raises(ProjectTransferError, match="无效审阅记录"):
        service.import_bundle(malformed)
    assert [item["id"] for item in store.list()] == [project["id"]]


def test_project_import_rejects_archive_path_traversal(tmp_path):
    store = ProjectStore(tmp_path / "agent-platform.db")
    assets = AssetStore(tmp_path / "media")
    service = ProjectTransferService(store, assets)
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("../outside.txt", b"not safe")

    package.seek(0)
    with pytest.raises(ProjectTransferError, match="不安全的文件路径"):
        service.import_bundle(package)
    assert store.list() == []
    assert list(assets.directory.iterdir()) == []


def test_project_transfer_api_is_local_only_and_round_trips(tmp_path):
    root = Path(__file__).resolve().parents[1]
    catalog_path = root / "config" / "models.json"
    catalog = ModelCatalog.load(catalog_path)
    settings = Settings(
        database_path=tmp_path / "agent-platform.db",
        model_catalog_path=catalog_path,
        model_catalog=catalog,
        api_keys={profile.api_key_env: "test-key" for profile in catalog.profiles.values()},
    )
    client = TestClient(create_app(settings=settings), client=("127.0.0.1", 54001))
    project = client.app.state.workflow.create_project({
        "mode": "chat",
        "title": "Transfer API test",
    })
    headers = {"Origin": "http://testserver"}

    response = client.get(f"/api/projects/{project['id']}/export", headers=headers)
    assert response.status_code == 200
    imported = client.post(
        "/api/projects/import",
        files={"file": ("project.zip", response.content, "application/zip")},
        headers=headers,
    )
    assert imported.status_code == 201
    assert imported.json()["project"]["id"] != project["id"]
    assert imported.json()["project"]["title"].endswith("（导入副本）")

    cross_origin = client.post(
        "/api/projects/import",
        files={"file": ("project.zip", response.content, "application/zip")},
        headers={"Origin": "https://attacker.example"},
    )
    assert cross_origin.status_code == 403

    active_media_url = client.app.state.assets.save(b"<script>document.body.textContent='unsafe'</script>", ".html")
    active_media = client.get(active_media_url)
    assert active_media.status_code == 200
    assert active_media.headers["x-content-type-options"] == "nosniff"
    assert active_media.headers["content-disposition"] == "attachment"
    assert "sandbox" in active_media.headers["content-security-policy"]

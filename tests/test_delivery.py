from __future__ import annotations

import hashlib
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
from app.services.delivery import DeliveryPackageError, DeliveryService
from app.services.video_assembly import VideoAssemblyService
from app.store import ProjectStore

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "models.json"


def _settings(tmp_path: Path) -> Settings:
    catalog = ModelCatalog.load(CATALOG_PATH)
    return Settings(
        database_path=tmp_path / "delivery.db",
        model_catalog_path=CATALOG_PATH,
        model_catalog=catalog,
        api_keys={profile.api_key_env: "test-key" for profile in catalog.profiles.values()},
    )


def _project(project_id: str, media_url: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": project_id,
        "title": "雾港来信 EP01",
        "mode": "workflow",
        "track": "drama",
        "episode_number": 1,
        "brief": {
            "business": "妹妹寻找失踪的姐姐",
            "product": "雾港来信",
            "audience": "悬疑短剧观众",
            "platform": "竖屏短剧",
            "style": "冷峻写实",
            "duration_seconds": 60,
        },
        "created_at": now,
        "updated_at": now,
        "workflow": {
            "script": {"status": "approved", "content": "林澄找到姐姐留下的录音带。"},
            "prompt": {"status": "approved", "content": "低饱和冷色，雨夜港口。"},
            "storyboard": {"status": "ready", "content": "S01：林澄进入仓库。"},
            "scenes": {
                "status": "approved",
                "content": json.dumps({"shots": [{"shot_id": "S01"}], "edit_plan": {"sequence": ["S01"]}}, ensure_ascii=False),
            },
            "video": {
                "status": "partial",
                "shots": [{
                    "shot_id": "S01",
                    "duration_seconds": 6,
                    "image": {"status": "ready", "asset_url": media_url, "job_id": "private-job-id"},
                    "video": {"status": "ready", "asset_url": media_url, "task_id": "private-provider-task"},
                }],
                "external_asset": {"asset_url": "https://media.example.test/clip.mp4?token=do-not-export"},
            },
        },
    }


def test_delivery_package_contains_human_readable_artifacts_media_and_verified_manifest(tmp_path):
    store = ProjectStore(tmp_path / "test.db")
    assets = AssetStore(tmp_path / "media")
    media_url = assets.save(b"local image bytes", ".png")
    project = _project("project-1", media_url)
    store.save(project)
    output = tmp_path / "delivery.zip"

    DeliveryService(store, assets).create_package(project["id"], output)

    media_key = Path(urlsplit(media_url).path).name
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert {
            "README.md", "01-script.md", "02-visual-guide.md", "03-storyboard.md",
            "04-shot-plan.json", "production-report.json", "manifest.json", f"media/{media_key}",
        }.issubset(names)
        assert "project.json" not in names
        assert archive.read("01-script.md").decode("utf-8").find("林澄找到姐姐") >= 0
        assert archive.read(f"media/{media_key}") == b"local image bytes"
        assert "本地合成 MP4 也需人工审核" in archive.read("README.md").decode("utf-8")
        report = json.loads(archive.read("production-report.json"))
        assert report["final_video_assembled"] is False
        assert report["shots"][0]["image_included"] is True
        assert report["shots"][0]["video_included"] is True
        assert any(item.get("reason") == "remote_url_not_downloaded" for item in report["media"])
        manifest = json.loads(archive.read("manifest.json"))
        for record in manifest["files"]:
            payload = archive.read(record["path"])
            assert len(payload) == record["size"]
            assert hashlib.sha256(payload).hexdigest() == record["sha256"]
        package_bytes = b"".join(archive.read(name) for name in names)
        assert b"private-job-id" not in package_bytes
        assert b"private-provider-task" not in package_bytes
        assert b"do-not-export" not in package_bytes


def test_delivery_package_fails_closed_when_referenced_local_media_is_missing(tmp_path):
    store = ProjectStore(tmp_path / "test.db")
    assets = AssetStore(tmp_path / "media")
    project = _project("project-missing", "/media/not-on-disk.png")
    store.save(project)

    with pytest.raises(DeliveryPackageError) as error:
        DeliveryService(store, assets).create_package(project["id"], tmp_path / "missing.zip")

    assert error.value.status_code == 409
    assert "不存在或不安全" in str(error.value)


def test_delivery_download_is_limited_to_same_origin_loopback(tmp_path):
    client = TestClient(create_app(settings=_settings(tmp_path)), client=("127.0.0.1", 54321))
    created = client.post("/api/projects", json={
        "track": "drama",
        "business": "妹妹寻找失踪的姐姐",
        "product": "雾港来信",
        "audience": "悬疑短剧观众",
        "platform": "竖屏短剧",
        "style": "冷峻写实",
        "duration_seconds": 60,
    }).json()
    path = f"/api/projects/{created['id']}/delivery"

    exported = client.get(path, headers={"Origin": "http://testserver"})
    cross_origin = client.get(path, headers={"Origin": "https://attacker.example"})

    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(__import__("io").BytesIO(exported.content)) as archive:
        assert "README.md" in archive.namelist()
        assert "production-report.json" in archive.namelist()
    assert cross_origin.status_code == 403

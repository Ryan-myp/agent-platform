from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from app.providers.media import AssetStore
from app.services.delivery import DeliveryService
from app.services.video_assembly import VideoAssemblyService
from app.store import ProjectStore


def _project(project_id: str, media_url: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": project_id,
        "title": "雾港来信 EP01",
        "mode": "workflow",
        "track": "drama",
        "episode_number": 1,
        "brief": {"duration_seconds": 60, "platform": "竖屏短剧"},
        "created_at": now,
        "updated_at": now,
        "workflow": {
            "script": {"status": "approved", "content": "林澄找到姐姐留下的录音带。"},
            "prompt": {"status": "approved", "content": "雨夜港口。"},
            "storyboard": {"status": "ready", "content": "S01：林澄进入仓库。"},
            "scenes": {"status": "approved", "content": json.dumps({"shots": [{"shot_id": "S01"}], "edit_plan": {"sequence": ["S01"]}})},
            "video": {
                "status": "partial",
                "shots": [{
                    "shot_id": "S01", "duration_seconds": 6,
                    "image": {"status": "ready", "asset_url": media_url},
                    "video": {"status": "ready", "asset_url": media_url},
                }],
                "external_asset": {"asset_url": "https://media.example.test/clip.mp4"},
            },
        },
    }


def test_delivery_includes_only_current_assembled_video_and_does_not_map_remote_paths_locally(tmp_path):
    store = ProjectStore(tmp_path / "delivery-final.db")
    assets = AssetStore(tmp_path / "delivery-media")
    shot_media = assets.save(b"local shot media", ".png")
    final_bytes = b"\x00\x00\x00\x18ftypisom" + b"f" * 64
    final_url = assets.save(final_bytes, ".mp4")
    old_final_url = assets.save(b"old assembled video", ".mp4")
    collision_url = assets.save(b"must not be selected by a remote URL", ".mp4")
    project = _project("delivery-final", shot_media)
    project["workflow"]["video"]["external_asset"]["asset_url"] = (
        f"https://attacker.example/media/{Path(urlsplit(collision_url).path).name}"
    )
    project["workflow"]["video"]["shots"][0]["video_prompt"] = "镜头提示词"
    project["workflow"]["video"]["final_video"] = {
        "status": "ready",
        "asset_url": final_url,
        "source_signature": VideoAssemblyService._signature(project),
        "size_bytes": len(final_bytes),
        "duration_seconds": 6.0,
        "sha256": hashlib.sha256(final_bytes).hexdigest(),
    }
    project["workflow"]["video"]["final_video_versions"] = [{"asset_url": old_final_url}]
    store.save(project)

    current_package = tmp_path / "current.zip"
    DeliveryService(store, assets).create_package(project["id"], current_package)
    final_key = Path(urlsplit(final_url).path).name
    old_key = Path(urlsplit(old_final_url).path).name
    collision_key = Path(urlsplit(collision_url).path).name
    with zipfile.ZipFile(current_package) as archive:
        names = set(archive.namelist())
        assert f"media/{final_key}" in names
        assert f"media/{old_key}" not in names
        assert f"media/{collision_key}" not in names
        report = json.loads(archive.read("production-report.json"))
        assert report["final_video"]["included"] is True
        assert report["final_video_assembled"] is True
        assert any(item.get("reason") == "remote_url_not_downloaded" for item in report["media"])

    (assets.directory / final_key).write_bytes(b"tampered final mp4")
    corrupt_package = tmp_path / "corrupt.zip"
    DeliveryService(store, assets).create_package(project["id"], corrupt_package)
    with zipfile.ZipFile(corrupt_package) as archive:
        assert f"media/{final_key}" not in archive.namelist()
        corrupt_report = json.loads(archive.read("production-report.json"))
        assert corrupt_report["final_video"]["status"] == "stale"
        assert "校验失败" in corrupt_report["final_video"]["stale_reason"]

    stale_project = store.get(project["id"])
    stale_project["workflow"]["video"]["shots"][0]["video_prompt"] = "修改后已过期的镜头提示词"
    store.save(stale_project)
    stale_package = tmp_path / "stale.zip"
    DeliveryService(store, assets).create_package(project["id"], stale_package)
    with zipfile.ZipFile(stale_package) as archive:
        names = set(archive.namelist())
        assert f"media/{final_key}" not in names
        stale_report = json.loads(archive.read("production-report.json"))
        assert stale_report["final_video"]["included"] is False
        assert stale_report["final_video"]["status"] == "stale"
        assert stale_report["final_video_assembled"] is False

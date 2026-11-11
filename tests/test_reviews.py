from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import API_CONTRACT_VERSION, create_app
from app.providers.catalog import ModelCatalog
from app.services.reviews import ReviewError, ReviewService
from app.services.video_assembly import VideoAssemblyService

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "models.json"
HEADERS = {"Origin": "http://testserver"}


def _client(tmp_path: Path) -> TestClient:
    catalog = ModelCatalog.load(CATALOG_PATH)
    settings = Settings(
        database_path=tmp_path / "reviews.db",
        model_catalog_path=CATALOG_PATH,
        model_catalog=catalog,
        api_keys={profile.api_key_env: "test-key" for profile in catalog.profiles.values()},
    )
    return TestClient(create_app(settings=settings), client=("127.0.0.1", 54321))


def _create_project(client: TestClient) -> dict:
    response = client.post("/api/projects", json={
        "track": "drama", "business": "妹妹调查姐姐的失踪。", "product": "雾港来信",
        "audience": "悬疑短剧观众", "platform": "竖屏短剧", "style": "克制写实",
        "duration_seconds": 60,
    })
    assert response.status_code == 201
    return response.json()


def _review(target_type: str, revision: str, **values) -> dict:
    return {
        "target_type": target_type,
        "target_revision": revision,
        "decision": values.pop("decision", "approved"),
        **values,
    }


def test_script_review_is_version_bound_and_project_list_only_returns_count(tmp_path):
    client = _client(tmp_path)
    created = _create_project(client)
    project_id = created["id"]
    workflow = client.app.state.workflow
    project = workflow.get_project(project_id)
    project["workflow"]["script"].update({
        "status": "ready", "content": "妹妹在雨夜发现姐姐的录音带。",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    workflow.store.save(project)

    current = client.get(f"/api/projects/{project_id}").json()
    revision = current["review_targets"]["script"]["revision"]
    submitted = client.post(
        f"/api/projects/{project_id}/reviews",
        json=_review("script", revision), headers=HEADERS,
    )
    assert submitted.status_code == 201
    record = submitted.json()["review_records"][0]
    assert record["decision"] == "approved"
    assert record["is_current"] is True

    changed = workflow.get_project(project_id)
    changed["workflow"]["script"]["content"] += " 她决定追查线索。"
    changed["workflow"]["script"]["updated_at"] = datetime.now(timezone.utc).isoformat()
    workflow.store.save(changed)
    refreshed = client.get(f"/api/projects/{project_id}").json()
    assert refreshed["review_records"][0]["is_current"] is False

    stale = client.post(
        f"/api/projects/{project_id}/reviews",
        json=_review("script", revision), headers=HEADERS,
    )
    assert stale.status_code == 409
    new_revision = refreshed["review_targets"]["script"]["revision"]
    second = client.post(
        f"/api/projects/{project_id}/reviews",
        json=_review("script", new_revision, decision="revision_requested", note="补足妹妹追查的动机。"),
        headers=HEADERS,
    )
    assert second.status_code == 201
    listed = client.get("/api/projects").json()
    listed_project = next(item for item in listed if item["id"] == project_id)
    assert listed_project["review_count"] == 2
    assert "review_records" not in listed_project


def test_review_rejects_timecodes_outside_media_and_stale_concurrent_write(tmp_path):
    client = _client(tmp_path)
    created = _create_project(client)
    project_id = created["id"]
    workflow = client.app.state.workflow
    project = workflow.get_project(project_id)
    project["workflow"]["scenes"] = {"status": "approved", "content": "已确认场景。"}
    project["workflow"]["video"] = {
        "status": "partial",
        "shots": [{
            "shot_id": "S01", "duration_seconds": 6,
            "video": {"status": "ready", "asset_url": "/media/clip.mp4", "completed_at": "2025-01-01T00:00:00+00:00", "version": 1, "sha256": "b" * 64},
        }],
        "final_video": {},
    }
    final = project["workflow"]["video"]["final_video"]
    final.update({
        "status": "ready", "asset_url": "/media/final.mp4", "sha256": "a" * 64,
        "duration_seconds": 6, "rendered_at": "2025-01-01T00:00:00+00:00",
    })
    final["source_signature"] = VideoAssemblyService._signature(project)
    workflow.store.save(project)

    current = client.get(f"/api/projects/{project_id}").json()
    clip_revision = current["review_targets"]["clip:S01"]["revision"]
    too_late = client.post(
        f"/api/projects/{project_id}/reviews",
        json=_review("clip", clip_revision, target_id="S01", timecode="00:07", note="检查结尾。"),
        headers=HEADERS,
    )
    assert too_late.status_code == 400
    assert "超出" in too_late.json()["detail"]
    clip_review = client.post(
        f"/api/projects/{project_id}/reviews",
        json=_review("clip", clip_revision, target_id="S01", timecode="00:05", note="检查结尾。"),
        headers=HEADERS,
    )
    assert clip_review.status_code == 201
    final_revision = current["review_targets"]["final_video"]["revision"]
    final_too_late = client.post(
        f"/api/projects/{project_id}/reviews",
        json=_review("final_video", final_revision, timecode="00:07", note="检查片尾。"),
        headers=HEADERS,
    )
    assert final_too_late.status_code == 400

    service = ReviewService(workflow.store)
    stale_project = workflow.get_project(project_id)
    stale_revision = service.target_states(stale_project)["clip:S01"]["revision"]
    changed = workflow.get_project(project_id)
    changed["title"] = "并发修改项目标题"
    changed["updated_at"] = datetime.now(timezone.utc).isoformat()
    workflow.store.save(changed)
    with pytest.raises(ReviewError, match="项目内容已更新") as error:
        service.add_review(stale_project, _review("clip", stale_revision, target_id="S01", note="通过。"))
    assert error.value.status_code == 409


def test_review_api_is_same_origin_only_and_advertised_by_contract(tmp_path):
    client = _client(tmp_path)
    project = _create_project(client)
    cross_origin = client.post(
        f"/api/projects/{project['id']}/reviews",
        json=_review("script", "0" * 64),
        headers={"Origin": "https://attacker.example"},
    )
    assert cross_origin.status_code == 403
    status = client.get("/api/system/status").json()
    assert status["api_contract_version"] == API_CONTRACT_VERSION == 7
    assert "production_reviews" in status["features"]

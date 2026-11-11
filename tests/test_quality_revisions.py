from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import API_CONTRACT_VERSION, create_app
from app.providers.catalog import ModelCatalog

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "models.json"


def _client(tmp_path: Path) -> TestClient:
    catalog = ModelCatalog.load(CATALOG_PATH)
    settings = Settings(
        database_path=tmp_path / "quality-revisions.db",
        model_catalog_path=CATALOG_PATH,
        model_catalog=catalog,
        api_keys={profile.api_key_env: "test-key" for profile in catalog.profiles.values()},
    )
    return TestClient(create_app(settings=settings), client=("127.0.0.1", 54321))


def _project_payload() -> dict:
    return {
        "track": "drama", "business": "妹妹调查姐姐的失踪。", "product": "雾港来信",
        "audience": "悬疑短剧观众", "platform": "竖屏短剧", "style": "克制写实",
        "duration_seconds": 60,
    }


def test_revision_restore_quality_report_and_diagnostics_routes(tmp_path):
    client = _client(tmp_path)
    project = client.post("/api/projects", json=_project_payload()).json()
    project_id = project["id"]
    original = "第一版脚本：妹妹在雨夜发现一卷录音带，标签上写着待确认。"
    current = "第二版脚本：妹妹在港口发现一卷录音带，并决定追查姐姐的去向；动机待确认。"

    workflow = client.app.state.workflow
    seeded = workflow.get_project(project_id)
    seeded["workflow"]["script"].update({
        "status": "ready", "content": original,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    workflow.store.save(seeded)
    second = client.put(f"/api/projects/{project_id}/content/script", json={"content": current})
    assert second.status_code == 200
    revisions = client.get(f"/api/projects/{project_id}/content/script/revisions")
    assert revisions.status_code == 200
    archived = revisions.json()["revisions"]
    assert len(archived) == 1
    revision_id = archived[0]["revision_id"]
    assert archived[0]["content"] == original

    quality = client.post(f"/api/projects/{project_id}/quality/script")
    assert quality.status_code == 200
    report = quality.json()
    assert report["heuristic_only"] is True
    assert report["project_id"] == project_id
    assert report["stage"] == "script"
    assert any(item["code"] == "placeholder" for item in report["findings"])
    narrative = report["narrative_review"]
    assert narrative["heuristic_only"] is True
    dimensions = {item["key"]: item for item in narrative["dimensions"]}
    assert set(dimensions) == {"hook", "goal", "obstacle", "stakes", "turn"}
    assert dimensions["hook"]["status"] == "signal_found"
    assert dimensions["goal"]["status"] == "signal_found"

    restored = client.post(
        f"/api/projects/{project_id}/content/script/revisions/{revision_id}/restore"
    )
    assert restored.status_code == 200
    assert restored.json()["workflow"]["script"]["content"] == original
    assert restored.json()["workflow"]["script"]["status"] == "ready"
    assert restored.json()["workflow"]["script"]["revisions"][-1]["content"] == current

    unknown = client.post(f"/api/projects/{project_id}/quality/not-a-stage")
    assert unknown.status_code == 404
    video_revisions = client.get(f"/api/projects/{project_id}/content/video/revisions")
    assert video_revisions.status_code == 200
    assert video_revisions.json()["revisions"] == []

    health = client.get("/health").json()
    status = client.get("/api/system/status").json()
    assert health["api_contract_version"] == API_CONTRACT_VERSION
    assert health["server_instance_id"] == status["server_instance_id"]
    assert status["api_contract_version"] == API_CONTRACT_VERSION
    assert status["server_started_at"]
    assert status["python_version"]
    assert {"series_transfer", "story_continuity_ledger", "quality_checks", "story_quality_review", "local_subtitles", "assembly_presets", "local_visual_assets", "scheduled_backups", "video_jobs", "content_revisions", "production_delivery", "production_reviews"}.issubset(set(status["features"]))
    assert isinstance(status["video_job_worker_running"], bool)
    jobs = client.get("/api/video-jobs")
    assert jobs.status_code == 200
    assert jobs.json()["jobs"] == []
    assert isinstance(jobs.json()["worker_running"], bool)

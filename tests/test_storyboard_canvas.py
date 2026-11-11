from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import API_CONTRACT_VERSION, create_app
from app.providers.catalog import ModelCatalog
from app.services.storyboard_canvas import StoryboardCanvasError, StoryboardCanvasService

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "models.json"
HEADERS = {"Origin": "http://testserver"}


def _client(tmp_path: Path) -> TestClient:
    catalog = ModelCatalog.load(CATALOG_PATH)
    settings = Settings(
        database_path=tmp_path / "canvas.db",
        model_catalog_path=CATALOG_PATH,
        model_catalog=catalog,
        api_keys={profile.api_key_env: "test-key" for profile in catalog.profiles.values()},
    )
    return TestClient(create_app(settings=settings), client=("127.0.0.1", 54321))


def _create_project(client: TestClient) -> dict:
    response = client.post("/api/projects", json={
        "track": "drama", "business": "失踪案背后的家族秘密。", "product": "雾港来信",
        "audience": "悬疑短剧观众", "platform": "竖屏短剧", "style": "克制写实",
        "duration_seconds": 60,
    })
    assert response.status_code == 201
    return response.json()


def _card(card_id: str = "card-1", shot_id: str = "S01", **overrides) -> dict:
    return {
        "id": card_id,
        "shot_id": shot_id,
        "title": "雨夜录音带",
        "duration_seconds": 6,
        "purpose": "建立失踪悬念",
        "visual_description": "妹妹在雨夜拾起一盘旧录音带。",
        "camera": "缓慢推近",
        "voiceover": "",
        "screen_text": "雾港，凌晨两点",
        "transition": "切至室内",
        "continuity": "录音带标签已经破损。",
        **overrides,
    }


def test_manual_canvas_saves_to_project_without_provider_calls_and_checks_revision(tmp_path):
    client = _client(tmp_path)
    project = _create_project(client)
    project_id = project["id"]
    initial = client.get(f"/api/projects/{project_id}").json()
    assert "storyboard_canvas" not in initial

    saved = client.put(
        f"/api/projects/{project_id}/storyboard-canvas",
        json={"expected_updated_at": initial["updated_at"], "cards": [_card()]},
        headers=HEADERS,
    )
    assert saved.status_code == 200
    board = saved.json()["storyboard_canvas"]
    assert board["version"] == 1
    assert board["cards"][0]["visual_description"] == "妹妹在雨夜拾起一盘旧录音带。"
    assert saved.json()["updated_at"] == board["updated_at"]

    reread = client.get(f"/api/projects/{project_id}").json()
    assert reread["storyboard_canvas"] == board
    stale = client.put(
        f"/api/projects/{project_id}/storyboard-canvas",
        json={"expected_updated_at": initial["updated_at"], "cards": [_card(title="陈旧版本")]},
        headers=HEADERS,
    )
    assert stale.status_code == 409
    assert client.get(f"/api/projects/{project_id}").json()["storyboard_canvas"] == board


def test_manual_canvas_rejects_invalid_cards_and_cross_origin_writes(tmp_path):
    client = _client(tmp_path)
    project = _create_project(client)
    url = f"/api/projects/{project['id']}/storyboard-canvas"
    base = {"expected_updated_at": project["updated_at"]}

    duplicate_shots = client.put(
        url,
        json={**base, "cards": [_card("first", "S01"), _card("second", "S01")]},
        headers=HEADERS,
    )
    assert duplicate_shots.status_code == 422

    too_short = client.put(
        url,
        json={**base, "cards": [_card(duration_seconds=3)]},
        headers=HEADERS,
    )
    assert too_short.status_code == 422

    cross_origin = client.put(
        url,
        json={**base, "cards": [_card()]},
        headers={"Origin": "https://attacker.example"},
    )
    assert cross_origin.status_code == 403
    assert "storyboard_canvas" not in client.get(url.removesuffix("/storyboard-canvas")).json()


def test_canvas_record_rejects_boolean_version():
    with pytest.raises(StoryboardCanvasError):
        StoryboardCanvasService.validate_record({"version": True, "cards": []})


def test_canvas_is_advertised_by_api_contract(tmp_path):
    status = _client(tmp_path).get("/api/system/status").json()
    assert status["api_contract_version"] == API_CONTRACT_VERSION == 7
    assert "manual_storyboard_canvas" in status["features"]

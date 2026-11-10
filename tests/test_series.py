from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.providers.catalog import ModelCatalog


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "models.json"


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        database_path=tmp_path / "series.db",
        model_catalog_path=CATALOG_PATH,
        model_catalog=ModelCatalog.load(CATALOG_PATH),
        api_keys={profile.api_key_env: "test-key" for profile in ModelCatalog.load(CATALOG_PATH).profiles.values()},
    )
    return TestClient(create_app(settings=settings), client=("127.0.0.1", 54321))


def series_payload(title: str = "雾港来信") -> dict:
    return {
        "title": title,
        "logline": "失踪的姐姐留下一卷旧录音带。",
        "visual_style": "低饱和冷色，雨夜港口。",
        "notes": "每集结尾留一个尚未解开的线索。",
    }


def episode_payload(series_id: str, *, episode_number: int | None = None, track: str = "drama") -> dict:
    payload = {
        "track": track,
        "business": "姐姐失踪后，妹妹回到港口寻找真相。",
        "product": "雾港来信",
        "audience": "喜欢悬疑短剧的观众",
        "platform": "竖屏短剧",
        "style": "冷峻、克制",
        "duration_seconds": 60,
        "series_id": series_id,
    }
    if episode_number is not None:
        payload["episode_number"] = episode_number
    return payload


def test_series_assets_and_episodes_are_persisted_and_archive_is_non_destructive(client):
    created = client.post("/api/series", json=series_payload())
    assert created.status_code == 201
    series_id = created.json()["id"]

    asset = client.post(
        f"/api/series/{series_id}/assets",
        json={
            "kind": "character",
            "name": "林澄",
            "description": "二十多岁，调查姐姐失踪的妹妹。",
            "visual_prompt": "短黑发，深色雨衣，写实电影光线。",
            "continuity_notes": "右手腕始终戴姐姐留下的旧表。",
        },
    )
    assert asset.status_code == 201
    asset_id = asset.json()["id"]

    first_episode = client.post("/api/projects", json=episode_payload(series_id))
    second_episode = client.post("/api/projects", json=episode_payload(series_id, episode_number=4))
    assert first_episode.status_code == 201
    assert second_episode.status_code == 201
    assert first_episode.json()["episode_number"] == 1
    assert second_episode.json()["episode_number"] == 4

    duplicate = client.post("/api/projects", json=episode_payload(series_id, episode_number=4))
    assert duplicate.status_code == 409
    assert "已存在" in duplicate.json()["detail"]

    detail = client.get(f"/api/series/{series_id}")
    assert detail.status_code == 200
    assert [episode["episode_number"] for episode in detail.json()["episodes"]] == [1, 4]
    assert detail.json()["assets"][0]["id"] == asset_id

    archived_asset = client.put(
        f"/api/series/{series_id}/assets/{asset_id}", json={"archived": True}
    )
    assert archived_asset.status_code == 200
    archived_series = client.put(f"/api/series/{series_id}", json={"archived": True})
    assert archived_series.status_code == 200
    rejected_episode = client.post("/api/projects", json=episode_payload(series_id))
    assert rejected_episode.status_code == 409

    # Archiving retains the series, asset, and existing episode records.
    archived_detail = client.get(f"/api/series/{series_id}").json()
    assert archived_detail["archived"] is True
    assert archived_detail["assets"][0]["archived"] is True
    assert len(archived_detail["episodes"]) == 2
    summary = client.get("/api/series").json()
    assert summary[0]["episode_count"] == 2
    assert summary[0]["asset_count"] == 0


def test_story_bible_and_traceable_continuity_ledger(client, monkeypatch):
    payload = series_payload()
    payload.update({"genre": "悬疑", "world_setting": "雾港依靠潮汐封锁旧仓库。", "central_theme": "记忆与证词"})
    series = client.post("/api/series", json=payload).json()
    series_id = series["id"]
    episode = client.post("/api/projects", json=episode_payload(series_id)).json()
    quoted_text = "林澄在海关仓库找到姐姐留下的录音带，随后把它带回住处。"

    rejected_unconfirmed = client.post(f"/api/series/{series_id}/continuity", json={
        "kind": "fact", "summary": "录音带已经找到。", "source_project_id": episode["id"],
        "source_stage": "script", "source_excerpt": quoted_text,
    })
    assert rejected_unconfirmed.status_code == 409

    class CapturingProvider:
        def generate(self, *, system, user):
            return "模型生成的初始草稿。"

    monkeypatch.setattr("app.services.workflow.create_text_provider", lambda profile, api_key: CapturingProvider())
    assert client.post(f"/api/projects/{episode['id']}/generate/script", json={}).status_code == 200
    assert client.put(f"/api/projects/{episode['id']}/content/script", json={"content": quoted_text}).status_code == 200
    assert client.post(f"/api/projects/{episode['id']}/approve/script").status_code == 200
    rejected_quote = client.post(f"/api/series/{series_id}/continuity", json={
        "kind": "fact", "summary": "录音带已经找到。", "source_project_id": episode["id"],
        "source_stage": "script", "source_excerpt": "模型生成的另一个句子。",
    })
    assert rejected_quote.status_code == 422

    created = client.post(f"/api/series/{series_id}/continuity", json={
        "kind": "event", "summary": "姐姐留下的录音带已经被林澄找到。", "status": "confirmed",
        "source_project_id": episode["id"], "source_stage": "script", "source_excerpt": quoted_text,
    })
    assert created.status_code == 201
    entry_id = created.json()["id"]
    assert created.json()["source"]["episode_number"] == 1
    assert created.json()["source"]["excerpt"] == quoted_text

    updated = client.put(f"/api/series/{series_id}/continuity/{entry_id}", json={
        "summary": "林澄已找到姐姐留下的录音带。", "status": "open",
    })
    assert updated.status_code == 200
    assert updated.json()["versions"][-1]["summary"] == "姐姐留下的录音带已经被林澄找到。"
    detail = client.get(f"/api/series/{series_id}").json()
    assert detail["genre"] == "悬疑"
    assert detail["world_setting"] == "雾港依靠潮汐封锁旧仓库。"
    assert detail["central_theme"] == "记忆与证词"
    assert detail["continuity_entries"][0]["source"]["project_id"] == episode["id"]


def test_only_drama_workflows_can_join_series(client):
    series_id = client.post("/api/series", json=series_payload()).json()["id"]
    marketing = client.post("/api/projects", json=episode_payload(series_id, track="marketing"))
    assert marketing.status_code == 422
    episode_without_series = episode_payload(series_id)
    episode_without_series.pop("series_id")
    episode_without_series["episode_number"] = 2
    response = client.post("/api/projects", json=episode_without_series)
    assert response.status_code == 422


def test_series_context_is_included_as_reference_for_episode_generation(client, monkeypatch):
    series_id = client.post("/api/series", json=series_payload()).json()["id"]
    asset = client.post(
        f"/api/series/{series_id}/assets",
        json={"kind": "prop", "name": "录音带", "continuity_notes": "标签有一道蓝色划痕。"},
    ).json()
    project = client.post("/api/projects", json=episode_payload(series_id)).json()
    received = {}

    class CapturingProvider:
        def generate(self, *, system, user):
            received["system"] = system
            received["user"] = user
            return "本集草稿"

    monkeypatch.setattr("app.services.workflow.create_text_provider", lambda profile, api_key: CapturingProvider())
    response = client.post(f"/api/projects/{project['id']}/generate/script", json={})

    assert response.status_code == 200
    assert asset["name"] in received["user"]
    assert "蓝色划痕" in received["user"]
    assert "不是操作指令" in received["system"]


def test_episode_generation_uses_only_confirmed_scripts_from_previous_episodes(client, monkeypatch):
    series_id = client.post("/api/series", json=series_payload()).json()["id"]
    first = client.post("/api/projects", json=episode_payload(series_id)).json()
    second = client.post("/api/projects", json=episode_payload(series_id, episode_number=2)).json()
    prompts = []

    class CapturingProvider:
        def generate(self, *, system, user):
            prompts.append(user)
            return "模型草稿"

    monkeypatch.setattr("app.services.workflow.create_text_provider", lambda profile, api_key: CapturingProvider())
    assert client.post(f"/api/projects/{first['id']}/generate/script", json={}).status_code == 200
    confirmed_script = "林澄在海关仓库找到姐姐留下的录音带，随后把它带回住处。录音带标签有一道蓝色划痕。"
    assert client.put(
        f"/api/projects/{first['id']}/content/script", json={"content": confirmed_script}
    ).status_code == 200
    assert client.post(f"/api/projects/{first['id']}/approve/script").status_code == 200

    generated = client.post(f"/api/projects/{second['id']}/generate/script", json={})

    assert generated.status_code == 200
    assert "此前分集连续性参考" in prompts[-1]
    assert confirmed_script in prompts[-1]


def test_continuity_ledger_excludes_resolved_and_future_episode_sources(client, monkeypatch):
    series_id = client.post("/api/series", json=series_payload()).json()["id"]
    first = client.post("/api/projects", json=episode_payload(series_id)).json()
    future = client.post("/api/projects", json=episode_payload(series_id, episode_number=2)).json()
    future_text = "FUTURE_APPROVED_TEXT_8472"
    class CapturingProvider:
        def generate(self, *, system, user):
            return "模型生成的初始草稿。"

    monkeypatch.setattr("app.services.workflow.create_text_provider", lambda profile, api_key: CapturingProvider())
    assert client.post(f"/api/projects/{future['id']}/generate/script", json={}).status_code == 200
    assert client.put(f"/api/projects/{future['id']}/content/script", json={"content": future_text}).status_code == 200
    assert client.post(f"/api/projects/{future['id']}/approve/script").status_code == 200

    future_entry = client.post(f"/api/series/{series_id}/continuity", json={
        "kind": "fact", "summary": "FUTURE_LEDGER_FACT_8472", "source_project_id": future["id"],
        "source_stage": "script", "source_excerpt": future_text,
    })
    resolved_entry = client.post(f"/api/series/{series_id}/continuity", json={
        "kind": "clue", "summary": "RESOLVED_LEDGER_CLUE_8472", "status": "resolved",
    })
    current_entry = client.post(f"/api/series/{series_id}/continuity", json={
        "kind": "open_thread", "summary": "MANUAL_OPEN_THREAD_8472", "status": "open",
    })
    assert future_entry.status_code == resolved_entry.status_code == current_entry.status_code == 201
    prompts = []

    class CapturingProvider:
        def generate(self, *, system, user):
            prompts.append(user)
            return "第一集草稿"

    monkeypatch.setattr("app.services.workflow.create_text_provider", lambda profile, api_key: CapturingProvider())
    response = client.post(f"/api/projects/{first['id']}/generate/script", json={})

    assert response.status_code == 200
    assert "MANUAL_OPEN_THREAD_8472" in prompts[-1]
    assert "FUTURE_LEDGER_FACT_8472" not in prompts[-1]
    assert "FUTURE_APPROVED_TEXT_8472" not in prompts[-1]
    assert "RESOLVED_LEDGER_CLUE_8472" not in prompts[-1]


def test_unapproved_previous_episode_script_is_not_treated_as_story_fact(client, monkeypatch):
    series_id = client.post("/api/series", json=series_payload()).json()["id"]
    first = client.post("/api/projects", json=episode_payload(series_id)).json()
    second = client.post("/api/projects", json=episode_payload(series_id, episode_number=2)).json()
    prompts = []
    unapproved_script = "未确认剧情不得泄漏到后续：线索-7391"

    class CapturingProvider:
        def generate(self, *, system, user):
            prompts.append(user)
            return unapproved_script

    monkeypatch.setattr("app.services.workflow.create_text_provider", lambda profile, api_key: CapturingProvider())
    assert client.post(f"/api/projects/{first['id']}/generate/script", json={}).status_code == 200

    generated = client.post(f"/api/projects/{second['id']}/generate/script", json={})

    assert generated.status_code == 200
    assert unapproved_script not in prompts[-1]
    assert "脚本尚未确认，不提供草稿内容" in prompts[-1]

from __future__ import annotations

import json
import time
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.providers.catalog import ModelCatalog, ModelProfile
from app.providers.media import (
    AgnesVideoProvider,
    AssetStore,
    OpenAICompatibleImageProvider,
    VideoSubmissionUncertainError,
)
from app.providers.text import ChatResult, OpenAICompatibleTextProvider
from app.services.chat import ChatService

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "models.json"


def make_settings(tmp_path: Path, *, api_keys: dict[str, str] | None = None) -> Settings:
    test_keys = {
        "AGNES_API_KEY": "test-key",
        "ANTHROPIC_API_KEY": "test-key",
        "OPENAI_API_KEY": "test-key",
    }
    return Settings(
        database_path=tmp_path / "test.db",
        model_catalog_path=CATALOG_PATH,
        model_catalog=ModelCatalog.load(CATALOG_PATH),
        api_keys=test_keys if api_keys is None else api_keys,
    )


def make_client(tmp_path, *, api_keys: dict[str, str] | None = None):
    return TestClient(
        create_app(settings=make_settings(tmp_path, api_keys=api_keys)),
        client=("127.0.0.1", 54321),
    )


def editable_model_catalog(config: dict) -> dict:
    fields = ("id", "name", "modality", "adapter", "model_id", "base_url", "params")
    return {
        "defaults": dict(config["defaults"]),
        "models": [{key: deepcopy(profile[key]) for key in fields} for profile in config["model_profiles"]],
    }


def app_with_temporary_catalog(tmp_path: Path) -> tuple[TestClient, Path]:
    catalog_path = tmp_path / "models.json"
    catalog_path.write_text(CATALOG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    settings = replace(make_settings(tmp_path), model_catalog_path=catalog_path)
    return TestClient(create_app(settings=settings), client=("127.0.0.1", 54321)), catalog_path


@pytest.fixture(autouse=True)
def isolate_workflow_text_provider(monkeypatch):
    class FakeTextProvider:
        def generate(self, *, system, user):
            if "当前步骤：scenes" in system:
                shots = [{
                    "shot_id": f"S{index:02d}", "duration_seconds": 10, "aspect_ratio": "9:16",
                    "purpose": f"展示产品 {index}", "visual_description": "咖啡杯置于桌面",
                    "voiceover": "", "screen_text": "手作咖啡", "transition": "淡入",
                    "continuity": "保持杯身颜色一致", "image_prompt": "A coffee cup on a warm wooden table",
                    "video_prompt": "Slow push in toward the coffee cup", "negative_prompt": "distorted logo",
                } for index in range(1, 4)]
                return json.dumps({
                    "shots": shots,
                    "edit_plan": {"sequence": ["S01", "S02", "S03"], "pacing": "舒缓", "transitions": "淡入"},
                }, ensure_ascii=False)
            return "测试用的阶段草稿"

        def chat(self, *, messages, tools=None):
            return ChatResult(text="测试助手回复")

    monkeypatch.setattr(
        "app.services.workflow.create_text_provider",
        lambda profile, api_key: FakeTextProvider(),
    )


def new_project(client, model=None):
    return client.post(
        "/api/projects",
        json={
            "track": "marketing",
            "business": "帮助小型咖啡馆经营会员",
            "product": "咖啡馆会员工具",
            "audience": "希望提高复购的小型咖啡馆经营者",
            "platform": "抖音",
            "style": "真实、温暖",
            "duration_seconds": 30,
            "model": model or "agnes-text",
        },
    )


def test_home_page_avoids_stale_markup_and_loads_versioned_assets(tmp_path):
    response = make_client(tmp_path).get("/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate"
    assert 'id="open-records"' in response.text
    assert 'id="confirm-dialog"' in response.text
    assert "/static/app.js?v=11" in response.text
    assert "/static/style.css?v=10" in response.text


def test_create_generate_edit_and_approve_advances_one_stage(tmp_path):
    client = make_client(tmp_path)
    created = new_project(client)
    assert created.status_code == 201
    project = created.json()
    assert project["mode"] == "workflow"
    assert project["active_stage"] == "script"

    generated = client.post(
        f"/api/projects/{project['id']}/generate/script",
        json={"model": "claude-sonnet"},
    )
    assert generated.status_code == 200
    assert generated.json()["workflow"]["script"]["status"] == "ready"
    assert generated.json()["model"] == "claude-sonnet"

    edited = client.put(
        f"/api/projects/{project['id']}/content/script",
        json={"content": "已人工修改并审阅的脚本"},
    )
    assert edited.status_code == 200
    assert edited.json()["workflow"]["script"]["content"] == "已人工修改并审阅的脚本"

    approved = client.post(f"/api/projects/{project['id']}/approve/script")
    assert approved.status_code == 200
    assert approved.json()["workflow"]["script"]["status"] == "approved"
    assert approved.json()["active_stage"] == "prompt"


def test_project_track_persists_and_invalid_tracks_are_rejected(tmp_path):
    client = make_client(tmp_path)
    payload = {
        "track": "drama",
        "business": "被忽视的长女回乡争取旧宅产权",
        "product": "旧宅回声 EP001",
        "audience": "都市家庭悬疑观众",
        "platform": "竖屏短剧",
        "style": "真人短剧，克制写实",
        "duration_seconds": 60,
        "model": "agnes-text",
    }

    created = client.post("/api/projects", json=payload)

    assert created.status_code == 201
    assert created.json()["track"] == "drama"
    assert created.json()["brief"]["product"] == "旧宅回声 EP001"
    payload["track"] = "unknown"
    assert client.post("/api/projects", json=payload).status_code == 422


def test_drama_track_uses_only_drama_stage_guidance(tmp_path, monkeypatch):
    received = {}

    class CaptureProvider:
        def generate(self, *, system, user):
            received["system"] = system
            received["user"] = user
            return "测试短剧脚本草稿"

    monkeypatch.setattr(
        "app.services.workflow.create_text_provider",
        lambda profile, api_key: CaptureProvider(),
    )
    client = make_client(tmp_path)
    project = client.post("/api/projects", json={
        "track": "drama",
        "business": "长女回乡争取旧宅",
        "product": "旧宅回声",
        "audience": "都市家庭悬疑观众",
        "platform": "竖屏短剧",
        "style": "真人短剧，克制写实",
        "duration_seconds": 60,
        "model": "agnes-text",
    }).json()

    generated = client.post(f"/api/projects/{project['id']}/generate/script", json={})

    assert generated.status_code == 200
    assert generated.json()["workflow"]["script"]["content"] == "测试短剧脚本草稿"
    assert "短剧 / 漫剧" in received["system"]
    assert "短剧 / 漫剧脚本" in received["system"]
    assert "竖屏短视频脚本" not in received["system"]
    assert "核心卖点" not in received["system"]
    assert "明确 CTA" not in received["system"]
    assert "故事背景 / 核心设定" in received["user"]
    assert "短剧 / 漫剧创作 brief" in received["user"]


def test_drama_chat_context_uses_story_labels_and_no_marketing_assumptions():
    context = ChatService._context({
        "mode": "workflow",
        "track": "drama",
        "brief": {
            "business": "长女回乡争取旧宅",
            "product": "旧宅回声",
            "audience": "都市悬疑观众",
            "platform": "竖屏短剧",
            "duration_seconds": 60,
            "style": "真人写实",
        },
        "workflow": {},
    })

    assert "这是短剧 / 漫剧创作项目" in context
    assert "故事背景 / 核心设定" in context
    assert "长女回乡争取旧宅" in context
    assert "品牌营销视频项目" not in context
    assert "不强加品牌卖点" in context


def test_cannot_skip_unconfirmed_stage_or_approve_empty_stage(tmp_path):
    client = make_client(tmp_path)
    project = new_project(client).json()
    project_id = project["id"]

    skipped = client.post(f"/api/projects/{project_id}/generate/storyboard", json={})
    assert skipped.status_code == 409
    empty_approval = client.post(f"/api/projects/{project_id}/approve/script")
    assert empty_approval.status_code == 409

    client.post(f"/api/projects/{project_id}/generate/script", json={})
    still_locked = client.post(f"/api/projects/{project_id}/generate/prompt", json={})
    assert still_locked.status_code == 409


def test_scene_tasks_are_validated_before_save_and_approval(tmp_path):
    client = make_client(tmp_path)
    project = new_project(client).json()
    project_id = project["id"]
    for stage in ("script", "prompt", "storyboard"):
        assert client.post(f"/api/projects/{project_id}/generate/{stage}", json={}).status_code == 200
        assert client.post(f"/api/projects/{project_id}/approve/{stage}").status_code == 200

    generated = client.post(f"/api/projects/{project_id}/generate/scenes", json={})
    assert generated.status_code == 200
    plan = json.loads(generated.json()["workflow"]["scenes"]["content"])
    assert len(plan["shots"]) == 3
    assert all(4 <= shot["duration_seconds"] <= 12 for shot in plan["shots"])

    plan["shots"][0]["duration_seconds"] = 13
    invalid_edit = client.put(
        f"/api/projects/{project_id}/content/scenes",
        json={"content": json.dumps(plan, ensure_ascii=False)},
    )
    assert invalid_edit.status_code == 422
    assert invalid_edit.json()["detail"].find("4–12 秒") >= 0

    approved = client.post(f"/api/projects/{project_id}/approve/scenes")
    assert approved.status_code == 200
    assert approved.json()["active_stage"] == "video"
    shots = approved.json()["workflow"]["video"]["shots"]
    assert len(shots) == 3
    assert all(shot["image"]["status"] == "pending" and shot["video"]["status"] == "pending" for shot in shots)


def test_approval_rejects_stage_content_with_explicit_blockers(tmp_path):
    client = make_client(tmp_path)
    project_id = new_project(client).json()["id"]
    assert client.post(f"/api/projects/{project_id}/generate/script", json={}).status_code == 200

    blocked_content = "根据 brief，关键信息缺失或模糊，无法直接撰写有效脚本。请补充产品与受众信息后再继续。"
    edited = client.put(
        f"/api/projects/{project_id}/content/script",
        json={"content": blocked_content},
    )
    assert edited.status_code == 200

    response = client.post(f"/api/projects/{project_id}/approve/script")

    assert response.status_code == 409
    assert "不能确认" in response.json()["detail"]
    project = client.get(f"/api/projects/{project_id}").json()
    assert project["active_stage"] == "script"
    assert project["workflow"]["script"]["status"] == "ready"


def test_generation_stops_before_model_call_for_blocked_upstream_content(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    project = new_project(client).json()
    workflow = client.app.state.workflow
    stored = workflow.get_project(project["id"])
    blocked_content = "当前关键信息缺失或模糊，必须补充后才能生成有效内容。"
    for stage in ("script", "prompt", "storyboard"):
        stored["workflow"][stage].update({"status": "approved", "content": blocked_content})
    stored["active_stage"] = "scenes"
    workflow.store.save(stored)

    provider_created = []
    monkeypatch.setattr(
        "app.services.workflow.create_text_provider",
        lambda profile, api_key: provider_created.append(profile.id),
    )
    response = client.post(f"/api/projects/{project['id']}/generate/scenes", json={})

    assert response.status_code == 409
    assert "脚本、视觉设定、分镜" in response.json()["detail"]
    assert "本次未调用模型" in response.json()["detail"]
    assert provider_created == []


def test_empty_scene_plan_error_suggests_reviewing_blocked_upstream_content(tmp_path, monkeypatch):
    class EmptySceneProvider:
        def generate(self, *, system, user):
            return json.dumps({"shots": [], "edit_plan": {}})

    client = make_client(tmp_path)
    project_id = new_project(client).json()["id"]
    for stage in ("script", "prompt", "storyboard"):
        assert client.post(f"/api/projects/{project_id}/generate/{stage}", json={}).status_code == 200
        assert client.post(f"/api/projects/{project_id}/approve/{stage}").status_code == 200

    monkeypatch.setattr(
        "app.services.workflow.create_text_provider",
        lambda profile, api_key: EmptySceneProvider(),
    )
    response = client.post(f"/api/projects/{project_id}/generate/scenes", json={})

    assert response.status_code == 422
    assert "shots 为空" in response.json()["detail"]
    assert "回退补齐信息" in response.json()["detail"]
    project = client.get(f"/api/projects/{project_id}").json()
    assert project["active_stage"] == "scenes"
    assert project["workflow"]["scenes"]["status"] == "pending"
    assert project["workflow"]["scenes"]["content"] is None


def test_rollback_reopens_confirmed_stage_and_preserves_stale_asset_history(tmp_path):
    class FakeImageProvider:
        calls = 0

        def generate_image(self, *, prompt, aspect_ratio):
            self.calls += 1
            return {"asset_url": f"https://media.example/frame-{self.calls}.png"}

    class FakeVideoProvider:
        ready = True

        def __init__(self):
            self.calls = 0
            self.received_projects = []

        def generate_video(self, *, project):
            self.calls += 1
            self.received_projects.append(project)
            return {"asset_url": f"https://media.example/clip-{self.calls}.mp4"}

    image_provider = FakeImageProvider()
    video_provider = FakeVideoProvider()
    client = TestClient(
        create_app(
            settings=make_settings(tmp_path),
            image_provider=image_provider,
            video_provider=video_provider,
        ),
        client=("127.0.0.1", 54321),
    )
    project_id = new_project(client).json()["id"]
    for stage in ("script", "prompt", "storyboard", "scenes"):
        assert client.post(f"/api/projects/{project_id}/generate/{stage}", json={}).status_code == 200
        assert client.post(f"/api/projects/{project_id}/approve/{stage}").status_code == 200

    assert client.post(f"/api/projects/{project_id}/shots/S01/image").status_code == 200
    assert client.post(f"/api/projects/{project_id}/shots/S01/video").status_code == 200
    first = client.get(f"/api/projects/{project_id}").json()
    old_scene_content = first["workflow"]["scenes"]["content"]
    assert first["workflow"]["video"]["shots"][0]["video"]["asset_url"].endswith("clip-1.mp4")

    rolled_back = client.post(f"/api/projects/{project_id}/rollback/scenes")
    assert rolled_back.status_code == 200
    assert rolled_back.json()["active_stage"] == "scenes"
    assert rolled_back.json()["workflow"]["scenes"]["status"] == "ready"
    stale_shot = rolled_back.json()["workflow"]["video"]["shots"][0]
    assert stale_shot["image"]["status"] == "stale"
    assert stale_shot["video"]["status"] == "stale"
    assert stale_shot["video"]["asset_url"].endswith("clip-1.mp4")

    scene_plan = json.loads(old_scene_content)
    scene_plan["shots"][0]["video_prompt"] = "A newly revised camera move"
    edited = client.put(
        f"/api/projects/{project_id}/content/scenes",
        json={"content": json.dumps(scene_plan, ensure_ascii=False)},
    )
    assert edited.status_code == 200
    assert edited.json()["workflow"]["scenes"]["status"] == "ready"
    assert edited.json()["workflow"]["scenes"]["revisions"][0]["content"] == old_scene_content

    reapproved = client.post(f"/api/projects/{project_id}/approve/scenes")
    assert reapproved.status_code == 200
    revised_shot = reapproved.json()["workflow"]["video"]["shots"][0]
    assert revised_shot["video"]["status"] == "stale"
    assert revised_shot["video"]["asset_url"].endswith("clip-1.mp4")
    regenerated = client.post(f"/api/projects/{project_id}/shots/S01/video")
    assert regenerated.status_code == 200
    revised_video = regenerated.json()["workflow"]["video"]["shots"][0]["video"]
    assert revised_video["asset_url"].endswith("clip-2.mp4")
    assert len(revised_video["versions"]) == 2
    assert video_provider.received_projects[-1]["reference_images"] == []

    script_rollback = client.post(f"/api/projects/{project_id}/rollback/script")
    assert script_rollback.status_code == 200
    assert script_rollback.json()["active_stage"] == "script"
    assert script_rollback.json()["workflow"]["prompt"]["status"] == "stale"
    edited_script = client.put(
        f"/api/projects/{project_id}/content/script",
        json={"content": "回退后修改的脚本"},
    )
    assert edited_script.status_code == 200
    assert edited_script.json()["workflow"]["script"]["revisions"][-1]["content"] == "测试用的阶段草稿"
    assert client.post(f"/api/projects/{project_id}/approve/script").status_code == 200
    assert client.post(f"/api/projects/{project_id}/approve/prompt").status_code == 409
    regenerated_prompt = client.post(f"/api/projects/{project_id}/generate/prompt", json={})
    assert regenerated_prompt.status_code == 200
    assert regenerated_prompt.json()["workflow"]["prompt"]["status"] == "ready"


def test_video_provider_adapter_can_be_injected(tmp_path):
    class FakeVideoProvider:
        ready = True

        def generate_video(self, *, project):
            return {"asset_url": "https://media.example/video.mp4", "summary": "测试成片"}

    client = TestClient(
        create_app(settings=make_settings(tmp_path), video_provider=FakeVideoProvider()),
        client=("127.0.0.1", 54321),
    )
    project = new_project(client).json()

    for stage in ("script", "prompt", "storyboard", "scenes"):
        assert client.post(f"/api/projects/{project['id']}/generate/{stage}", json={}).status_code == 200
        assert client.post(f"/api/projects/{project['id']}/approve/{stage}").status_code == 200

    legacy_generation = client.post(f"/api/projects/{project['id']}/generate/video", json={})
    assert legacy_generation.status_code == 409
    result = client.post(f"/api/projects/{project['id']}/shots/S01/video")
    assert result.status_code == 200
    assert result.json()["workflow"]["video"]["shots"][0]["video"]["asset_url"] == "https://media.example/video.mp4"
    assert client.get("/api/config").json()["media_provider_ready"] is True


def test_shot_assets_support_failure_retry_versions_and_per_shot_video_context(tmp_path):
    class FlakyImageProvider:
        calls = 0

        def generate_image(self, *, prompt, aspect_ratio):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary image failure")
            return {"asset_url": f"https://media.example/frame-{self.calls}.png"}

    class FakeVideoProvider:
        ready = True
        received_project = None

        def generate_video(self, *, project):
            self.received_project = project
            return {"asset_url": "https://media.example/shot.mp4", "task_id": "task-123", "cost": 0.04}

    image_provider = FlakyImageProvider()
    video_provider = FakeVideoProvider()
    client = TestClient(
        create_app(
            settings=make_settings(tmp_path),
            image_provider=image_provider,
            video_provider=video_provider,
        ),
        client=("127.0.0.1", 54321),
    )
    project = new_project(client).json()
    for stage in ("script", "prompt", "storyboard", "scenes"):
        assert client.post(f"/api/projects/{project['id']}/generate/{stage}", json={}).status_code == 200
        assert client.post(f"/api/projects/{project['id']}/approve/{stage}").status_code == 200

    failed = client.post(f"/api/projects/{project['id']}/shots/S01/image")
    assert failed.status_code == 502
    shot = client.get(f"/api/projects/{project['id']}").json()["workflow"]["video"]["shots"][0]
    assert shot["image"]["status"] == "failed"
    assert "temporary image failure" in shot["image"]["error"]

    first_retry = client.post(f"/api/projects/{project['id']}/shots/S01/image")
    assert first_retry.status_code == 200
    second_version = client.post(f"/api/projects/{project['id']}/shots/S01/image")
    assert second_version.status_code == 200
    shot = second_version.json()["workflow"]["video"]["shots"][0]
    assert [version["asset_url"] for version in shot["image"]["versions"]] == [
        "https://media.example/frame-2.png", "https://media.example/frame-3.png"
    ]

    generated = client.post(f"/api/projects/{project['id']}/shots/S01/video")
    assert generated.status_code == 200
    assert video_provider.received_project["video_prompt"] == "Slow push in toward the coffee cup"
    assert video_provider.received_project["brief"]["duration_seconds"] == 10
    assert video_provider.received_project["reference_images"] == ["https://media.example/frame-3.png"]
    video_asset = generated.json()["workflow"]["video"]["shots"][0]["video"]
    assert video_asset["versions"][0]["task_id"] == "task-123"
    assert video_asset["versions"][0]["cost"] == 0.04


def test_production_records_preserve_script_storyboard_and_final_video(tmp_path):
    class FakeVideoProvider:
        ready = True

        def generate_video(self, *, project):
            return {"asset_url": "https://media.example/final.mp4", "summary": "测试成片"}

    client = TestClient(
        create_app(settings=make_settings(tmp_path), video_provider=FakeVideoProvider()),
        client=("127.0.0.1", 54321),
    )
    project = new_project(client).json()
    project_id = project["id"]

    for stage in ("script", "prompt", "storyboard", "scenes"):
        assert client.post(f"/api/projects/{project_id}/generate/{stage}", json={}).status_code == 200
        assert client.post(f"/api/projects/{project_id}/approve/{stage}").status_code == 200
    generated_video = client.post(f"/api/projects/{project_id}/shots/S01/video")
    assert generated_video.status_code == 200

    reopened = TestClient(create_app(settings=make_settings(tmp_path)), client=("127.0.0.1", 54321))
    records = reopened.get("/api/projects").json()
    record = next(item for item in records if item["id"] == project_id)
    assert record["mode"] == "workflow"
    assert record["workflow"]["script"]["content"] == "测试用的阶段草稿"
    assert record["workflow"]["script"]["status"] == "approved"
    assert record["workflow"]["storyboard"]["content"] == "测试用的阶段草稿"
    assert record["workflow"]["video"]["shots"][0]["video"]["asset_url"] == "https://media.example/final.mp4"
    assert record["workflow"]["video"]["shots"][0]["video"]["versions"][0]["task_id"] is None


def test_models_skills_and_chat_are_discoverable(tmp_path):
    client = make_client(tmp_path, api_keys={})
    homepage = client.get("/")
    assert homepage.status_code == 200
    assert "新对话" in homepage.text
    assert 'id="new-workflow"' in homepage.text
    assert 'id="view-toggle"' in homepage.text
    assert 'id="chat-dialog"' not in homepage.text
    assert client.get("/static/app.js").status_code == 200

    config = client.get("/api/config").json()
    assert config["provider_mode"] == "unconfigured"
    assert "agnes-text" in config["models"]
    assert config["defaults"]["image"] == "agnes-image"
    assert config["media_provider_ready"] is False
    assert all("api_key" not in profile for profile in config["model_profiles"])

    skills = client.get("/api/skills").json()
    names = {skill["name"] for skill in skills}
    assert {
        "brand-brief", "short-form-video", "storyboard", "scene-image-prompts",
        "model-first-video", "video", "drama-script", "drama-visual",
        "drama-storyboard", "drama-scene-prompts",
    } <= names

    project = new_project(client).json()
    history = client.get(f"/api/projects/{project['id']}/chat")
    assert history.status_code == 200


def test_plain_chat_session_needs_no_brief_and_titles_from_first_message(tmp_path, monkeypatch):
    from app.providers.text import ChatResult

    captured = {}

    class FakeTextProvider:
        def chat(self, *, messages, tools=None):
            captured["context"] = messages[0]["content"]
            return ChatResult(text="普通对话回复")

    monkeypatch.setattr(
        "app.services.chat.create_text_provider",
        lambda profile, api_key: FakeTextProvider(),
    )
    client = make_client(tmp_path)
    created = client.post("/api/chat/sessions", json={})
    assert created.status_code == 201
    project = created.json()
    assert project["mode"] == "chat"
    assert project["title"] == "新对话"
    assert project["brief"] == {}
    assert client.get(f"/api/projects/{project['id']}/chat").json() == []

    message = "帮我写一个咖啡店短视频开场"
    reply = client.post(
        f"/api/projects/{project['id']}/chat",
        json={"message": message},
    )
    assert reply.status_code == 200
    assert reply.json()[-1]["content"] == "普通对话回复"
    updated = client.get(f"/api/projects/{project['id']}").json()
    assert updated["title"] == message
    assert "普通对话" in captured["context"]
    assert "项目 brief：" not in captured["context"]


def test_unconfigured_real_provider_fails_without_faking_content(tmp_path):
    client = make_client(tmp_path, api_keys={})
    project = new_project(client).json()
    result = client.post(f"/api/projects/{project['id']}/generate/script", json={})
    assert result.status_code == 503
    assert "API Key" in result.json()["detail"]


def test_openai_compatible_text_adapter_calls_configured_endpoint():
    captured = {}

    def handler(request: httpx.Request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "文本已生成"}}]})

    catalog = ModelCatalog.load(CATALOG_PATH)
    profile = catalog.resolve("agnes-text", "text")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleTextProvider(profile, "test-key", client=client)
    result = provider.generate(system="系统约束", user="写一句开场")

    assert result == "文本已生成"
    assert captured["url"] == "https://apihub.agnes-ai.com/v1/chat/completions"
    assert captured["body"]["model"] == "agnes-2.5-flash"
    assert captured["body"]["messages"][1]["content"] == "写一句开场"


def test_agnes_image_provider_posts_generation_and_returns_url(tmp_path):
    captured = {}

    def handler(request: httpx.Request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"url": "https://media.example/image.png"}]})

    catalog = ModelCatalog.load(CATALOG_PATH)
    profile = catalog.resolve("agnes-image", "image")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleImageProvider(
        profile,
        "test-key",
        AssetStore(tmp_path / "media"),
        client=client,
    )
    result = provider.generate_image(prompt="A coffee product photo", aspect_ratio="9:16")

    assert result["asset_url"] == "https://media.example/image.png"
    assert captured["url"] == "https://apihub.agnes-ai.com/v1/images/generations"
    assert captured["body"]["model"] == "agnes-image-2.5-flash"
    assert captured["body"]["ratio"] == "9:16"
    assert captured["body"]["extra_body"]["response_format"] == "url"


def test_agnes_video_queue_full_is_actionable_and_does_not_poll(tmp_path):
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(503, json={
            "code": "video_queue_full",
            "message": "video queue is full, please retry later (request id: req-test-123)",
            "data": None,
        })

    profile = ModelCatalog.load(CATALOG_PATH).resolve("agnes-video", "video")
    provider = AgnesVideoProvider(
        profile,
        "test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client = TestClient(
        create_app(settings=make_settings(tmp_path), video_provider=provider),
        client=("127.0.0.1", 54321),
    )
    project = new_project(client).json()
    for stage in ("script", "prompt", "storyboard", "scenes"):
        assert client.post(f"/api/projects/{project['id']}/generate/{stage}", json={}).status_code == 200
        assert client.post(f"/api/projects/{project['id']}/approve/{stage}").status_code == 200

    response = client.post(f"/api/projects/{project['id']}/shots/S01/video")
    assert response.status_code == 202
    assert response.json()["workflow"]["video"]["shots"][0]["video"]["status"] == "queued"
    assert requests == []

    worker = client.app.state.video_job_worker
    assert worker.process_once() is True
    shot = client.get(f"/api/projects/{project['id']}").json()["workflow"]["video"]["shots"][0]
    assert shot["video"]["status"] == "failed"
    assert "Agnes 视频生成队列已满" in shot["video"]["error"]
    assert "请稍后手动重试" in shot["video"]["error"]
    assert "req-test-123" in shot["video"]["error"]
    assert len(requests) == 1
    assert requests[0].method == "POST"


def test_durable_video_jobs_resume_polling_and_guard_uncertain_submission(tmp_path):
    class AsyncVideoProvider:
        ready = True

        def __init__(self, poll_result=None, submit_error=None):
            self.poll_result = poll_result
            self.submit_error = submit_error
            self.submissions = 0
            self.polls = []

        def submit_video(self, *, project):
            self.submissions += 1
            if self.submit_error:
                raise self.submit_error
            return {"provider_task_id": f"provider-video-{self.submissions}", "task_id": f"task-{self.submissions}"}

        def poll_video(self, *, provider_task_id):
            self.polls.append(provider_task_id)
            return self.poll_result or {"status": "queued", "provider_status": "queued"}

        def generate_video(self, *, project):
            raise AssertionError("durable providers must use submit_video/poll_video")

    first_provider = AsyncVideoProvider()
    first_client = TestClient(
        create_app(settings=make_settings(tmp_path), video_provider=first_provider),
        client=("127.0.0.1", 54321),
    )
    project = new_project(first_client).json()
    for stage in ("script", "prompt", "storyboard", "scenes"):
        assert first_client.post(f"/api/projects/{project['id']}/generate/{stage}", json={}).status_code == 200
        assert first_client.post(f"/api/projects/{project['id']}/approve/{stage}").status_code == 200

    queued = first_client.post(f"/api/projects/{project['id']}/shots/S01/video")
    assert queued.status_code == 202
    job_id = queued.json()["workflow"]["video"]["shots"][0]["video"]["job_id"]
    assert first_provider.submissions == 0
    assert first_client.app.state.video_job_worker.process_once() is True
    job = first_client.app.state.workflow.store.get_video_job(job_id)
    assert job["status"] == "polling"
    assert job["provider_task_id"] == "provider-video-1"
    assert job["task_id"] == "task-1"

    resumed_provider = AsyncVideoProvider({
        "status": "completed",
        "provider_status": "completed",
        "asset_url": "https://media.example/resumed.mp4",
    })
    resumed_client = TestClient(
        create_app(settings=make_settings(tmp_path), video_provider=resumed_provider),
        client=("127.0.0.1", 54321),
    )
    resumed_store = resumed_client.app.state.workflow.store
    resumed_store.update_video_job(
        job_id,
        {"next_poll_at": datetime.now(timezone.utc).isoformat()},
    )
    with resumed_client:
        deadline = time.monotonic() + 5
        resumed_project = resumed_client.get(f"/api/projects/{project['id']}").json()
        while time.monotonic() < deadline:
            video_asset = resumed_project["workflow"]["video"]["shots"][0]["video"]
            if video_asset["status"] == "ready":
                break
            time.sleep(0.05)
            resumed_project = resumed_client.get(f"/api/projects/{project['id']}").json()
        video_asset = resumed_project["workflow"]["video"]["shots"][0]["video"]
        assert video_asset["status"] == "ready"
        assert video_asset["asset_url"] == "https://media.example/resumed.mp4"
    assert resumed_provider.polls == ["provider-video-1"]
    assert resumed_store.get_video_job(job_id)["status"] == "completed"

    stale_queued = first_client.post(f"/api/projects/{project['id']}/shots/S03/video")
    assert stale_queued.status_code == 202
    stale_store = first_client.app.state.workflow.store
    stale_job_id = stale_queued.json()["workflow"]["video"]["shots"][2]["video"]["job_id"]
    assert first_client.app.state.video_job_worker.process_once() is True
    changed_project = stale_store.get(project["id"])
    changed_project["workflow"]["video"]["shots"][2]["video_prompt"] += " Camera moves left."
    stale_store.save(changed_project)
    stale_store.update_video_job(
        stale_job_id,
        {"next_poll_at": datetime.now(timezone.utc).isoformat()},
    )
    stale_provider = AsyncVideoProvider({
        "status": "completed",
        "provider_status": "completed",
        "asset_url": "https://media.example/stale.mp4",
    })
    stale_client = TestClient(
        create_app(settings=make_settings(tmp_path), video_provider=stale_provider),
        client=("127.0.0.1", 54321),
    )
    with stale_client:
        deadline = time.monotonic() + 5
        stale_project = stale_client.get(f"/api/projects/{project['id']}").json()
        while time.monotonic() < deadline:
            stale_asset = stale_project["workflow"]["video"]["shots"][2]["video"]
            if stale_asset["status"] == "stale":
                break
            time.sleep(0.05)
            stale_project = stale_client.get(f"/api/projects/{project['id']}").json()
        stale_asset = stale_project["workflow"]["video"]["shots"][2]["video"]
        assert stale_asset["status"] == "stale"
        assert stale_asset["asset_url"] == "https://media.example/stale.mp4"
        assert stale_asset["versions"][-1]["stale"] is True
    assert stale_store.get_video_job(stale_job_id)["status"] == "completed"

    uncertain_provider = AsyncVideoProvider(submit_error=VideoSubmissionUncertainError("响应丢失"))
    uncertain_client = TestClient(
        create_app(settings=make_settings(tmp_path), video_provider=uncertain_provider),
        client=("127.0.0.1", 54321),
    )
    uncertain = uncertain_client.post(f"/api/projects/{project['id']}/shots/S02/video")
    assert uncertain.status_code == 202
    uncertain_worker = uncertain_client.app.state.video_job_worker
    assert uncertain_worker.process_once() is True
    interrupted_project = uncertain_client.get(f"/api/projects/{project['id']}").json()
    interrupted_asset = interrupted_project["workflow"]["video"]["shots"][1]["video"]
    assert interrupted_asset["status"] == "interrupted"
    assert "响应丢失" in interrupted_asset["error"]
    assert uncertain_client.post(f"/api/projects/{project['id']}/shots/S02/video").status_code == 409
    retried = uncertain_client.post(
        f"/api/projects/{project['id']}/shots/S02/video?confirm_uncertain=true"
    )
    assert retried.status_code == 202
    assert uncertain_provider.submissions == 1


def test_submitting_video_job_is_recovered_as_uncertain_after_restart(tmp_path):
    class AsyncVideoProvider:
        ready = True

        def __init__(self):
            self.submissions = 0

        def submit_video(self, *, project):
            self.submissions += 1
            return {"provider_task_id": "must-not-submit-again", "task_id": "must-not-submit-again"}

        def poll_video(self, *, provider_task_id):
            raise AssertionError("an interrupted submission has no safe provider task ID to poll")

    first_provider = AsyncVideoProvider()
    first_client = TestClient(
        create_app(settings=make_settings(tmp_path), video_provider=first_provider),
        client=("127.0.0.1", 54321),
    )
    project = new_project(first_client).json()
    for stage in ("script", "prompt", "storyboard", "scenes"):
        first_client.post(f"/api/projects/{project['id']}/generate/{stage}", json={})
        first_client.post(f"/api/projects/{project['id']}/approve/{stage}")
    queued = first_client.post(f"/api/projects/{project['id']}/shots/S01/video")
    assert queued.status_code == 202
    job_id = queued.json()["workflow"]["video"]["shots"][0]["video"]["job_id"]
    claimed = first_client.app.state.workflow.store.claim_next_video_job()
    assert claimed["id"] == job_id
    assert claimed["status"] == "submitting"

    resumed_provider = AsyncVideoProvider()
    resumed_client = TestClient(
        create_app(settings=make_settings(tmp_path), video_provider=resumed_provider),
        client=("127.0.0.1", 54321),
    )
    with resumed_client:
        recovered_project = resumed_client.get(f"/api/projects/{project['id']}").json()
        recovered_asset = recovered_project["workflow"]["video"]["shots"][0]["video"]
        assert recovered_asset["status"] == "interrupted"
        assert "服务在提交视频任务期间停止" in recovered_asset["error"]
        assert resumed_client.app.state.workflow.store.get_video_job(job_id)["status"] == "interrupted"
    assert first_provider.submissions == 0
    assert resumed_provider.submissions == 0


def test_agnes_video_provider_marks_server_error_submission_uncertain():
    profile = ModelCatalog.load(CATALOG_PATH).resolve("agnes-video", "video")
    provider = AgnesVideoProvider(
        profile,
        "test-key",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    502,
                    json={"code": "gateway_error", "message": "upstream unavailable"},
                    headers={"x-request-id": "req-uncertain-502"},
                )
            )
        ),
    )

    with pytest.raises(VideoSubmissionUncertainError) as error:
        provider.submit_video(project={
            "brief": {"duration_seconds": 6},
            "video_prompt": "A cinematic coffee ad",
            "aspect_ratio": "9:16",
        })

    assert "req-uncertain-502" in str(error.value)
    assert "供应商是否已受理未知" in str(error.value)


def test_agnes_video_provider_creates_and_polls_async_task():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["model"] == "agnes-video-2.5-flash"
            return httpx.Response(200, json={"id": "task-1", "video_id": "video-1"})
        return httpx.Response(200, json={"status": "completed", "url": "https://media.example/final.mp4"})

    catalog = ModelCatalog.load(CATALOG_PATH)
    profile = catalog.resolve("agnes-video", "video")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AgnesVideoProvider(profile, "test-key", client=client, sleep=lambda _: None)
    result = provider.generate_video(project={
        "brief": {"duration_seconds": 5},
        "video_prompt": "A cinematic coffee ad",
        "aspect_ratio": "9:16",
    })

    assert result["asset_url"] == "https://media.example/final.mp4"
    assert len(requests) == 2
    assert str(requests[0].url) == "https://apihub.agnes-ai.com/v1/videos"
    assert requests[1].url.path == "/agnesapi"
    assert requests[1].url.params["model_name"] == "agnes-video-2.5-flash"


def test_chat_image_tool_creates_an_attachment(monkeypatch, tmp_path):
    import app.services.chat as chat_module

    class FakeTextProvider:
        calls = 0

        def chat(self, *, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                assert any(item["function"]["name"] == "generate_image" for item in tools)
                return ChatResult(tool_calls=[{
                    "id": "call-image-1",
                    "name": "generate_image",
                    "arguments": {"prompt": "咖啡产品海报", "aspect_ratio": "9:16"},
                }])
            return ChatResult(text="图片已经生成，见附件。")

    class FakeImageProvider:
        def generate_image(self, *, prompt, aspect_ratio="9:16"):
            assert prompt == "咖啡产品海报"
            return {"asset_url": "/media/generated.png"}

    text_provider = FakeTextProvider()
    monkeypatch.setattr(chat_module, "create_text_provider", lambda profile, key: text_provider)
    monkeypatch.setattr(chat_module, "create_image_provider", lambda profile, key, assets: FakeImageProvider())
    client = TestClient(create_app(settings=make_settings(
        tmp_path, api_keys={"AGNES_API_KEY": "test-key"},
    )), client=("127.0.0.1", 54321))
    project = new_project(client).json()

    response = client.post(
        f"/api/projects/{project['id']}/chat",
        json={"message": "请生成一张咖啡产品海报"},
    )
    assert response.status_code == 200
    assistant = [message for message in response.json() if message["role"] == "assistant" and not message.get("internal")]
    assert assistant[-1]["attachments"] == [{
        "type": "image", "url": "/media/generated.png", "model": "Agnes Image 2.5 Flash",
    }]


def test_config_reports_media_provider_credentials_without_exposing_them(tmp_path):
    client = make_client(tmp_path, api_keys={"AGNES_API_KEY": "test-key"})
    config = client.get("/api/config").json()
    assert config["provider_mode"] == "connected"
    assert config["media_provider_ready"] is True
    assert all("test-key" not in str(profile) for profile in config["model_profiles"])


def test_chat_with_missing_text_key_does_not_persist_unsent_message(tmp_path):
    client = make_client(tmp_path, api_keys={})
    project = new_project(client).json()
    response = client.post(
        f"/api/projects/{project['id']}/chat",
        json={"message": "这条消息不能发送"},
    )
    assert response.status_code == 502
    assert "API Key" in response.json()["detail"]
    assert client.get(f"/api/projects/{project['id']}/chat").json() == []

    stream_response = client.post(
        f"/api/projects/{project['id']}/chat/stream",
        json={"message": "这条消息不能发送"},
    )
    stream_events = [
        json.loads(line[6:]) for line in stream_response.text.splitlines() if line.startswith("data: ")
    ]
    assert stream_events[-1]["type"] == "error"
    assert "API Key" in stream_events[-1]["message"]
    assert client.get(f"/api/projects/{project['id']}/chat").json() == []


def test_public_model_params_filter_credential_like_fields():
    profile = ModelProfile.from_dict({
        "id": "safe-model",
        "name": "Safe model",
        "modality": "text",
        "adapter": "openai_compatible",
        "model_id": "safe-model-v1",
        "base_url": "https://api.example.com/v1",
        "api_key_env": "TEST_API_KEY",
        "params": {
            "temperature": 0.5,
            "api_key": "must-not-be-returned",
            "extra": {"Authorization": "Bearer must-not-leak"},
        },
    })

    public = profile.public_dict({"TEST_API_KEY": "secret-value"})
    assert public["params"] == {"temperature": 0.5, "extra": {}}
    assert "secret-value" not in str(public)
    assert "must-not" not in str(public)


def test_model_catalog_can_be_edited_persisted_and_applied_immediately(tmp_path):
    client, catalog_path = app_with_temporary_catalog(tmp_path)
    initial = client.get("/api/config").json()
    payload = editable_model_catalog(initial)
    agnes = next(profile for profile in payload["models"] if profile["id"] == "agnes-text")
    agnes["name"] = "Agnes 文本（已编辑）"
    agnes["params"]["max_tokens"] = 4321
    payload["defaults"]["text"] = "claude-sonnet"

    response = client.put("/api/config/models", json=payload)
    assert response.status_code == 200
    updated = response.json()
    edited = next(profile for profile in updated["model_profiles"] if profile["id"] == "agnes-text")
    assert edited["name"] == "Agnes 文本（已编辑）"
    assert edited["params"]["max_tokens"] == 4321
    assert updated["default_model"] == "claude-sonnet"
    assert client.get("/api/config").json()["default_model"] == "claude-sonnet"

    saved = catalog_path.read_text(encoding="utf-8")
    assert "test-key" not in saved
    assert '"api_key":' not in saved


def test_chat_stream_emits_text_deltas_before_done(monkeypatch, tmp_path):
    class StreamingTextProvider:
        def chat(self, *, messages, tools=None):
            return ChatResult(text="流式回复")

        def chat_stream(self, *, messages, tools=None, on_delta):
            on_delta("流式")
            on_delta("回复")
            return ChatResult(text="流式回复")

    monkeypatch.setattr(
        "app.services.chat.create_text_provider",
        lambda profile, api_key: StreamingTextProvider(),
    )
    client = make_client(tmp_path, api_keys={"AGNES_API_KEY": "test-key"})
    project = client.post("/api/chat/sessions", json={}).json()

    response = client.post(
        f"/api/projects/{project['id']}/chat/stream",
        json={"message": "请回复"},
    )
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert response.status_code == 200
    assert [event["type"] for event in events] == ["status", "delta", "delta", "done"]
    assert "流式回复" in "".join(event.get("text", "") for event in events)
    history = client.get(f"/api/projects/{project['id']}/chat").json()
    assert [item["role"] for item in history] == ["user", "assistant"]


def test_model_catalog_rejects_secret_params_and_accepts_separate_credentials(tmp_path):
    client, catalog_path = app_with_temporary_catalog(tmp_path)
    initial = client.get("/api/config").json()
    payload = editable_model_catalog(initial)
    payload["models"][0]["params"]["nested"] = {"api_key": "must-not-be-saved"}

    response = client.put("/api/config/models", json=payload)
    assert response.status_code == 422
    assert "API Key" in response.json()["detail"]
    assert "must-not-be-saved" not in catalog_path.read_text(encoding="utf-8")

    payload = editable_model_catalog(initial)
    profile_id = payload["models"][0]["id"]
    payload["models"][0]["api_key"] = "direct-profile-secret"
    response = client.put("/api/config/models", json=payload)
    assert response.status_code == 200
    assert "direct-profile-secret" not in response.text
    credential_file = tmp_path / "model-credentials.json"
    assert json.loads(credential_file.read_text(encoding="utf-8")) == {profile_id: "direct-profile-secret"}
    assert credential_file.stat().st_mode & 0o777 == 0o600
    assert "direct-profile-secret" not in catalog_path.read_text(encoding="utf-8")
    assert "direct-profile-secret" not in str(client.get("/api/config").json())

    loopback_client = TestClient(
        create_app(settings=make_settings(tmp_path)),
        client=("127.0.0.1", 54321),
    )
    credential_response = loopback_client.get(f"/api/config/models/{profile_id}/credential")
    assert credential_response.status_code == 200
    assert credential_response.headers["cache-control"] == "no-store, max-age=0"
    assert credential_response.json() == {"api_key": "direct-profile-secret"}

    payload = editable_model_catalog(initial)
    payload["models"][0]["adapter"] = "unimplemented_adapter"
    response = client.put("/api/config/models", json=payload)
    assert response.status_code == 422
    assert "适配器" in response.json()["detail"]


def test_profile_credentials_are_isolated_from_shared_environment_names(tmp_path):
    client, catalog_path = app_with_temporary_catalog(tmp_path)
    initial = client.get("/api/config").json()
    payload = editable_model_catalog(initial)
    text_profile = next(item for item in payload["models"] if item["id"] == "agnes-text")
    text_profile["api_key"] = "text-profile-only-secret"
    response = client.put("/api/config/models", json=payload)
    assert response.status_code == 200

    settings = replace(
        make_settings(tmp_path),
        model_catalog_path=catalog_path,
        model_catalog=ModelCatalog.load(catalog_path),
    )
    app = create_app(settings=settings)
    loopback = TestClient(app, client=("127.0.0.1", 54321))
    remote = TestClient(app, client=("203.0.113.8", 54321))
    assert loopback.get("/api/config/models/agnes-text/credential").json() == {
        "api_key": "text-profile-only-secret",
    }
    assert loopback.get("/api/config/models/agnes-image/credential").json() == {
        "api_key": "test-key",
    }
    denied = remote.get("/api/config/models/agnes-text/credential")
    assert denied.status_code == 403
    denied_update = remote.put("/api/config/models", json=payload)
    assert denied_update.status_code == 403
    assert loopback.get("/api/config/models/agnes-text/credential").json() == {
        "api_key": "text-profile-only-secret",
    }

    payload = editable_model_catalog(loopback.get("/api/config").json())
    text_profile = next(item for item in payload["models"] if item["id"] == "agnes-text")
    text_profile["api_key"] = ""
    assert loopback.put("/api/config/models", json=payload).status_code == 200
    assert loopback.get("/api/config/models/agnes-text/credential").json() == {
        "api_key": "test-key",
    }
    assert json.loads((tmp_path / "model-credentials.json").read_text(encoding="utf-8")) == {}


def test_legacy_environment_name_credentials_migrate_to_profile_ids(tmp_path):
    settings = make_settings(tmp_path, api_keys={})
    legacy_key_name = settings.model_catalog.profiles["agnes-text"].api_key_env
    credential_path = tmp_path / "model-credentials.json"
    credential_path.write_text(json.dumps({legacy_key_name: "legacy-local-secret"}), encoding="utf-8")
    client = TestClient(create_app(settings=settings), client=("127.0.0.1", 54321))

    assert client.get("/api/config/models/agnes-text/credential").json() == {
        "api_key": "legacy-local-secret",
    }
    config = client.get("/api/config").json()
    response = client.put("/api/config/models", json=editable_model_catalog(config))
    assert response.status_code == 200
    migrated = json.loads(credential_path.read_text(encoding="utf-8"))
    assert migrated["agnes-text"] == "legacy-local-secret"
    assert legacy_key_name not in migrated


def test_model_in_use_by_project_cannot_be_deleted(tmp_path):
    client, catalog_path = app_with_temporary_catalog(tmp_path)
    project = new_project(client).json()
    initial_catalog = catalog_path.read_text(encoding="utf-8")
    payload = editable_model_catalog(client.get("/api/config").json())
    payload["models"] = [profile for profile in payload["models"] if profile["id"] != "agnes-text"]
    payload["defaults"]["text"] = "claude-sonnet"

    response = client.put("/api/config/models", json=payload)
    assert response.status_code == 409
    assert "agnes-text" in response.json()["detail"]
    assert catalog_path.read_text(encoding="utf-8") == initial_catalog
    assert client.get(f"/api/projects/{project['id']}").json()["model"] == "agnes-text"

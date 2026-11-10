from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.providers.catalog import ModelCatalog
from app.providers.media import AssetStore
from app.services.video_assembly import VideoAssemblyError, VideoAssemblyService
from app.store import ProjectStore

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "models.json"


def make_settings(tmp_path: Path) -> Settings:
    catalog = ModelCatalog.load(CATALOG_PATH)
    return Settings(
        database_path=tmp_path / "test.db",
        model_catalog_path=CATALOG_PATH,
        model_catalog=catalog,
        api_keys={profile.api_key_env: "test-key" for profile in catalog.profiles.values()},
    )


def make_client(tmp_path: Path, *, image_provider=None) -> TestClient:
    return TestClient(
        create_app(settings=make_settings(tmp_path), image_provider=image_provider),
        client=("127.0.0.1", 54321),
    )


def test_novel_chapters_keep_revisions_and_only_use_approved_prior_context(tmp_path, monkeypatch):
    class CapturingProvider:
        calls: list[str] = []

        def generate(self, *, system, user):
            self.calls.append(user)
            return ["生成的大纲", "CHAPTER_ONE_CONFIRMED", "第三章草稿"][len(self.calls) - 1]

    provider = CapturingProvider()
    monkeypatch.setattr("app.services.novels.create_text_provider", lambda profile, api_key: provider)
    client = make_client(tmp_path)
    novel = client.post("/api/novels", json={
        "title": "潮汐档案",
        "logline": "一名档案员追查被抹去的海难记录。",
        "world_setting": "港口城市每逢大潮都会封存一批档案。",
        "protagonist": "林澄，谨慎但不愿放弃追问。",
    }).json()
    novel_id = novel["id"]
    chapters = []
    for number in (1, 2, 3):
        response = client.post(f"/api/novels/{novel_id}/chapters", json={
            "chapter_number": number,
            "title": f"第 {number} 章",
            "outline": f"第 {number} 章的初始大纲。",
        })
        assert response.status_code == 201
        chapters.append(response.json())

    duplicate = client.post(f"/api/novels/{novel_id}/chapters", json={
        "chapter_number": 1, "title": "重复章节",
    })
    assert duplicate.status_code == 409

    outline = client.post(
        f"/api/novels/{novel_id}/chapters/{chapters[0]['id']}/generate",
        json={"kind": "outline", "model": "agnes-text"},
    )
    assert outline.status_code == 200
    assert outline.json()["outline"] == "生成的大纲"
    generated = client.post(
        f"/api/novels/{novel_id}/chapters/{chapters[0]['id']}/generate",
        json={"kind": "chapter", "model": "agnes-text"},
    )
    assert generated.status_code == 200
    assert generated.json()["content"] == "CHAPTER_ONE_CONFIRMED"
    approved = client.post(f"/api/novels/{novel_id}/chapters/{chapters[0]['id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    unapproved_text = "UNAPPROVED_CHAPTER_TWO_MUST_NOT_LEAK"
    edited_second = client.put(
        f"/api/novels/{novel_id}/chapters/{chapters[1]['id']}",
        json={"content": unapproved_text},
    )
    assert edited_second.status_code == 200
    assert edited_second.json()["status"] == "draft"

    third = client.post(
        f"/api/novels/{novel_id}/chapters/{chapters[2]['id']}/generate",
        json={"kind": "chapter", "model": "agnes-text"},
    )
    assert third.status_code == 200
    assert "CHAPTER_ONE_CONFIRMED" in provider.calls[-1]
    assert unapproved_text not in provider.calls[-1]

    changed_first = client.put(
        f"/api/novels/{novel_id}/chapters/{chapters[0]['id']}",
        json={"content": "人工修订后的第一章"},
    )
    assert changed_first.status_code == 200
    assert changed_first.json()["status"] == "draft"
    assert changed_first.json()["revisions"][-1]["content"] == "CHAPTER_ONE_CONFIRMED"
    assert changed_first.json()["revisions"][-1]["status"] == "approved"
    refreshed = client.get(f"/api/novels/{novel_id}").json()
    later_chapters = {chapter["chapter_number"]: chapter for chapter in refreshed["chapters"]}
    assert later_chapters[2]["status"] == "stale"
    assert unapproved_text in later_chapters[2]["revisions"][-1]["content"]
    assert later_chapters[3]["status"] == "stale"
    assert later_chapters[3]["revisions"][-1]["status"] == "draft"
    summary = client.get("/api/novels").json()
    assert summary[0]["chapter_count"] == 3
    assert summary[0]["approved_chapter_count"] == 0


def test_episode_outline_needs_approval_and_shot_assets_are_prompt_references(tmp_path, monkeypatch):
    class CapturingTextProvider:
        calls: list[tuple[str, str]] = []

        def generate(self, *, system, user):
            self.calls.append((system, user))
            if "分集规划助手" in system:
                return "EPISODE_OUTLINE_DRAFT_MARKER"
            if "当前步骤：scenes" in system:
                shots = [{
                    "shot_id": "S01", "duration_seconds": 8, "aspect_ratio": "9:16",
                    "purpose": "让主角发现异常", "visual_description": "雨夜仓库入口",
                    "voiceover": "", "screen_text": "", "transition": "切入",
                    "continuity": "雨衣保持深蓝色", "image_prompt": "雨夜仓库",
                    "video_prompt": "缓慢推进", "negative_prompt": "",
                }]
                return json.dumps({
                    "shots": shots,
                    "edit_plan": {"sequence": ["S01"], "pacing": "克制", "transitions": "切入"},
                }, ensure_ascii=False)
            return "阶段草稿"

    class CapturingImageProvider:
        prompt = ""

        def generate_image(self, *, prompt, aspect_ratio):
            self.prompt = prompt
            return {"asset_url": "https://media.example.test/frame.png"}

    text_provider = CapturingTextProvider()
    image_provider = CapturingImageProvider()
    monkeypatch.setattr(
        "app.services.workflow.create_text_provider",
        lambda profile, api_key: text_provider,
    )
    client = make_client(tmp_path, image_provider=image_provider)
    series = client.post("/api/series", json={
        "title": "雾港来信",
        "logline": "妹妹追查姐姐失踪的真相。",
        "world_setting": "潮汐会封锁港口旧仓库。",
        "visual_style": "低饱和雨夜，写实光影。",
    }).json()
    series_id = series["id"]
    asset = client.post(f"/api/series/{series_id}/assets", json={
        "kind": "character", "name": "林澄", "description": "调查姐姐失踪的妹妹。",
        "visual_prompt": "深蓝雨衣，短黑发。", "continuity_notes": "右手腕始终戴旧表。",
    }).json()
    project = client.post("/api/projects", json={
        "track": "drama", "business": "妹妹寻找失踪的姐姐。", "product": "雾港来信 EP01",
        "audience": "悬疑短剧观众", "platform": "竖屏短剧", "style": "克制写实",
        "duration_seconds": 60, "series_id": series_id, "episode_number": 1,
    }).json()
    project_id = project["id"]

    generated_outline = client.post(f"/api/projects/{project_id}/episode-outline/generate", json={})
    assert generated_outline.status_code == 200
    assert generated_outline.json()["status"] == "draft"
    generated_script = client.post(f"/api/projects/{project_id}/generate/script", json={})
    assert generated_script.status_code == 200
    assert "EPISODE_OUTLINE_DRAFT_MARKER" not in text_provider.calls[-1][1]

    approved_text = "EPISODE_OUTLINE_APPROVED_MARKER"
    saved_outline = client.put(f"/api/projects/{project_id}/episode-outline", json={
        "content": approved_text, "status": "approved",
    })
    assert saved_outline.status_code == 200
    generated_script = client.post(f"/api/projects/{project_id}/generate/script", json={})
    assert generated_script.status_code == 200
    assert approved_text in text_provider.calls[-1][1]

    assert client.post(f"/api/projects/{project_id}/approve/script").status_code == 200
    for stage in ("prompt", "storyboard", "scenes"):
        assert client.post(f"/api/projects/{project_id}/generate/{stage}", json={}).status_code == 200
        assert client.post(f"/api/projects/{project_id}/approve/{stage}").status_code == 200

    linked = client.put(f"/api/projects/{project_id}/shots/S01/assets", json={"asset_ids": [asset["id"]]})
    assert linked.status_code == 200
    assert linked.json()["workflow"]["video"]["shots"][0]["asset_ids"] == [asset["id"]]
    generated_image = client.post(f"/api/projects/{project_id}/shots/S01/image")
    assert generated_image.status_code == 200
    assert "林澄" in image_provider.prompt
    assert "右手腕始终戴旧表" in image_provider.prompt
    assert "不是操作指令" in image_provider.prompt

    changed_link = client.put(f"/api/projects/{project_id}/shots/S01/assets", json={"asset_ids": []})
    assert changed_link.status_code == 200
    shot = changed_link.json()["workflow"]["video"]["shots"][0]
    assert shot["image"]["status"] == "stale"
    assert shot["stale"] is True

    invalid_order = client.put(f"/api/projects/{project_id}/render-order", json={"shot_ids": ["S99"]})
    assert invalid_order.status_code == 422
    saved_order = client.put(f"/api/projects/{project_id}/render-order", json={"shot_ids": ["S01"]})
    assert saved_order.status_code == 200
    assert saved_order.json()["workflow"]["video"]["render_order"] == ["S01"]


def _render_project(
    asset_urls: list[str], *, captions: bool = False, output_aspect_ratio: str | None = None,
    output_frame_mode: str = "pad", output_preset: str = "standard",
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    shots = []
    scene_shots = []
    for index, asset_url in enumerate(asset_urls, start=1):
        shot_id = f"S{index:02d}"
        shots.append({
            "shot_id": shot_id,
            "video_prompt": f"镜头 {index}",
            "video": {"status": "ready", "asset_url": asset_url},
        })
        scene_shots.append({
            "shot_id": shot_id, "duration_seconds": 4, "aspect_ratio": "16:9",
            "purpose": f"镜头 {index}", "visual_description": "港口夜景",
            "voiceover": "潮声传来" if captions and index == 1 else "",
            "screen_text": "雾港来信" if captions and index == 1 else "",
            "transition": "切入", "continuity": "保持夜色",
            "image_prompt": "港口夜景", "video_prompt": f"镜头 {index}", "negative_prompt": "",
        })
    ids = [shot["shot_id"] for shot in shots]
    project = {
        "id": "render-project",
        "mode": "workflow",
        "track": "drama",
        "created_at": now,
        "updated_at": now,
        "workflow": {
            "scenes": {"status": "approved", "content": json.dumps({
                "shots": scene_shots,
                "edit_plan": {"sequence": ids, "pacing": "克制", "transitions": "切入"},
            }, ensure_ascii=False)},
            "video": {"shots": shots, "render_order": ids},
        },
    }
    if output_aspect_ratio:
        project["brief"] = {
            "output_aspect_ratio": output_aspect_ratio,
            "output_frame_mode": output_frame_mode,
            "output_preset": output_preset,
        }
    return project


def test_local_video_assembly_normalizes_clips_and_marks_result_stale_after_reorder(tmp_path):
    store = ProjectStore(tmp_path / "assembly.db")
    assets = AssetStore(tmp_path / "media")
    first_url = assets.save(b"fake first mp4", ".mp4")
    second_url = assets.save(b"fake second mp4", ".mp4")
    project = store.save(_render_project([first_url, second_url]))
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text("test double", encoding="utf-8")
    first_path = assets.directory / Path(urlsplit(first_url).path).name
    second_path = assets.directory / Path(urlsplit(second_url).path).name
    commands: list[list[str]] = []

    def fake_runner(command, **kwargs):
        commands.append(list(command))
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 64)
            return subprocess.CompletedProcess(command, 0, "", "")
        input_path = Path(command[command.index("-i") + 1])
        if input_path.name == "assembled.mp4":
            diagnostic = (
                "Duration: 00:00:05.25, start: 0.000000, bitrate: 500 kb/s\n"
                " Stream #0:0: Video: h264, yuv420p, 640x360, 30 fps\n"
                " Stream #0:1: Audio: aac, 48000 Hz, stereo\n"
            )
            return subprocess.CompletedProcess(command, 0, "", diagnostic)
        has_audio = input_path == first_path
        diagnostic = (
            "Duration: 00:00:02.00, start: 0.000000, bitrate: 500 kb/s\n"
            " Stream #0:0: Video: h264, yuv420p, 640x360, 30 fps\n"
        )
        if has_audio:
            diagnostic += " Stream #0:1: Audio: aac, 48000 Hz, stereo\n"
        else:
            diagnostic = diagnostic.replace("00:00:02.00", "00:00:03.25")
        return subprocess.CompletedProcess(command, 0, "", diagnostic)

    service = VideoAssemblyService(store, assets, ffmpeg_path=str(ffmpeg), runner=fake_runner)
    assembled = service.assemble(project["id"])
    final_video = assembled["workflow"]["video"]["final_video"]
    assert final_video["status"] == "ready"
    assert final_video["duration_seconds"] == 5.25
    assert final_video["width"] == 640
    assert final_video["height"] == 360
    assert Path(assets.directory / Path(urlsplit(final_video["asset_url"]).path).name).is_file()
    filter_graph = commands[2][commands[2].index("-filter_complex") + 1]
    assert "anullsrc=r=48000:cl=stereo" in filter_graph

    reordered = service.set_order(project["id"], ["S02", "S01"])
    assert reordered["workflow"]["video"]["final_video"]["status"] == "stale"
    assert "顺序已更改" in reordered["workflow"]["video"]["final_video"]["stale_reason"]


def test_local_assembly_applies_project_frame_preset_and_burns_approved_subtitles(tmp_path):
    store = ProjectStore(tmp_path / "assembly-settings.db")
    assets = AssetStore(tmp_path / "assembly-settings-media")
    media_url = assets.save(b"fake mp4", ".mp4")
    project = store.save(_render_project(
        [media_url], captions=True, output_aspect_ratio="9:16", output_frame_mode="crop", output_preset="compact",
    ))
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text("test double", encoding="utf-8")
    media_path = assets.directory / Path(urlsplit(media_url).path).name
    commands: list[list[str]] = []

    def fake_runner(command, **kwargs):
        commands.append(list(command))
        input_path = Path(command[command.index("-i") + 1]) if "-i" in command else None
        if "-filter_complex" in command:
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 64)
            return subprocess.CompletedProcess(command, 0, "", "")
        duration = "00:00:04.00"
        width, height = (720, 1280) if input_path and input_path.name == "assembled.mp4" else (640, 360)
        diagnostic = (
            f"Duration: {duration}, start: 0.000000, bitrate: 500 kb/s\\n"
            f" Stream #0:0: Video: h264, yuv420p, {width}x{height}, 30 fps\\n"
            " Stream #0:1: Audio: aac, 48000 Hz, stereo\\n"
        )
        return subprocess.CompletedProcess(command, 0, "", diagnostic)

    service = VideoAssemblyService(store, assets, ffmpeg_path=str(ffmpeg), runner=fake_runner)
    assembled = service.assemble(project["id"])
    final_video = assembled["workflow"]["video"]["final_video"]
    assert final_video["status"] == "ready"
    assert (final_video["width"], final_video["height"]) == (720, 1280)
    assert final_video["output_aspect_ratio"] == "9:16"
    assert final_video["preset"] == "compact"
    assert final_video["subtitles_burned"] is True
    assert final_video["subtitle_cue_count"] == 1
    command = next(item for item in commands if "-filter_complex" in item)
    graph = command[command.index("-filter_complex") + 1]
    assert "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280" in graph
    assert "subtitles=filename=" in graph
    assert command[command.index("-crf") + 1] == "25"
    assert command[command.index("-map") + 1] == "[vsub]"
    assert media_path.is_file()


def test_subtitle_download_routes_and_assembly_settings(tmp_path):
    client = make_client(tmp_path)
    project = client.post("/api/projects", json={
        "track": "drama", "business": "调查失踪案", "product": "雾港来信",
        "audience": "悬疑短剧观众", "platform": "竖屏短剧", "style": "克制写实",
        "duration_seconds": 60,
    }).json()
    workflow = client.app.state.workflow
    seeded = workflow.get_project(project["id"])
    seeded["workflow"]["scenes"].update({
        "status": "approved",
        "content": _render_project(["/media/placeholder.mp4"], captions=True)["workflow"]["scenes"]["content"],
    })
    workflow.store.save(seeded)

    srt = client.get(f"/api/projects/{project['id']}/subtitles.srt")
    assert srt.status_code == 200
    assert "潮声传来" in srt.text and "雾港来信" in srt.text
    assert "00:00:00,000 --> 00:00:04,000" in srt.text
    assert "attachment" in srt.headers["content-disposition"]
    ass = client.get(f"/api/projects/{project['id']}/subtitles.ass")
    assert ass.status_code == 200
    assert "[Events]" in ass.text and "潮声传来" in ass.text

    updated = client.put(
        f"/api/projects/{project['id']}/assembly-settings",
        headers={"Origin": "http://testserver"},
        json={"output_aspect_ratio": "16:9", "output_frame_mode": "crop", "output_preset": "quality"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["brief"]["output_aspect_ratio"] == "16:9"
    assert updated.json()["brief"]["output_frame_mode"] == "crop"
    assert updated.json()["brief"]["output_preset"] == "quality"
    blocked = client.put(
        f"/api/projects/{project['id']}/assembly-settings",
        headers={"Origin": "https://attacker.example"},
        json={"output_aspect_ratio": "16:9", "output_frame_mode": "pad", "output_preset": "standard"},
    )
    assert blocked.status_code == 403


def test_local_video_assembly_rejects_remote_inputs_and_preserves_concurrent_edits(tmp_path):
    store = ProjectStore(tmp_path / "assembly-race.db")
    assets = AssetStore(tmp_path / "race-media")
    remote = "https://media.example.test/clip.mp4"
    project = store.save(_render_project([remote]))
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text("test double", encoding="utf-8")
    calls = 0

    def fake_runner(command, **kwargs):
        nonlocal calls
        calls += 1
        if "-filter_complex" in command:
            current = store.get(project["id"])
            current["updated_at"] = "concurrent-user-edit"
            store.save(current)
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 64)
            return subprocess.CompletedProcess(command, 0, "", "")
        diagnostic = (
            "Duration: 00:00:01.00, start: 0.000000, bitrate: 500 kb/s\n"
            " Stream #0:0: Video: h264, yuv420p, 640x360, 30 fps\n"
        )
        return subprocess.CompletedProcess(command, 0, "", diagnostic)

    service = VideoAssemblyService(store, assets, ffmpeg_path=str(ffmpeg), runner=fake_runner)
    with pytest.raises(VideoAssemblyError) as remote_error:
        service.assemble(project["id"])
    assert remote_error.value.status_code == 422
    assert "远程视频" in str(remote_error.value)
    assert calls == 0

    local_url = assets.save(b"fake mp4", ".mp4")
    project = store.get(project["id"])
    project["workflow"]["video"]["shots"][0]["video"]["asset_url"] = local_url
    project["updated_at"] = datetime.now(timezone.utc).isoformat()
    store.save(project)
    with pytest.raises(VideoAssemblyError) as conflict:
        service.assemble(project["id"])
    assert conflict.value.status_code == 409
    assert "项目或镜头素材发生变化" in str(conflict.value)
    saved = store.get(project["id"])
    assert "final_video" not in saved["workflow"]["video"]


def test_local_assembly_api_requires_loopback_and_same_origin(tmp_path):
    client = make_client(tmp_path)
    created = client.post("/api/projects", json={
        "track": "drama", "business": "调查失踪案", "product": "雾港来信",
        "audience": "悬疑短剧观众", "platform": "竖屏短剧", "style": "克制写实",
        "duration_seconds": 60,
    })
    assert created.status_code == 201
    path = f"/api/projects/{created.json()['id']}/assemble"
    assert client.post(path).status_code == 403
    assert client.post(path, headers={"Origin": "https://attacker.example"}).status_code == 403

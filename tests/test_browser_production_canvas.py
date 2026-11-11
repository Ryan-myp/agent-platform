from __future__ import annotations

import json
import socket
import threading
import time
import zipfile
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright

import uvicorn
from app.config import Settings
from app.main import create_app
from app.providers.catalog import ModelCatalog

ROOT = Path(__file__).resolve().parents[1]


def _complete_text_stages(application, project_id: str) -> None:
    workflow = application.state.workflow
    content = {
        "script": "妹妹在雨夜听见姐姐留下的录音带，决定循着杂音寻找真相。",
        "prompt": "人物：妹妹穿深色雨衣；场景：旧公寓与雨夜；画面克制写实，录音带裂痕保持连续。",
        "storyboard": "S01 · 特写 · 妹妹按下录音机，屏幕跳出 01:17。",
        "scenes": json.dumps({
            "shots": [{
                "shot_id": "S01",
                "duration_seconds": 6,
                "aspect_ratio": "9:16",
                "purpose": "用录音带建立悬念",
                "visual_description": "妹妹在旧公寓窗边按下录音键，雨水映着微弱灯光。",
                "voiceover": "听见了吗？",
                "screen_text": "01:17",
                "transition": "切黑",
                "continuity": "录音带外壳有一道裂痕。",
                "image_prompt": "A restrained cinematic close-up of a cassette recorder beside a rain-streaked window.",
                "video_prompt": "A hand presses play; the cassette reels begin to turn slowly.",
                "negative_prompt": "No logos or additional characters.",
            }],
            "edit_plan": {
                "sequence": ["S01"],
                "pacing": "短停顿后切黑。",
                "transitions": "切黑。",
                "music_direction": "低音氛围。",
                "sound_design": "雨声与磁带杂音。",
                "subtitle_direction": "口播与屏幕文字分行显示。",
                "notes": "仅用于本地流程演示。",
            },
        }, ensure_ascii=False),
    }
    for stage, text in content.items():
        project = workflow.store.get(project_id)
        project["workflow"][stage]["status"] = "ready"
        workflow.store.save(project)
        workflow.update_content(project_id, stage, text)
        workflow.approve(project_id, stage)


def test_production_canvas_walks_workflow_subtitles_and_delivery_without_providers(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.chat.create_text_provider",
        lambda profile, api_key: pytest.fail("2D production canvas must not call a text provider"),
    )
    monkeypatch.setattr(
        "app.services.workflow.create_text_provider",
        lambda profile, api_key: pytest.fail("2D production canvas must not call a text provider"),
    )
    catalog_path = ROOT / "config" / "models.json"
    catalog = ModelCatalog.load(catalog_path)
    settings = Settings(
        database_path=tmp_path / "production-canvas-browser.db",
        model_catalog_path=catalog_path,
        model_catalog=catalog,
        api_keys={profile.api_key_env: "production-canvas-test" for profile in catalog.profiles.values()},
    )
    application = create_app(settings=settings)
    project = application.state.workflow.create_project({
        "mode": "workflow",
        "track": "drama",
        "business": "旧录音带揭开一宗失踪案。",
        "product": "雨夜录音带",
        "audience": "悬疑短剧观众",
        "platform": "竖屏短剧",
        "duration_seconds": 30,
        "style": "克制写实",
    })
    _complete_text_stages(application, project["id"])

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("Production canvas E2E server did not start")

    base = f"http://127.0.0.1:{port}"
    external_requests: list[str] = []
    page_errors: list[str] = []

    def guard_route(route):
        if route.request.url.startswith(base):
            route.continue_()
        else:
            external_requests.append(route.request.url)
            route.abort()

    try:
        with sync_playwright() as playwright_api:
            browser = playwright_api.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.route("**/*", guard_route)
            page = context.new_page()
            page.set_default_timeout(10_000)
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(base, wait_until="networkidle")
            page.locator(f'.project-link[data-project-id="{project["id"]}"]').click()
            page.locator("#view-toggle").click()
            page.get_by_role("button", name="2D 制作预演").click()

            expect(page.get_by_role("heading", name="从项目设定到交付")).to_be_visible()
            expect(page.locator(".production-flow-step")).to_have_count(6)
            expect(page.get_by_text("关键帧 0/1 · 视频片段 0/1")).to_be_visible()
            expect(page.get_by_text("1 个镜头含口播或屏幕文字")).to_be_visible()
            expect(page.get_by_role("heading", name="2D 镜头预演带")).to_be_visible()

            page.get_by_role("button", name="打开分镜步骤").click()
            expect(page.get_by_role("heading", name="分镜", exact=True)).to_be_visible()
            page.get_by_role("button", name="2D 制作预演").click()
            page.get_by_role("button", name="打开手工分镜").first.click()
            page.get_by_role("button", name="添加第一个镜头").click()
            page.locator('[data-canvas-card] [data-canvas-field="title"]').fill("手工补充镜头")
            page.locator('[data-canvas-card] [data-canvas-field="shot_id"]').fill("S02")
            with page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/storyboard-canvas")) as canvas_save:
                page.wait_for_timeout(800)
            assert canvas_save.value.status == 200
            page.get_by_role("button", name="制作总览").click()
            expect(page.get_by_role("list", name="按顺序排列的镜头与逐镜素材")).to_be_visible()
            expect(page.get_by_role("list", name="未关联到工作流的手工分镜")).to_be_visible()
            expect(page.get_by_role("heading", name="独立手工分镜 · 不自动同步")).to_be_visible()
            expect(page.get_by_text("手工补充镜头")).to_be_visible()

            page.get_by_role("button", name="查看逐镜素材").click()
            expect(page.get_by_role("heading", name="最终 MP4 合成")).to_be_visible()
            with page.expect_download() as subtitle_download:
                page.get_by_role("link", name="下载 SRT").click()
            subtitle_text = Path(subtitle_download.value.path()).read_text(encoding="utf-8")
            assert "听见了吗？" in subtitle_text
            assert "01:17" in subtitle_text

            page.get_by_role("button", name="2D 制作预演").click()
            with page.expect_download() as delivery_download:
                page.get_by_role("link", name="下载制作交付包").first.click()
            with zipfile.ZipFile(delivery_download.value.path()) as package:
                names = package.namelist()
                assert "manual-storyboard.md" in names
                assert any(name.endswith("production-report.json") for name in names)

            page.set_viewport_size({"width": 390, "height": 844})
            dimensions = page.evaluate("({viewport: innerWidth, document: document.documentElement.scrollWidth})")
            assert dimensions["document"] <= dimensions["viewport"] + 1, dimensions
            expect(page.get_by_role("button", name="返回创作流程").first).to_be_visible()
            assert not external_requests, external_requests
            assert not page_errors, page_errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert not thread.is_alive(), "Production canvas E2E server did not stop cleanly"

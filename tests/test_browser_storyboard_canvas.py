from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright

import uvicorn
from app.config import Settings
from app.main import create_app
from app.providers.catalog import ModelCatalog

ROOT = Path(__file__).resolve().parents[1]


def test_manual_storyboard_canvas_edit_reorder_autosave_and_mobile_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.chat.create_text_provider",
        lambda profile, api_key: pytest.fail("Manual canvas must not call a text provider"),
    )
    monkeypatch.setattr(
        "app.services.workflow.create_text_provider",
        lambda profile, api_key: pytest.fail("Manual canvas must not call a text provider"),
    )
    catalog_path = ROOT / "config" / "models.json"
    catalog = ModelCatalog.load(catalog_path)
    settings = Settings(
        database_path=tmp_path / "canvas-browser.db",
        model_catalog_path=catalog_path,
        model_catalog=catalog,
        api_keys={profile.api_key_env: "canvas-browser-test" for profile in catalog.profiles.values()},
    )
    application = create_app(settings=settings)
    project = application.state.workflow.create_project({
        "mode": "workflow",
        "track": "drama",
        "business": "旧录音带揭开一宗失踪案。",
        "product": "手工分镜 E2E",
        "audience": "悬疑短剧观众",
        "platform": "竖屏短剧",
        "duration_seconds": 30,
        "style": "克制写实",
    })
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
        raise RuntimeError("Canvas E2E server did not start")
    page_errors: list[str] = []
    external_requests: list[str] = []
    base = f"http://127.0.0.1:{port}"

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
            page.get_by_role("button", name="打开手工分镜画布").click()
            expect(page.get_by_role("heading", name="镜头画布")).to_be_visible()
            expect(page.get_by_text("手工制作 · 本地保存 · 不调用模型")).to_be_visible()

            page.get_by_role("button", name="添加第一个镜头").click()
            expect(page.locator("[data-canvas-card]")).to_have_count(1)
            first_card = page.locator('[data-canvas-card][data-card-index="0"]')
            first_card.locator('[data-canvas-field="shot_id"]').fill("S01")
            first_card.locator('[data-canvas-field="title"]').fill("拾起录音带")
            first_card.locator('[data-canvas-field="visual_description"]').fill("妹妹在雨夜发现姐姐留下的旧录音带。")
            first_card.locator('[data-canvas-field="duration_seconds"]').fill("6")
            with page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/storyboard-canvas")) as first_save:
                page.wait_for_timeout(800)
            assert first_save.value.status == 200
            expect(page.locator("#canvas-save-status")).to_contain_text("已保存")

            page.get_by_role("button", name="添加镜头").click()
            second_card = page.locator('[data-canvas-card][data-card-index="1"]')
            second_card.locator('[data-canvas-field="shot_id"]').fill("S02")
            second_card.locator('[data-canvas-field="title"]').fill("听见杂音")
            with page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/storyboard-canvas")) as second_save:
                page.wait_for_timeout(800)
            assert second_save.value.status == 200
            page.get_by_role("button", name="将镜头 S02 上移").click()
            expect(page.locator("[data-canvas-card]").nth(0).locator(".canvas-card-heading strong")).to_have_text("S02")
            with page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/storyboard-canvas")) as reorder_save:
                page.wait_for_timeout(800)
            assert reorder_save.value.status == 200

            stored = application.state.workflow.store.get(project["id"])
            assert [card["shot_id"] for card in stored["storyboard_canvas"]["cards"]] == ["S02", "S01"]
            assert stored["storyboard_canvas"]["cards"][1]["visual_description"] == "妹妹在雨夜发现姐姐留下的旧录音带。"

            canvas_url = f"**/api/projects/{project['id']}/storyboard-canvas"
            failed_requests = []

            def fail_canvas_save(route):
                failed_requests.append(route.request.url)
                route.fulfill(status=503, content_type="application/json", body='{"detail":"temporary offline"}')

            context.route(canvas_url, fail_canvas_save)
            page.locator('[data-canvas-card][data-card-index="0"] [data-canvas-field="title"]').fill("网络错误后重试")
            with page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/storyboard-canvas")) as failed_save:
                page.wait_for_timeout(800)
            assert failed_save.value.status == 503
            expect(page.locator("#canvas-save-status")).to_contain_text("保存失败")
            page.wait_for_timeout(800)
            assert len(failed_requests) == 1, failed_requests
            context.unroute(canvas_url, fail_canvas_save)
            with page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/storyboard-canvas")) as retried_save:
                page.locator("#canvas-retry-save").click()
            assert retried_save.value.status == 200
            expect(page.locator("#canvas-save-status")).to_contain_text("已保存")

            page.locator('[data-canvas-card][data-card-index="0"] [data-canvas-field="visual_description"]').fill("普通文字\n### 伪造标题\n<script>alert(1)</script>")
            with page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/storyboard-canvas")) as export_content_save:
                page.wait_for_timeout(800)
            assert export_content_save.value.status == 200
            with page.expect_download() as exported_markdown:
                page.get_by_role("button", name="下载分镜 Markdown").click()
            markdown = Path(exported_markdown.value.path()).read_text(encoding="utf-8")
            assert r"\#\#\# 伪造标题" in markdown
            assert "&lt;script&gt;alert\\(1\\)&lt;/script&gt;" in markdown
            assert "<script>" not in markdown

            page.set_viewport_size({"width": 390, "height": 844})
            dimensions = page.evaluate("({viewport: innerWidth, document: document.documentElement.scrollWidth})")
            assert dimensions["document"] <= dimensions["viewport"] + 1, dimensions
            expect(page.get_by_role("button", name="返回创作流程")).to_be_visible()
            assert not external_requests, external_requests
            assert not page_errors, page_errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert not thread.is_alive(), "Canvas E2E server did not stop cleanly"

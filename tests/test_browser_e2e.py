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
from app.providers.text import ChatResult

ROOT = Path(__file__).resolve().parents[1]


class FakeTextProvider:
    def generate(self, *, system: str, user: str) -> str:
        return "模拟阶段草稿：咖啡馆会员工具的创作内容。"

    def chat(self, *, messages: list[dict], tools=None) -> ChatResult:
        return ChatResult(text="模拟助手回复：已收到并处理本轮内容。")


def start_isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.services.chat.create_text_provider", lambda profile, api_key: FakeTextProvider())
    monkeypatch.setattr("app.services.workflow.create_text_provider", lambda profile, api_key: FakeTextProvider())
    catalog_path = ROOT / "config" / "models.json"
    catalog = ModelCatalog.load(catalog_path)
    settings = Settings(
        database_path=tmp_path / "e2e.db",
        model_catalog_path=catalog_path,
        model_catalog=catalog,
        api_keys={profile.api_key_env: "e2e-no-network" for profile in catalog.profiles.values()},
    )
    application = create_app(settings=settings)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, name="browser-e2e-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("Isolated E2E server did not start")
    return f"http://127.0.0.1:{port}", server, thread


def test_browser_chat_workflow_archive_and_mobile_regression(tmp_path, monkeypatch):
    base, server, server_thread = start_isolated_app(tmp_path, monkeypatch)
    page_errors: list[str] = []
    http_errors: list[tuple[int, str]] = []
    external_requests: list[str] = []
    chat_title = "E2E 附件对话已重命名"
    workflow_title = "E2E 咖啡会员流程"
    attachment_name = "e2e-notes.txt"

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
            page.on(
                "response",
                lambda response: http_errors.append((response.status, response.url))
                if response.status >= 400 else None,
            )
            page.goto(base, wait_until="networkidle")
            expect(page.locator("#chat-input")).to_be_visible()

            # Attachment preview, remove, upload and send through the isolated fake model.
            page.locator("#new-project").click()
            file_payload = {
                "name": attachment_name,
                "mimeType": "text/plain",
                "buffer": b"Local coffee membership renewal notes.",
            }
            page.locator("#chat-file-input").set_input_files(file_payload)
            expect(page.locator(".chat-pending-file")).to_be_visible()
            expect(page.locator("#chat-pending-attachments")).to_contain_text(attachment_name)
            page.locator("[data-remove-pending]").click()
            expect(page.locator(".chat-pending-file")).to_have_count(0)
            page.locator("#chat-file-input").set_input_files(file_payload)
            page.locator("#chat-input").fill("请结合附件内容，给我一个简短总结。")
            page.locator("#send-chat").click()
            expect(page.get_by_text("模拟助手回复：已收到并处理本轮内容。", exact=True)).to_be_visible()
            expect(page.locator(".chat-message-user").last).to_contain_text(attachment_name)
            expect(page.locator("#chat-pending-attachments")).to_be_hidden()

            chat_entry = page.locator(".project-entry").filter(has_text="请结合附件内容").first
            chat_id = chat_entry.locator(".project-link").get_attribute("data-project-id")
            chat_entry.locator("[data-rename-project]").click()
            page.locator("#rename-input").fill(chat_title)
            page.locator("#rename-dialog button[type=submit]").click()
            expect(page.locator(".project-link").filter(has_text=chat_title)).to_be_visible()
            page.locator(".project-entry").filter(has_text=chat_title).locator("[data-archive-project]").click()
            expect(page.locator("#confirm-message")).to_contain_text("聊天内容、制作记录、任务和本地媒体都会保留")
            page.locator("#confirm-accept").click()
            expect(page.locator(".project-link").filter(has_text=chat_title)).to_have_count(0)
            archived_chat = page.request.get(f"{base}/api/projects/{chat_id}").json()
            assert archived_chat["archived"] is True
            history = page.request.get(f"{base}/api/projects/{chat_id}/chat/history?limit=10").json()
            assert attachment_name in str(history)

            # Inspect usage and safely remove an attachment that was uploaded but never referenced.
            orphan_upload = page.evaluate("""async ({projectId}) => {
              const form = new FormData();
              form.append("files", new File(["unused E2E attachment"], "e2e-orphan.txt", {type: "text/plain"}));
              const response = await fetch(`/api/projects/${projectId}/chat/attachments`, {method: "POST", body: form});
              return {status: response.status, body: await response.json()};
            }""", {"projectId": chat_id})
            assert orphan_upload["status"] == 201, orphan_upload
            page.locator("#open-usage").click()
            expect(page.locator("#usage-dialog")).to_be_visible()
            expect(page.locator("#usage-summary")).to_contain_text("本地保留记录")
            expect(page.locator("#storage-cleanup-list")).to_contain_text("e2e-orphan.txt")
            page.locator("#confirm-storage-cleanup").click()
            expect(page.locator("#confirm-message")).to_contain_text("1 条未引用附件记录")
            page.locator("#confirm-accept").click()
            expect(page.locator("#storage-cleanup-status")).to_contain_text("已删除 1 条附件记录")
            remaining = page.request.get(f"{base}/api/storage/cleanup/preview").json()
            assert not any(item["filename"] == "e2e-orphan.txt" for item in remaining["attachments"])
            page.locator("#close-usage").click()

            # Create a workflow, generate and approve one stage, then archive and reopen it.
            page.locator("#new-workflow").click()
            form = page.locator("#create-form")
            form.locator('input[name="business"]').fill("帮助小型咖啡馆提高复购")
            form.locator('input[name="product"]').fill(workflow_title)
            form.locator('textarea[name="audience"]').fill("本地咖啡馆经营者")
            form.locator('input[name="platform"]').fill("短视频平台")
            form.locator('input[name="duration_seconds"]').fill("30")
            form.locator('input[name="style"]').fill("真实、温暖")
            form.locator('button[type="submit"]').click()
            expect(page.locator("#generate-stage")).to_be_visible()
            page.locator("#generate-stage").click()
            expect(page.locator("#stage-content")).to_have_value("模拟阶段草稿：咖啡馆会员工具的创作内容。")
            page.locator("#approve-stage").click()
            expect(page.locator(".project-state")).to_contain_text("当前步骤：视觉设定")
            workflow_entry = page.locator(".project-entry").filter(has_text=workflow_title)
            workflow_id = workflow_entry.locator(".project-link").get_attribute("data-project-id")
            workflow_entry.locator("[data-archive-project]").click()
            page.locator("#confirm-accept").click()
            page.locator("#open-records").click()
            record_card = page.locator("[data-record-card]").filter(has_text=workflow_title)
            expect(record_card).to_be_visible()
            record_card.locator(".record-open-button").click()
            expect(page.locator("#record-detail-title")).to_have_text(workflow_title)
            page.locator("#continue-production").click()
            expect(page.locator(".project-state")).to_contain_text("当前步骤：视觉设定")
            assert page.request.get(f"{base}/api/projects/{workflow_id}").json()["archived"] is True

            # Verify the real responsive layout at a narrow mobile viewport.
            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#new-project").click()
            page.locator("#chat-file-input").set_input_files(file_payload)
            expect(page.locator(".chat-pending-file")).to_be_visible()
            dimensions = page.evaluate(
                "({viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth})"
            )
            assert dimensions["document"] <= dimensions["viewport"] + 1, dimensions
            assert dimensions["body"] <= dimensions["viewport"] + 1, dimensions
            assert not external_requests, external_requests
            assert not page_errors, page_errors
            assert not http_errors, http_errors
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)

    assert not server_thread.is_alive(), "Isolated E2E server did not stop cleanly"

from __future__ import annotations

import json
import socket
import threading
import time
import zipfile
from datetime import datetime, timezone
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
        return "模拟阶段草稿：咖啡馆会员工具的创作内容。"

    def chat(self, *, messages: list[dict], tools=None) -> ChatResult:
        return ChatResult(text="模拟助手回复：已收到并处理本轮内容。")


class FakeAsyncVideoProvider:
    ready = True

    def __init__(self):
        self.submissions = []
        self.polls = []

    def submit_video(self, *, project):
        self.submissions.append(project)
        return {"provider_task_id": f"provider-e2e-{len(self.submissions)}", "task_id": f"task-e2e-{len(self.submissions)}"}

    def poll_video(self, *, provider_task_id):
        self.polls.append(provider_task_id)
        return {"status": "processing", "provider_status": "processing"}


def start_isolated_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    video_provider=None,
    daily_limit: int = 0,
):
    monkeypatch.setattr("app.services.chat.create_text_provider", lambda profile, api_key: FakeTextProvider())
    monkeypatch.setattr("app.services.workflow.create_text_provider", lambda profile, api_key: FakeTextProvider())
    catalog_path = ROOT / "config" / "models.json"
    catalog = ModelCatalog.load(catalog_path)
    settings = Settings(
        database_path=tmp_path / "e2e.db",
        model_catalog_path=catalog_path,
        model_catalog=catalog,
        api_keys={profile.api_key_env: "e2e-no-network" for profile in catalog.profiles.values()},
        media_generation_daily_limit=daily_limit,
        video_job_timeout_seconds=60,
    )
    application = create_app(settings=settings, video_provider=video_provider)

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
    return f"http://127.0.0.1:{port}", server, thread, application


def test_browser_chat_workflow_archive_and_mobile_regression(tmp_path, monkeypatch):
    base, server, server_thread, _application = start_isolated_app(tmp_path, monkeypatch)
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
            blocked_backup = page.request.get(
                f"{base}/api/backup/export",
                headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
            )
            assert blocked_backup.status == 403
            with page.expect_download() as backup_download_info:
                page.locator("#export-backup").click()
            backup_download = backup_download_info.value
            assert backup_download.suggested_filename.startswith("agent-platform-backup-")
            with zipfile.ZipFile(backup_download.path()) as backup_archive:
                backup_names = set(backup_archive.namelist())
                assert "agent-platform.db" in backup_names
                assert any(name.startswith("media/") for name in backup_names)
                backup_manifest = json.loads(backup_archive.read("manifest.json"))
                assert backup_manifest["credentials_included"] is False
            expect(page.locator("#backup-export-status")).to_contain_text("正在生成备份")
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


def test_browser_series_assets_and_episode_setup(tmp_path, monkeypatch):
    base, server, server_thread, application = start_isolated_app(tmp_path, monkeypatch)
    page_errors: list[str] = []
    http_errors: list[tuple[int, str]] = []
    external_requests: list[str] = []
    series_title = "E2E 雾港故事"
    episode_title = "E2E 第一集：来信"

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

            page.get_by_role("button", name="剧集资产").click()
            expect(page.get_by_role("heading", name="剧集与创作资产")).to_be_visible()
            page.get_by_role("button", name="新建系列").click()
            create_series = page.locator("#series-create-form")
            create_series.locator('[name="title"]').fill(series_title)
            create_series.locator('[name="logline"]').fill("一封迟到的信，牵出港口小镇的旧秘密。")
            create_series.locator('[name="visual_style"]').fill("潮湿海雾、低饱和青绿、手持电影感。")
            create_series.locator('button[type="submit"]').click()
            expect(page.locator(".series-detail-heading h1")).to_have_text(series_title)
            series_id = next(item["id"] for item in application.state.series.list_series() if item["title"] == series_title)
            page.locator("#series-world-setting").fill("雾港旧仓库每逢大潮都会封闭。")
            page.locator("#series-central-theme").fill("记忆与证词的可信度。")
            page.locator("#series-update-form").locator('button[type="submit"]').click()
            expect(page.locator("#notice")).to_have_text("系列设定已保存。")
            continuity_form = page.locator("#new-continuity-entry")
            continuity_form.locator('[name="kind"]').select_option("open_thread")
            continuity_form.locator('[name="status"]').select_option("open")
            continuity_form.locator('[name="summary"]').fill("旧仓库的潮汐封锁与姐姐失踪之间存在联系。")
            with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith(f"/api/series/{series_id}/continuity")) as continuity_response:
                continuity_form.locator('button[type="submit"]').click()
            assert continuity_response.value.status == 201, continuity_response.value.text()
            assert application.state.series.get_series(series_id)["continuity_entries"]
            continuity_entry = page.locator(".continuity-entry").filter(has_text="旧仓库的潮汐封锁")
            expect(continuity_entry).to_be_visible()
            continuity_entry.locator('[name="summary"]').fill("旧仓库的封锁记录可能被人修改。")
            continuity_entry.locator('[name="status"]').select_option("resolved")
            continuity_entry.locator('button[type="submit"]').click()
            expect(page.locator(".continuity-entry").filter(has_text="旧仓库的封锁记录可能被人修改。")).to_be_visible()
            expect(page.locator(".continuity-entry details")).to_contain_text("旧仓库的潮汐封锁与姐姐失踪之间存在联系。")

            page.locator("#show-series-asset-form").click()
            asset_form = page.locator("#new-series-asset-form")
            asset_form.locator('[name="kind"]').select_option("character")
            asset_form.locator('[name="name"]').fill("林雾")
            asset_form.locator('[name="description"]').fill("港口档案员，习惯先观察再开口。")
            asset_form.locator('[name="visual_prompt"]').fill("短黑发，墨绿色旧风衣，左眉浅疤。")
            asset_form.locator('[name="continuity_notes"]').fill("旧银色怀表始终放在外套内袋。")
            asset_form.locator('button[type="submit"]').click()
            asset_item = page.locator(".series-asset-item").filter(has_text="林雾")
            expect(asset_item).to_be_visible()
            asset_item.locator("summary").click()
            asset_edit = asset_item.locator("form")
            asset_edit.locator('[name="continuity_notes"]').fill("怀表放在外套内袋；右手腕没有饰品。")
            asset_edit.locator('[name="archived"]').check()
            asset_edit.locator('button[type="submit"]').click()
            expect(page.locator(".series-asset-item").filter(has_text="已归档")).to_be_visible()
            stored_series = application.state.series.get_series(series_id)
            stored_asset = next(asset for asset in stored_series["assets"] if asset["name"] == "林雾")
            assert stored_asset["archived"] is True
            assert stored_asset["continuity_notes"] == "怀表放在外套内袋；右手腕没有饰品。"
            page.set_viewport_size({"width": 390, "height": 844})
            dimensions = page.evaluate("({viewport: innerWidth, document: document.documentElement.scrollWidth})")
            assert dimensions["document"] <= dimensions["viewport"] + 1, dimensions

            page.locator("#new-series-episode").click()
            setup_dimensions = page.evaluate("({viewport: innerWidth, document: document.documentElement.scrollWidth})")
            assert setup_dimensions["document"] <= setup_dimensions["viewport"] + 1, setup_dimensions
            form = page.locator("#create-form")
            form.locator('[name="business"]').fill("雾港小镇的旧秘密与一封迟来的信")
            form.locator('[name="product"]').fill(episode_title)
            form.locator('[name="audience"]').fill("喜欢悬疑、慢节奏故事的观众")
            expect(page.locator("#series-association")).to_be_visible()
            expect(form.locator('[name="series_id"]')).to_have_value(stored_series["id"])
            form.locator('[name="track"]').select_option("marketing")
            expect(page.locator("#series-association")).to_be_hidden()
            form.locator('[name="track"]').select_option("drama")
            expect(page.locator("#series-association")).to_be_visible()
            assert form.locator('[name="business"]').input_value() == "雾港小镇的旧秘密与一封迟来的信"
            assert form.locator('[name="product"]').input_value() == episode_title
            assert form.locator('[name="audience"]').input_value() == "喜欢悬疑、慢节奏故事的观众"
            form.locator('[name="episode_number"]').fill("1")
            form.locator('button[type="submit"]').click()
            expect(page.locator(".project-heading h1")).to_contain_text(episode_title)
            project_id = (
                page.locator(".project-entry").filter(has_text=episode_title)
                .locator(".project-link").get_attribute("data-project-id")
            )
            created_project = application.state.workflow.store.get(project_id)
            assert created_project["track"] == "drama"
            assert created_project["series_id"] == stored_series["id"]
            assert created_project["episode_number"] == 1

            page.locator("#open-series").click()
            page.locator(".series-list-row").filter(has_text=series_title).locator("[data-open-series]").click()
            episode_button = page.locator("[data-open-episode]").filter(has_text=episode_title)
            expect(episode_button).to_contain_text("EP01")
            episode_button.click()
            expect(page.locator("#record-detail-title")).to_have_text(episode_title)
            assert not external_requests, external_requests
            assert not page_errors, page_errors
            assert not http_errors, http_errors
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)

    assert not server_thread.is_alive(), "Isolated E2E server did not stop cleanly"


def test_browser_video_job_controls_and_daily_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.video_jobs.VideoJobWorker.start", lambda self: None)
    video_provider = FakeAsyncVideoProvider()
    base, server, server_thread, application = start_isolated_app(
        tmp_path, monkeypatch, video_provider=video_provider, daily_limit=1
    )
    page_errors: list[str] = []
    http_errors: list[tuple[int, str]] = []
    external_requests: list[str] = []
    workflow_title = "E2E 视频任务控制"

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
            page.locator("#new-workflow").click()
            form = page.locator("#create-form")
            form.locator('input[name="business"]').fill("帮助小型咖啡馆提高复购")
            form.locator('input[name="product"]').fill(workflow_title)
            form.locator('textarea[name="audience"]').fill("本地咖啡馆经营者")
            form.locator('input[name="platform"]').fill("短视频平台")
            form.locator('input[name="duration_seconds"]').fill("30")
            form.locator('input[name="style"]').fill("真实、温暖")
            form.locator('button[type="submit"]').click()

            for next_stage in ("视觉设定", "分镜", "镜头任务", "视频片段"):
                page.locator("#generate-stage").click()
                expect(page.locator("#stage-content")).not_to_have_value("")
                page.locator("#approve-stage").click()
                expect(page.locator(".project-state")).to_contain_text(f"当前步骤：{next_stage}")

            expect(page.locator(".production-shot-card")).to_have_count(3)
            page.locator('[data-shot-action="video"][data-shot-id="S01"]').click()
            cancel_button = page.locator('[data-shot-action="cancel-video"][data-shot-id="S01"]')
            expect(cancel_button).to_be_visible()
            cancel_button.click()
            expect(page.locator("#confirm-message")).to_contain_text("只会取消尚未提交给供应商")
            page.locator("#confirm-accept").click()
            shot_one = page.locator(".production-shot-card").filter(has_text="S01")
            expect(shot_one.locator(".shot-status")).to_have_text("本地队列已取消")
            assert video_provider.submissions == []

            page.locator('[data-shot-action="video"][data-shot-id="S02"]').click()
            expect(page.locator('[data-shot-action="cancel-video"][data-shot-id="S02"]')).to_be_visible()
            project_id = page.locator(".project-entry").filter(has_text=workflow_title).locator(".project-link").get_attribute("data-project-id")
            project = application.state.workflow.store.get(project_id)
            shot_two = next(item for item in project["workflow"]["video"]["shots"] if item["shot_id"] == "S02")
            job = application.state.workflow.store.get_video_job(shot_two["video"]["job_id"])
            timed_out_at = datetime.now(timezone.utc).isoformat()
            application.state.workflow._update_video_job(
                job,
                {
                    "status": "timed_out",
                    "provider_task_id": "provider-e2e-timeout",
                    "task_id": "task-e2e-timeout",
                    "next_poll_at": None,
                    "timed_out_at": timed_out_at,
                    "error": "本地监控期限已到",
                    "error_category": "provider_timeout",
                },
                asset_updates={
                    "status": "timed_out",
                    "task_id": "task-e2e-timeout",
                    "completed_at": timed_out_at,
                    "provider_status": "超过本地监控期限",
                    "error": "本地监控期限已到",
                    "error_category": "provider_timeout",
                },
            )
            page.locator("#open-records").click()
            record_card = page.locator("[data-record-card]").filter(has_text=workflow_title)
            expect(record_card).to_be_visible()
            record_card.locator(".record-open-button").click()
            page.locator("#continue-production").click()
            resume_button = page.locator('[data-shot-action="resume-video"][data-shot-id="S02"]')
            expect(resume_button).to_be_visible()
            resume_button.click()
            expect(page.get_by_text("已恢复状态查询；不会重新提交生成任务。", exact=True)).to_be_visible()
            assert video_provider.submissions == []
            assert video_provider.polls == []

            page.locator('[data-shot-action="video"][data-shot-id="S03"]').click()
            expect(page.get_by_text("已达到本地每日媒体生成上限", exact=False)).to_be_visible()
            assert len(http_errors) == 1 and http_errors[0][0] == 429, http_errors
            assert not external_requests, external_requests
            assert not page_errors, page_errors
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)

    assert not server_thread.is_alive(), "Isolated E2E server did not stop cleanly"
    assert video_provider.submissions == []
    assert video_provider.polls == []


def test_browser_series_copy_quality_revisions_and_compatibility_diagnostics(tmp_path, monkeypatch):
    base, server, server_thread, application = start_isolated_app(tmp_path, monkeypatch)
    page_errors: list[str] = []
    http_errors: list[tuple[int, str]] = []
    external_requests: list[str] = []
    series_title = "E2E 系列迁移"
    workflow_title = "E2E 草稿修订"

    def guard_route(route):
        if route.request.url.startswith(base):
            route.continue_()
        else:
            external_requests.append(route.request.url)
            route.abort()

    try:
        with sync_playwright() as playwright_api:
            browser = playwright_api.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000}, accept_downloads=True
            )
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
            expect(page.locator("#compatibility-status")).to_be_hidden()
            expect(page.locator("#chat-input")).to_be_visible()
            page.evaluate("showCompatibilityStatus({api_contract_version: 1, features: [], video_job_worker_running: false})")
            expect(page.locator("#compatibility-status")).to_contain_text("接口版本不兼容")
            expect(page.locator("#compatibility-status")).to_contain_text("后台工作线程未运行")
            page.evaluate("showCompatibilityStatus({api_contract_version: 3, features: ['series', 'series_transfer', 'story_continuity_ledger', 'quality_checks', 'video_jobs', 'content_revisions', 'production_delivery'], video_job_worker_running: true})")
            expect(page.locator("#compatibility-status")).to_be_hidden()

            created_series = page.request.post(
                f"{base}/api/series", data={"title": series_title, "logline": "一封信改变了港口小镇。"}
            )
            assert created_series.status == 201, created_series.text()
            series = created_series.json()
            asset_response = page.request.post(
                f"{base}/api/series/{series['id']}/assets",
                data={"kind": "character", "name": "林澄", "description": "正在寻找姐姐。"},
            )
            assert asset_response.status == 201, asset_response.text()
            media_url = application.state.assets.save(b"browser series media", ".png")
            episode = application.state.workflow.create_project({
                "track": "drama", "business": "寻找失踪的姐姐。", "product": "第一集",
                "audience": "悬疑短剧观众", "platform": "竖屏短剧", "style": "克制写实",
                "duration_seconds": 60, "series_id": series["id"], "episode_number": 1,
            })
            episode["workflow"]["script"]["reference_image"] = media_url
            application.state.workflow.store.save(episode)

            page.locator("#open-series").click()
            page.locator(".series-list-row").filter(has_text=series_title).locator("[data-open-series]").click()
            expect(page.locator(".series-detail-heading h1")).to_have_text(series_title)
            with page.expect_download() as download_info:
                page.locator("#export-series").click()
            bundle_path = download_info.value.path()
            assert bundle_path
            with zipfile.ZipFile(bundle_path) as archive:
                names = set(archive.namelist())
                assert "series.json" in names
                assert "episodes/0001/project.json" in names
                assert any(name.startswith("media/") for name in names)
            bundle_bytes = Path(bundle_path).read_bytes()

            page.locator("#back-to-series-list").click()
            page.locator("#series-import-file").set_input_files({
                "name": "series-copy.zip", "mimeType": "application/zip", "buffer": bundle_bytes,
            })
            expect(page.locator("#confirm-dialog")).to_be_visible()
            page.locator("#confirm-accept").click()
            expect(page.locator(".series-detail-heading h1")).to_contain_text("导入副本")
            imported_series = next(
                item for item in application.state.series.list_series()
                if item["title"].endswith("（导入副本）")
            )
            imported_detail = application.state.series.get_series(imported_series["id"])
            assert len(imported_detail["episodes"]) == 1
            assert len(imported_detail["assets"]) == 1
            imported_episode = imported_detail["episodes"][0]
            copied_media = imported_episode["workflow"]["script"]["reference_image"]
            assert copied_media != media_url
            assert (application.state.assets.directory / Path(copied_media).name).read_bytes() == b"browser series media"

            page.locator("#new-workflow").click()
            form = page.locator("#create-form")
            form.locator('[name="business"]').fill("帮助咖啡馆提高复购")
            form.locator('[name="product"]').fill(workflow_title)
            form.locator('[name="audience"]').fill("本地咖啡馆经营者")
            form.locator('[name="platform"]').fill("短视频平台")
            form.locator('[name="duration_seconds"]').fill("30")
            form.locator('[name="style"]').fill("真实、温暖")
            form.locator('button[type="submit"]').click()
            page.locator("#generate-stage").click()
            original = "模拟阶段草稿：咖啡馆会员工具的创作内容。"
            expect(page.locator("#stage-content")).to_have_value(original)
            edited = "第二版：主角在雨夜抵达港口，发现一张旧照片；日期待确认，先不补写未核实的事实。"
            page.locator("#stage-content").fill(edited)
            page.locator("#save-content").click()
            expect(page.locator(".stage-revision-history summary")).to_contain_text("历史草稿（1）")
            page.locator("#run-quality-check").click()
            expect(page.locator("#quality-results")).to_contain_text("发现待补充占位内容")
            page.locator(".stage-revision-history summary").click()
            page.locator("[data-revision-restore]").first.click()
            expect(page.locator("#confirm-message")).to_contain_text("当前内容会先归档")
            page.locator("#confirm-accept").click()
            expect(page.locator("#stage-content")).to_have_value(original)
            workflow_id = page.locator(".project-entry").filter(has_text=workflow_title).locator(".project-link").get_attribute("data-project-id")
            restored_project = application.state.workflow.store.get(workflow_id)
            assert restored_project["workflow"]["script"]["content"] == original
            assert restored_project["workflow"]["script"]["revisions"][-1]["content"] == edited

            page.locator("#open-usage").click()
            expect(page.locator("#video-jobs-title")).to_be_visible()
            expect(page.locator("#video-jobs-summary")).to_contain_text("后台工作线程运行中")
            page.locator("#close-usage").click()
            assert not external_requests, external_requests
            assert not page_errors, page_errors
            assert not http_errors, http_errors
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)

    assert not server_thread.is_alive(), "Isolated E2E server did not stop cleanly"


def test_browser_project_copy_export_import(tmp_path, monkeypatch):
    base, server, server_thread, application = start_isolated_app(tmp_path, monkeypatch)
    project_title = "E2E 项目副本迁移"
    project = application.state.workflow.create_project({
        "mode": "workflow",
        "track": "marketing",
        "business": "帮助咖啡馆提高复购",
        "product": project_title,
        "audience": "本地咖啡馆经营者",
        "platform": "短视频平台",
        "duration_seconds": 30,
        "style": "真实、温暖",
    })
    page_errors: list[str] = []
    http_errors: list[tuple[int, str]] = []
    external_requests: list[str] = []

    def guard_route(route):
        if route.request.url.startswith(base):
            route.continue_()
        else:
            external_requests.append(route.request.url)
            route.abort()

    try:
        with sync_playwright() as playwright_api:
            browser = playwright_api.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
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
            page.locator("#open-records").click()
            card = page.locator("[data-record-card]").filter(has_text=project_title)
            expect(card).to_be_visible()
            card.locator(".record-open-button").click()
            expect(page.locator("#record-detail-title")).to_have_text(project_title)

            expect(page.get_by_role("link", name="下载制作交付包")).to_be_visible()
            with page.expect_download() as delivery_info:
                page.get_by_role("link", name="下载制作交付包").click()
            delivery_download = delivery_info.value
            delivery_path = tmp_path / "production-delivery.zip"
            delivery_download.save_as(delivery_path)
            with zipfile.ZipFile(delivery_path) as delivery_archive:
                assert "README.md" in delivery_archive.namelist()
                assert "production-report.json" in delivery_archive.namelist()

            with page.expect_download() as download_info:
                page.get_by_role("link", name="导出项目副本").click()
            download = download_info.value
            archive_path = tmp_path / "project-copy.zip"
            download.save_as(archive_path)
            assert archive_path.is_file() and archive_path.stat().st_size > 0

            page.locator("#back-to-records").click()
            page.locator("#project-import-file").set_input_files({
                "name": "project-copy.zip",
                "mimeType": "application/zip",
                "buffer": archive_path.read_bytes(),
            })
            expect(page.locator("#confirm-title")).to_have_text("导入为新的项目副本？")
            page.locator("#confirm-accept").click()
            expect(page.locator("#record-detail-title")).to_have_text(f"{project_title}（导入副本）")
            expect(page.locator("#notice")).to_contain_text("已导入新的项目副本")
            imported_projects = [
                item for item in application.state.workflow.store.list()
                if item["title"].endswith("（导入副本）")
            ]
            assert len(imported_projects) == 1
            imported = application.state.workflow.get_project(imported_projects[0]["id"])
            assert imported["id"] != project["id"]
            assert imported["title"].endswith("（导入副本）")
            assert not external_requests, external_requests
            assert not page_errors, page_errors
            assert not http_errors, http_errors
            browser.close()
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)

    assert not server_thread.is_alive(), "Isolated E2E server did not stop cleanly"

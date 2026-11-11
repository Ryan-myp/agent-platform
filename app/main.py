from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from app.config import Settings
from app.models import (
    ChatRequest,
    ChatSessionCreate,
    ContentUpdate,
    GenerateRequest,
    EpisodeOutlineUpdate,
    ShotAssetsUpdate,
    RenderOrderUpdate,
    NovelCreate,
    NovelUpdate,
    NovelChapterCreate,
    NovelChapterUpdate,
    NovelChapterGenerateRequest,
    ModelCatalogUpdate,
    ModelUpdate,
    ProjectCreate,
    ProjectTitleUpdate,
    ReviewCreate,
    StoryboardCanvasUpdate,
    AssemblySettingsUpdate,
    SeriesAssetCreate,
    SeriesAssetUpdate,
    SeriesCreate,
    SeriesContinuityCreate,
    SeriesContinuityUpdate,
    SeriesUpdate,
    StorageCleanupRequest,
)
from app.providers.media import AssetStore, ImageProvider, MediaProviderError, VideoProvider, create_audio_provider
from app.providers.catalog import MODALITIES, SUPPORTED_ADAPTERS, ModelCatalog, ModelProfile
from app.providers.credentials import ModelCredentialStore
from app.services.attachments import (
    MAX_TOTAL_ATTACHMENT_BYTES,
    ChatAttachmentError,
    ChatAttachmentService,
)
from app.services.backup import BackupError, create_backup_archive
from app.services.chat import ChatService
from app.services.workflow import WorkflowError, WorkflowService
from app.services.video_jobs import VideoJobWorker
from app.services.usage import UsageService
from app.services.series import SeriesError, SeriesService
from app.services.project_transfer import ProjectTransferError, ProjectTransferService
from app.services.quality import QualityService
from app.services.delivery import DeliveryPackageError, DeliveryService
from app.services.series_transfer import SeriesTransferService
from app.services.novels import NovelError, NovelService
from app.services.video_assembly import VideoAssemblyError, VideoAssemblyService
from app.services.reviews import ReviewError, ReviewService
from app.services.storyboard_canvas import StoryboardCanvasError, StoryboardCanvasService
from app.skills.loader import SkillLoader
from app.store import ProjectStore

ROOT = Path(__file__).resolve().parent.parent
API_CONTRACT_VERSION = 7
_SECRET_PARAM_NAMES = {"api_key", "apikey", "authorization", "access_token", "refresh_token", "secret"}


def _contains_secret_param(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower().replace("-", "_") in _SECRET_PARAM_NAMES
            or _contains_secret_param(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret_param(item) for item in value)
    return False


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_same_origin_request(request: Request) -> bool:
    host = request.headers.get("host", "").lower()
    origin = request.headers.get("origin")
    if origin:
        parsed_origin = urlparse(origin)
        return parsed_origin.scheme == request.url.scheme and parsed_origin.netloc.lower() == host
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site:
        return fetch_site.lower() == "same-origin"
    referer = request.headers.get("referer")
    if referer:
        parsed_referer = urlparse(referer)
        return parsed_referer.scheme == request.url.scheme and parsed_referer.netloc.lower() == host
    return False


def _build_model_catalog(
    payload: ModelCatalogUpdate, existing: ModelCatalog | None = None
) -> ModelCatalog:
    profiles: list[ModelProfile] = []
    for item in payload.models:
        data = item.model_dump(exclude={"api_key"})
        data = {key: value.strip() if isinstance(value, str) else value for key, value in data.items()}
        if not data["name"] or not data["model_id"]:
            raise ValueError("模型名称和上游 model_id 不能为空。")
        if data["adapter"] not in SUPPORTED_ADAPTERS[data["modality"]]:
            choices = ", ".join(sorted(SUPPORTED_ADAPTERS[data["modality"]]))
            raise ValueError(f"{data['modality']} 暂不支持适配器 {data['adapter']}；可用：{choices}")
        if not data["api_key_env"] and existing and data["id"] in existing.profiles:
            data["api_key_env"] = existing.profiles[data["id"]].api_key_env
        if not data["api_key_env"]:
            safe_id = re.sub(r"[^A-Z0-9]+", "_", data["id"].upper()).strip("_")
            data["api_key_env"] = f"MODEL_{safe_id}_API_KEY"
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", data["api_key_env"]):
            raise ValueError("模型凭据标识无效。")
        if data["adapter"] == "anthropic":
            if data["base_url"]:
                raise ValueError("Anthropic SDK 使用官方端点；此适配器不支持自定义 base URL。")
        else:
            parsed = urlparse(data["base_url"])
            if parsed.scheme not in {"https", "http"} or not parsed.hostname:
                raise ValueError("base URL 必须是有效的 HTTPS 地址；仅 localhost 可使用 HTTP。")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("base URL 不能包含账号、密码、查询参数或片段。")
            if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
                raise ValueError("非本机服务请使用 HTTPS；HTTP 仅允许 localhost。")
        try:
            encoded_params = json.dumps(data["params"], ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("模型参数必须是有效的 JSON 对象。") from error
        if len(encoded_params) > 16000:
            raise ValueError("模型参数不能超过 16 KB。")
        if _contains_secret_param(data["params"]):
            raise ValueError("模型参数中不能保存 API Key、Token 或 Authorization。")
        profiles.append(ModelProfile.from_dict(data))

    defaults = {modality: payload.defaults.get(modality, "") for modality in MODALITIES}
    if not defaults["text"]:
        raise ValueError("请为文本模型设置一个默认模型。")
    return ModelCatalog(profiles, defaults)


def _write_model_catalog(path: Path, catalog: ModelCatalog) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(catalog.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(serialized)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def create_app(
    *,
    database_path: Path | None = None,
    settings: Settings | None = None,
    video_provider: VideoProvider | None = None,
    image_provider: ImageProvider | None = None,
) -> FastAPI:
    current_settings = settings or Settings.from_env()
    if database_path is not None:
        current_settings = replace(current_settings, database_path=database_path)
    credential_store = ModelCredentialStore(current_settings.database_path.parent / "model-credentials.json")
    stored_credentials = credential_store.load()
    model_credentials: dict[str, str] = {}
    for profile in current_settings.model_catalog.profiles.values():
        if profile.id in stored_credentials:
            model_credentials[profile.id] = stored_credentials[profile.id]
        elif profile.api_key_env in stored_credentials:
            # Migrate credentials from the earlier environment-name keyed format.
            model_credentials[profile.id] = stored_credentials[profile.api_key_env]
    current_settings = replace(current_settings, model_credentials=model_credentials)
    store = ProjectStore(current_settings.database_path)
    skill_loader = SkillLoader(ROOT / "skills")
    media_path = current_settings.database_path.parent / "media"
    assets = AssetStore(media_path)
    usage = UsageService(store, current_settings.media_generation_daily_limit)
    workflow = WorkflowService(store, current_settings, skill_loader, video_provider, image_provider, assets, usage)
    video_job_worker = VideoJobWorker(workflow, store)
    workflow.video_job_worker = video_job_worker
    chat_attachments = ChatAttachmentService(store, assets)
    chat = ChatService(store, current_settings, assets, chat_attachments, usage)
    series = SeriesService(store)
    project_transfer = ProjectTransferService(store, assets)
    delivery = DeliveryService(store, assets)
    quality = QualityService(store)
    series_transfer = SeriesTransferService(store, project_transfer)
    novels = NovelService(store, current_settings, usage)
    video_assembly = VideoAssemblyService(store, assets)
    reviews = ReviewService(store)
    storyboard_canvas = StoryboardCanvasService(store)

    server_started_at: str | None = datetime.now(timezone.utc).isoformat()
    server_instance_id = uuid4().hex[:12]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal server_started_at
        server_started_at = datetime.now(timezone.utc).isoformat()
        video_job_worker.start()
        try:
            yield
        finally:
            video_job_worker.stop()

    application = FastAPI(
        title="片场 · AI 视频创作台",
        version="0.2.0",
        description="支持多供应商多模态模型与对话式创作的内容平台。",
        lifespan=lifespan,
    )
    application.state.workflow = workflow
    application.state.chat = chat
    application.state.chat_attachments = chat_attachments
    application.state.usage = usage
    application.state.series = series
    application.state.assets = assets
    application.state.project_transfer = project_transfer
    application.state.delivery = delivery
    application.state.quality = quality
    application.state.series_transfer = series_transfer
    application.state.novels = novels
    application.state.video_assembly = video_assembly
    application.state.reviews = reviews
    application.state.storyboard_canvas = storyboard_canvas
    application.state.model_credentials = credential_store
    application.state.video_job_worker = video_job_worker
    catalog_update_lock = threading.Lock()

    @application.exception_handler(RequestValidationError)
    async def safe_model_config_validation_error(request: Request, error: RequestValidationError):
        if request.url.path == "/api/config/models":
            details = [
                {key: item[key] for key in ("loc", "msg", "type") if key in item}
                for item in error.errors()
            ]
            return JSONResponse(status_code=422, content={"detail": details})
        return await request_validation_exception_handler(request, error)

    @application.middleware("http")
    async def reject_cross_origin_writes(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site", "").lower()
            if (origin and not _is_same_origin_request(request)) or fetch_site == "cross-site":
                return JSONResponse(status_code=403, content={"detail": "跨来源写入请求已拒绝。"})
        return await call_next(request)

    @application.middleware("http")
    async def protect_local_media(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/media/"):
            response.headers["X-Content-Type-Options"] = "nosniff"
            if Path(request.url.path).suffix.lower() in {
                ".html", ".htm", ".xhtml", ".svg", ".xml", ".json", ".yaml", ".yml",
                ".txt", ".md", ".csv", ".log", ".pdf", ".docx", ".js", ".mjs", ".css",
            }:
                response.headers["Content-Disposition"] = "attachment"
                response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response

    application.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
    application.mount("/media", StaticFiles(directory=media_path, check_dir=False), name="media")

    def run(action, *args: Any, **kwargs: Any):
        try:
            return action(*args, **kwargs)
        except (WorkflowError, SeriesError, NovelError, VideoAssemblyError) as error:
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error

    @application.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(
            ROOT / "app" / "static" / "index.html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    @application.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "app_version": application.version,
            "api_contract_version": API_CONTRACT_VERSION,
            "server_started_at": server_started_at,
            "server_instance_id": server_instance_id,
        }

    @application.get("/api/system/status")
    def system_status() -> dict[str, Any]:
        return {
            "status": "ok",
            "app_version": application.version,
            "api_contract_version": API_CONTRACT_VERSION,
            "server_started_at": server_started_at,
            "server_instance_id": server_instance_id,
            "python_version": sys.version.split()[0],
            "features": ["series", "series_transfer", "story_continuity_ledger", "episode_outlines", "longform_novels", "shot_asset_links", "local_video_assembly", "local_subtitles", "assembly_presets", "local_visual_assets", "quality_checks", "story_quality_review", "scheduled_backups", "video_jobs", "content_revisions", "production_delivery", "production_reviews", "manual_storyboard_canvas"],
            "video_job_worker_running": video_job_worker.is_running,
        }

    @application.get("/api/config")
    def get_config() -> dict:
        return workflow.config_view()

    @application.get("/api/backup/export")
    def export_backup(request: Request) -> FileResponse:
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host) or not _is_same_origin_request(request):
            raise HTTPException(status_code=403, detail="备份导出仅允许从本机工作台发起。")
        temp_directory = Path(tempfile.mkdtemp(prefix="agent-platform-export-"))
        filename = f"agent-platform-backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip"
        output_path = temp_directory / filename
        try:
            create_backup_archive(current_settings.database_path, media_path, output_path)
        except BackupError as error:
            shutil.rmtree(temp_directory, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"创建备份失败：{error}") from error
        return FileResponse(
            output_path,
            media_type="application/zip",
            filename=filename,
            headers={"Cache-Control": "no-store, max-age=0"},
            background=BackgroundTask(shutil.rmtree, temp_directory, ignore_errors=True),
        )

    @application.get("/api/usage")
    def get_usage() -> dict:
        return usage.summary()

    @application.get("/api/video-jobs")
    def list_video_jobs() -> dict[str, Any]:
        statuses = {"queued", "submitting", "polling", "timed_out", "stalled", "interrupted", "failed"}
        projects = {item["id"]: item for item in store.list()}
        jobs = []
        for job in store.list_video_jobs(statuses):
            project = projects.get(job.get("project_id"), {})
            jobs.append({
                "id": job.get("id"),
                "project_id": job.get("project_id"),
                "project_title": project.get("title", "已删除项目"),
                "shot_id": job.get("shot_id"),
                "status": job.get("status"),
                "created_at": job.get("created_at"),
                "updated_at": job.get("updated_at"),
                "next_poll_at": job.get("next_poll_at"),
                "error": job.get("error"),
                "provider_task_id": job.get("provider_task_id"),
            })
        return {"worker_running": video_job_worker.is_running, "jobs": jobs[-100:]}

    @application.get("/api/storage/cleanup/preview")
    def preview_storage_cleanup() -> dict:
        return chat_attachments.preview_storage_cleanup()

    @application.post("/api/storage/cleanup")
    def confirm_storage_cleanup(payload: StorageCleanupRequest) -> dict:
        if not payload.confirm:
            raise HTTPException(status_code=409, detail="请先预览候选项并明确确认本地存储清理。")
        try:
            return chat_attachments.confirm_storage_cleanup(
                attachment_ids=payload.attachment_ids,
                storage_keys=payload.storage_keys,
            )
        except ChatAttachmentError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/api/config/models/{profile_id}/credential")
    def get_model_credential(profile_id: str, request: Request) -> JSONResponse:
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host):
            raise HTTPException(status_code=403, detail="API Key 仅允许在本机查看。")
        try:
            profile = workflow.settings.model_catalog.resolve(profile_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="找不到此模型配置。") from error
        return JSONResponse(
            {"api_key": workflow.settings.api_key_for(profile)},
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @application.put("/api/config/models")
    def update_model_catalog(payload: ModelCatalogUpdate, request: Request) -> dict:
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host):
            raise HTTPException(status_code=403, detail="模型配置仅允许在本机修改。")
        try:
            catalog = _build_model_catalog(payload, workflow.settings.model_catalog)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        with catalog_update_lock:
            for project in store.list():
                model_id = project.get("model")
                if not model_id:
                    continue
                try:
                    catalog.resolve(model_id, "text")
                except ValueError as error:
                    raise HTTPException(
                        status_code=409,
                        detail=f"项目“{project.get('title', project['id'])}”仍在使用文本模型 {model_id}；请先切换该项目模型。",
                    ) from error

            active_profile_ids = set(catalog.profiles)
            try:
                previous_credentials = credential_store.load()
            except RuntimeError as error:
                raise HTTPException(status_code=500, detail="无法读取本机密钥文件；模型配置未更改。") from error
            next_credentials: dict[str, str] = {}
            for profile in catalog.profiles.values():
                if profile.id in previous_credentials:
                    next_credentials[profile.id] = previous_credentials[profile.id]
                elif profile.api_key_env in previous_credentials:
                    next_credentials[profile.id] = previous_credentials[profile.api_key_env]
            for item in payload.models:
                if item.api_key is not None:
                    if item.api_key:
                        next_credentials[item.id] = item.api_key
                    else:
                        next_credentials.pop(item.id, None)
            next_credentials = {
                profile_id: value for profile_id, value in next_credentials.items()
                if profile_id in active_profile_ids
            }
            credentials_changed = next_credentials != previous_credentials
            old_catalog = workflow.settings.model_catalog
            try:
                _write_model_catalog(workflow.settings.model_catalog_path, catalog)
                if credentials_changed:
                    credential_store.save(next_credentials)
            except (OSError, RuntimeError) as error:
                try:
                    _write_model_catalog(workflow.settings.model_catalog_path, old_catalog)
                except OSError:
                    pass
                raise HTTPException(
                    status_code=500,
                    detail="无法安全保存模型配置或本机 API Key；请检查目录权限。",
                ) from error

            old_keys = workflow.settings.api_keys
            active_env_names = {profile.api_key_env for profile in catalog.profiles.values() if profile.api_key_env}
            api_keys = {
                env_name: old_keys.get(env_name, os.getenv(env_name, ""))
                for env_name in active_env_names
            }
            updated_settings = replace(
                workflow.settings,
                model_catalog=catalog,
                api_keys=api_keys,
                model_credentials=next_credentials,
            )
            workflow.settings = updated_settings
            chat.settings = updated_settings
            application.state.settings = updated_settings
        return workflow.config_view()

    @application.get("/api/skills")
    def get_skills() -> list[dict[str, str]]:
        return skill_loader.list_skills()

    @application.get("/api/series")
    def list_series() -> list[dict]:
        return series.list_series()

    @application.post("/api/series", status_code=201)
    def create_series(payload: SeriesCreate) -> dict:
        return run(series.create_series, payload.model_dump())

    @application.get("/api/series/{series_id}")
    def get_series(series_id: str) -> dict:
        return run(series.get_series, series_id)

    @application.get("/api/series/{series_id}/export")
    def export_series(series_id: str, request: Request) -> FileResponse:
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host) or not _is_same_origin_request(request):
            raise HTTPException(status_code=403, detail="系列导出仅允许从本机工作台发起。")
        temp_directory = Path(tempfile.mkdtemp(prefix="agent-platform-series-export-"))
        output_path = temp_directory / f"agent-platform-series-{series_id[:12]}.zip"
        try:
            series_transfer.create_export(series_id, output_path)
        except ProjectTransferError as error:
            shutil.rmtree(temp_directory, ignore_errors=True)
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error
        return FileResponse(
            output_path,
            media_type="application/zip",
            filename=output_path.name,
            headers={"Cache-Control": "no-store, max-age=0"},
            background=BackgroundTask(shutil.rmtree, temp_directory, ignore_errors=True),
        )

    @application.post("/api/series/import", status_code=201)
    def import_series(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host) or not _is_same_origin_request(request):
            raise HTTPException(status_code=403, detail="系列导入仅允许从本机工作台发起。")
        if file.size is not None and file.size > 512 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="系列导入包超过 512 MiB 限制。")
        try:
            return series_transfer.import_bundle(file.file)
        except ProjectTransferError as error:
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error

    @application.put("/api/series/{series_id}")
    def update_series(series_id: str, payload: SeriesUpdate) -> dict:
        return run(series.update_series, series_id, payload.model_dump(exclude_unset=True, exclude_none=True))

    @application.post("/api/series/{series_id}/continuity", status_code=201)
    def create_series_continuity(series_id: str, payload: SeriesContinuityCreate) -> dict:
        return run(series.create_continuity_entry, series_id, payload.model_dump())

    @application.put("/api/series/{series_id}/continuity/{entry_id}")
    def update_series_continuity(series_id: str, entry_id: str, payload: SeriesContinuityUpdate) -> dict:
        return run(
            series.update_continuity_entry,
            series_id,
            entry_id,
            payload.model_dump(exclude_unset=True, exclude_none=True),
        )

    @application.post("/api/series/{series_id}/assets", status_code=201)
    def create_series_asset(series_id: str, payload: SeriesAssetCreate) -> dict:
        return run(series.create_asset, series_id, payload.model_dump())

    @application.put("/api/series/{series_id}/assets/{asset_id}")
    def update_series_asset(series_id: str, asset_id: str, payload: SeriesAssetUpdate) -> dict:
        return run(
            series.update_asset,
            series_id,
            asset_id,
            payload.model_dump(exclude_unset=True, exclude_none=True),
        )

    @application.post("/api/series/{series_id}/assets/{asset_id}/media")
    async def upload_series_asset_media(
        series_id: str,
        asset_id: str,
        request: Request,
        files: list[UploadFile] = File(...),
    ) -> dict:
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host) or not _is_same_origin_request(request):
            raise HTTPException(status_code=403, detail="系列资产图片上传仅允许从本机工作台发起。")
        if len(files) > 8:
            raise HTTPException(status_code=413, detail="每次最多上传 8 张图片。")
        asset = store.get_series_asset(series_id, asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="找不到这个系列资产。")
        existing_media = asset.get("media") or []
        if len(existing_media) + len(files) > 8:
            raise HTTPException(status_code=413, detail="每个系列资产最多关联 8 张本地参考图。")
        created_urls: list[str] = []
        records: list[dict[str, Any]] = []
        try:
            for upload in files:
                content = await upload.read(20 * 1024 * 1024 + 1)
                if not content:
                    raise HTTPException(status_code=422, detail="不能上传空文件。")
                if len(content) > 20 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="单张参考图不能超过 20 MiB。")
                if content.startswith(b"\x89PNG\r\n\x1a\n"):
                    suffix, mime_type = ".png", "image/png"
                elif content.startswith(b"\xff\xd8\xff"):
                    suffix, mime_type = ".jpg", "image/jpeg"
                elif len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
                    suffix, mime_type = ".webp", "image/webp"
                else:
                    raise HTTPException(status_code=415, detail="仅支持有效的 PNG、JPEG 或 WebP 图片。")
                original_name = (upload.filename or "reference-image").replace("\\", "/").rsplit("/", 1)[-1]
                original_name = re.sub(r"[\x00-\x1f\x7f]", "", original_name).strip()[:180] or "reference-image"
                asset_url = assets.save(content, suffix)
                created_urls.append(asset_url)
                records.append({
                    "asset_url": asset_url,
                    "filename": original_name,
                    "mime_type": mime_type,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                })
            return run(series.add_asset_media, series_id, asset_id, records)
        except Exception:
            for asset_url in created_urls:
                (assets.directory / Path(asset_url).name).unlink(missing_ok=True)
            raise
        finally:
            for upload in files:
                await upload.close()

    @application.get("/api/novels")
    def list_novels() -> list[dict[str, Any]]:
        return novels.list_novels()

    @application.post("/api/novels", status_code=201)
    def create_novel(payload: NovelCreate) -> dict[str, Any]:
        return run(novels.create_novel, payload.model_dump())

    @application.get("/api/novels/{novel_id}")
    def get_novel(novel_id: str) -> dict[str, Any]:
        return run(novels.get_novel, novel_id)

    @application.put("/api/novels/{novel_id}")
    def update_novel(novel_id: str, payload: NovelUpdate) -> dict[str, Any]:
        return run(novels.update_novel, novel_id, payload.model_dump(exclude_unset=True, exclude_none=True))

    @application.post("/api/novels/{novel_id}/chapters", status_code=201)
    def create_novel_chapter(novel_id: str, payload: NovelChapterCreate) -> dict[str, Any]:
        return run(novels.create_chapter, novel_id, payload.model_dump())

    @application.put("/api/novels/{novel_id}/chapters/{chapter_id}")
    def update_novel_chapter(novel_id: str, chapter_id: str, payload: NovelChapterUpdate) -> dict[str, Any]:
        return run(
            novels.update_chapter, novel_id, chapter_id,
            payload.model_dump(exclude_unset=True, exclude_none=True),
        )

    @application.post("/api/novels/{novel_id}/chapters/{chapter_id}/generate")
    def generate_novel_chapter(
        novel_id: str, chapter_id: str, payload: NovelChapterGenerateRequest
    ) -> dict[str, Any]:
        return run(novels.generate_chapter, novel_id, chapter_id, kind=payload.kind, model=payload.model)

    @application.post("/api/novels/{novel_id}/chapters/{chapter_id}/approve")
    def approve_novel_chapter(novel_id: str, chapter_id: str) -> dict[str, Any]:
        return run(novels.approve_chapter, novel_id, chapter_id)

    @application.get("/api/projects")
    def list_projects() -> list[dict]:
        return [reviews.decorate(video_assembly.decorate(project), include_records=False) for project in workflow.list_projects()]

    @application.post("/api/projects", status_code=201)
    def create_project(payload: ProjectCreate) -> dict:
        return run(workflow.create_project, payload.model_dump())

    @application.post("/api/chat/sessions", status_code=201)
    def create_chat_session(payload: ChatSessionCreate) -> dict:
        return run(workflow.create_project, {**payload.model_dump(), "mode": "chat"})

    @application.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict:
        return reviews.decorate(video_assembly.decorate(run(workflow.get_project, project_id)))

    @application.put("/api/projects/{project_id}/storyboard-canvas")
    def update_storyboard_canvas(project_id: str, payload: StoryboardCanvasUpdate, request: Request) -> dict:
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host) or not _is_same_origin_request(request):
            raise HTTPException(status_code=403, detail="分镜画布仅允许从本机工作台保存。")
        try:
            project = storyboard_canvas.update(project_id, payload.expected_updated_at, payload.cards)
        except StoryboardCanvasError as error:
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error
        return reviews.decorate(video_assembly.decorate(project))

    @application.post("/api/projects/{project_id}/reviews", status_code=201)
    def create_project_review(project_id: str, payload: ReviewCreate, request: Request) -> dict:
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host) or not _is_same_origin_request(request):
            raise HTTPException(status_code=403, detail="审阅记录仅允许从本机工作台提交。")
        project = video_assembly.decorate(run(workflow.get_project, project_id))
        try:
            reviews.add_review(project, payload.model_dump())
        except ReviewError as error:
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error
        updated = video_assembly.decorate(run(workflow.get_project, project_id))
        return reviews.decorate(updated)

    @application.get("/api/projects/{project_id}/export")
    def export_project(project_id: str, request: Request) -> FileResponse:
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host) or not _is_same_origin_request(request):
            raise HTTPException(status_code=403, detail="项目导出仅允许从本机工作台发起。")
        temp_directory = Path(tempfile.mkdtemp(prefix="agent-platform-project-export-"))
        filename = f"agent-platform-project-{project_id[:12]}.zip"
        output_path = temp_directory / filename
        try:
            project_transfer.create_export(project_id, output_path)
        except ProjectTransferError as error:
            shutil.rmtree(temp_directory, ignore_errors=True)
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error
        return FileResponse(
            output_path,
            media_type="application/zip",
            filename=filename,
            headers={"Cache-Control": "no-store, max-age=0"},
            background=BackgroundTask(shutil.rmtree, temp_directory, ignore_errors=True),
        )

    @application.get("/api/projects/{project_id}/delivery")
    def export_delivery_package(project_id: str, request: Request) -> FileResponse:
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host) or not _is_same_origin_request(request):
            raise HTTPException(status_code=403, detail="制作交付包仅允许从本机工作台下载。")
        temp_directory = Path(tempfile.mkdtemp(prefix="agent-platform-delivery-"))
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", project_id)[:12] or "project"
        filename = f"agent-platform-delivery-{safe_id}.zip"
        output_path = temp_directory / filename
        try:
            delivery.create_package(project_id, output_path)
        except DeliveryPackageError as error:
            shutil.rmtree(temp_directory, ignore_errors=True)
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error
        return FileResponse(
            output_path,
            media_type="application/zip",
            filename=filename,
            headers={"Cache-Control": "no-store, max-age=0"},
            background=BackgroundTask(shutil.rmtree, temp_directory, ignore_errors=True),
        )

    @application.post("/api/projects/import", status_code=201)
    def import_project(request: Request, file: UploadFile = File(...)) -> dict:
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host) or not _is_same_origin_request(request):
            raise HTTPException(status_code=403, detail="项目导入仅允许从本机工作台发起。")
        if file.size is not None and file.size > 512 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="项目导入包超过 512 MiB 限制。")
        try:
            return project_transfer.import_bundle(file.file)
        except ProjectTransferError as error:
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error

    @application.put("/api/projects/{project_id}/title")
    def rename_project(project_id: str, payload: ProjectTitleUpdate) -> dict:
        return run(workflow.rename_project, project_id, payload.title)

    @application.post("/api/projects/{project_id}/archive")
    def archive_project(project_id: str) -> dict:
        return run(workflow.archive_project, project_id)

    @application.post("/api/projects/{project_id}/model")
    def update_model(project_id: str, payload: ModelUpdate) -> dict:
        return run(workflow.set_model, project_id, payload.model)

    @application.post("/api/projects/{project_id}/generate/{stage}")
    def generate(project_id: str, stage: str, payload: GenerateRequest | None = None) -> dict:
        return video_assembly.decorate(run(workflow.generate, project_id, stage, payload.model if payload else None))

    @application.put("/api/projects/{project_id}/episode-outline")
    def update_episode_outline(project_id: str, payload: EpisodeOutlineUpdate) -> dict:
        return video_assembly.decorate(run(workflow.update_episode_outline, project_id, payload.content, payload.status))

    @application.post("/api/projects/{project_id}/episode-outline/generate")
    def generate_episode_outline(project_id: str, payload: GenerateRequest | None = None) -> dict:
        return video_assembly.decorate(run(workflow.generate_episode_outline, project_id, payload.model if payload else None))

    @application.put("/api/projects/{project_id}/shots/{shot_id}/assets")
    def update_shot_assets(project_id: str, shot_id: str, payload: ShotAssetsUpdate) -> dict:
        return video_assembly.decorate(run(workflow.set_shot_assets, project_id, shot_id, payload.asset_ids))

    @application.put("/api/projects/{project_id}/render-order")
    def update_render_order(project_id: str, payload: RenderOrderUpdate) -> dict:
        return run(video_assembly.set_order, project_id, payload.shot_ids)

    @application.put("/api/projects/{project_id}/assembly-settings")
    def update_assembly_settings(project_id: str, payload: AssemblySettingsUpdate, request: Request) -> dict:
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host) or not _is_same_origin_request(request):
            raise HTTPException(status_code=403, detail="本地合成设置仅允许从本机工作台修改。")
        return run(video_assembly.update_settings, project_id, **payload.model_dump())

    @application.get("/api/projects/{project_id}/subtitles.{subtitle_format}")
    def download_project_subtitles(project_id: str, subtitle_format: str, request: Request) -> Response:
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host):
            raise HTTPException(status_code=403, detail="字幕文件仅允许从本机工作台下载。")
        content = run(video_assembly.generate_subtitles, project_id, subtitle_format)
        media_type = "application/x-subrip" if subtitle_format == "srt" else "text/plain"
        return Response(
            content=content,
            media_type=f"{media_type}; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="project-subtitles.{subtitle_format}"'},
        )

    @application.post("/api/projects/{project_id}/assemble")
    def assemble_project_video(project_id: str, request: Request) -> dict:
        client_host = request.client.host if request.client else ""
        if not _is_loopback(client_host) or not _is_same_origin_request(request):
            raise HTTPException(status_code=403, detail="本地视频合成仅允许从本机工作台发起。")
        return run(video_assembly.assemble, project_id)

    @application.post("/api/projects/{project_id}/shots/{shot_id}/image")
    def generate_shot_image(project_id: str, shot_id: str) -> dict:
        return video_assembly.decorate(run(workflow.generate_shot_image, project_id, shot_id))

    @application.post("/api/projects/{project_id}/shots/{shot_id}/video", response_model=None)
    def generate_shot_video(
        project_id: str, shot_id: str, confirm_uncertain: bool = False
    ) -> dict | JSONResponse:
        result = video_assembly.decorate(run(
            workflow.generate_shot_video,
            project_id,
            shot_id,
            confirm_uncertain=confirm_uncertain,
        ))
        shot = next(
            item for item in result["workflow"]["video"]["shots"]
            if item.get("shot_id") == shot_id
        )
        if shot.get("video", {}).get("status") == "queued":
            return JSONResponse(status_code=202, content=result)
        return result

    @application.post("/api/projects/{project_id}/shots/{shot_id}/video/cancel")
    def cancel_shot_video(project_id: str, shot_id: str) -> dict:
        return video_assembly.decorate(run(workflow.cancel_queued_shot_video, project_id, shot_id))

    @application.post("/api/projects/{project_id}/shots/{shot_id}/video/resume")
    def resume_shot_video_polling(project_id: str, shot_id: str) -> dict:
        return video_assembly.decorate(run(workflow.resume_shot_video_polling, project_id, shot_id))

    @application.get("/api/projects/{project_id}/content/{stage}/revisions")
    def list_content_revisions(project_id: str, stage: str) -> dict[str, Any]:
        project = run(workflow.get_project, project_id)
        if stage not in project.get("workflow", {}):
            raise HTTPException(status_code=404, detail="未知的创作步骤。")
        revisions = project["workflow"][stage].get("revisions", [])
        return {"project_id": project_id, "stage": stage, "revisions": revisions}

    @application.post("/api/projects/{project_id}/content/{stage}/revisions/{revision_id}/restore")
    def restore_content_revision(project_id: str, stage: str, revision_id: str) -> dict:
        return video_assembly.decorate(run(workflow.restore_revision, project_id, stage, revision_id))

    @application.post("/api/projects/{project_id}/quality/{stage}")
    def check_content_quality(project_id: str, stage: str) -> dict[str, Any]:
        try:
            return quality.check(project_id, stage)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @application.put("/api/projects/{project_id}/content/{stage}")
    def update_content(project_id: str, stage: str, payload: ContentUpdate) -> dict:
        return video_assembly.decorate(run(workflow.update_content, project_id, stage, payload.content))

    @application.post("/api/projects/{project_id}/rollback/{stage}")
    def rollback_stage(project_id: str, stage: str) -> dict:
        return video_assembly.decorate(run(workflow.rollback_to_stage, project_id, stage))

    @application.post("/api/projects/{project_id}/approve/{stage}")
    def approve(project_id: str, stage: str) -> dict:
        return video_assembly.decorate(run(workflow.approve, project_id, stage))

    @application.get("/api/projects/{project_id}/chat")
    def chat_history(project_id: str) -> list[dict]:
        try:
            return chat.history(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @application.get("/api/projects/{project_id}/chat/history")
    def chat_history_page(
        project_id: str,
        limit: int = Query(default=10, ge=1, le=100),
        before: str | None = Query(default=None, max_length=128),
    ) -> dict:
        try:
            return chat.history_page(project_id, limit=limit, before=before)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @application.post("/api/projects/{project_id}/chat/attachments", status_code=201)
    async def upload_chat_attachments(
        project_id: str,
        files: list[UploadFile] = File(...),
    ) -> list[dict]:
        uploaded = []

        def discard_partial_uploads() -> None:
            for item in uploaded:
                try:
                    chat_attachments.discard_upload(project_id, item["id"])
                except Exception:
                    pass

        try:
            workflow.get_project(project_id)
            if not files or len(files) > 8:
                raise ChatAttachmentError("一次最多上传 8 个附件。")
            total_size = 0
            for file in files:
                content = await file.read(20 * 1024 * 1024 + 1)
                total_size += len(content)
                if total_size > MAX_TOTAL_ATTACHMENT_BYTES:
                    raise ChatAttachmentError("单条消息的附件总大小不能超过 40 MB。")
                uploaded.append(chat_attachments.upload(project_id, file.filename, content))
            return uploaded
        except WorkflowError as error:
            discard_partial_uploads()
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error
        except ChatAttachmentError as error:
            discard_partial_uploads()
            raise HTTPException(status_code=422, detail=str(error)) from error
        except Exception:
            discard_partial_uploads()
            raise

    @application.post("/api/projects/{project_id}/chat")
    def send_chat(project_id: str, payload: ChatRequest) -> list[dict]:
        try:
            return chat.send(project_id, payload.message, payload.model, attachment_ids=payload.attachment_ids)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (ValueError, ChatAttachmentError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @application.post("/api/projects/{project_id}/chat/stream")
    def stream_chat(project_id: str, payload: ChatRequest) -> StreamingResponse:
        events: queue.Queue[Any] = queue.Queue()
        finished = object()

        def worker() -> None:
            try:
                chat.send(
                    project_id,
                    payload.message,
                    payload.model,
                    attachment_ids=payload.attachment_ids,
                    on_event=events.put,
                )
                events.put({
                    "type": "done",
                    "messages": chat.history(project_id, limit=10),
                    "project": workflow.get_project(project_id),
                })
            except KeyError as error:
                events.put({"type": "error", "message": str(error)})
            except (ValueError, RuntimeError, WorkflowError) as error:
                events.put({"type": "error", "message": str(error)})
            except Exception:
                events.put({"type": "error", "message": "对话请求失败，请稍后重试。"})
            finally:
                events.put(finished)

        def event_stream():
            threading.Thread(target=worker, daemon=True).start()
            yield ": connected\n\n"
            while True:
                event = events.get()
                if event is finished:
                    break
                if event is not None:
                    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    yield f"data: {data}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @application.post("/api/audio/transcriptions")
    async def transcribe_audio(
        audio_file: UploadFile = File(...),
        model: str | None = Form(default=None),
    ) -> dict[str, str]:
        profiles = workflow.settings.model_catalog.list("audio")
        profile = None
        if model:
            try:
                profile = workflow.settings.model_catalog.resolve(model, "audio")
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        else:
            profile = next((item for item in profiles if item.params.get("operation") == "transcription"), None)
        if not profile or profile.params.get("operation") != "transcription":
            raise HTTPException(status_code=503, detail="没有配置音频转写模型。")
        if not workflow.settings.profile_is_configured(profile):
            raise HTTPException(status_code=503, detail="音频转写模型未配置 API Key，请在模型配置中填写。")
        try:
            provider = create_audio_provider(
                profile,
                workflow.settings.api_key_for(profile),
                assets,
            )
            transcript = provider.transcribe(
                filename=audio_file.filename or "audio.bin",
                content=await audio_file.read(),
            )
            return {"text": transcript, "model": profile.id}
        except MediaProviderError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    return application


app = create_app()

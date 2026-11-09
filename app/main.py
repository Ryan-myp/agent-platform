from __future__ import annotations

import ipaddress
import json
import os
import queue
import re
import tempfile
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.models import (
    ChatRequest,
    ChatSessionCreate,
    ContentUpdate,
    GenerateRequest,
    ModelCatalogUpdate,
    ModelUpdate,
    ProjectCreate,
    ProjectTitleUpdate,
)
from app.providers.media import AssetStore, ImageProvider, MediaProviderError, VideoProvider, create_audio_provider
from app.providers.catalog import MODALITIES, SUPPORTED_ADAPTERS, ModelCatalog, ModelProfile
from app.providers.credentials import ModelCredentialStore
from app.services.attachments import (
    MAX_TOTAL_ATTACHMENT_BYTES,
    ChatAttachmentError,
    ChatAttachmentService,
)
from app.services.chat import ChatService
from app.services.workflow import WorkflowError, WorkflowService
from app.services.video_jobs import VideoJobWorker
from app.skills.loader import SkillLoader
from app.store import ProjectStore

ROOT = Path(__file__).resolve().parent.parent
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
    workflow = WorkflowService(store, current_settings, skill_loader, video_provider, image_provider, assets)
    video_job_worker = VideoJobWorker(workflow, store)
    workflow.video_job_worker = video_job_worker
    chat_attachments = ChatAttachmentService(store, assets)
    chat = ChatService(store, current_settings, assets, chat_attachments)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
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
    application.state.assets = assets
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

    application.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
    application.mount("/media", StaticFiles(directory=media_path, check_dir=False), name="media")

    def run(action, *args: Any, **kwargs: Any):
        try:
            return action(*args, **kwargs)
        except WorkflowError as error:
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error

    @application.get("/", include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(
            ROOT / "app" / "static" / "index.html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/config")
    def get_config() -> dict:
        return workflow.config_view()

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

    @application.get("/api/projects")
    def list_projects() -> list[dict]:
        return workflow.list_projects()

    @application.post("/api/projects", status_code=201)
    def create_project(payload: ProjectCreate) -> dict:
        return run(workflow.create_project, payload.model_dump())

    @application.post("/api/chat/sessions", status_code=201)
    def create_chat_session(payload: ChatSessionCreate) -> dict:
        return run(workflow.create_project, {**payload.model_dump(), "mode": "chat"})

    @application.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict:
        return run(workflow.get_project, project_id)

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
        return run(workflow.generate, project_id, stage, payload.model if payload else None)

    @application.post("/api/projects/{project_id}/shots/{shot_id}/image")
    def generate_shot_image(project_id: str, shot_id: str) -> dict:
        return run(workflow.generate_shot_image, project_id, shot_id)

    @application.post("/api/projects/{project_id}/shots/{shot_id}/video", response_model=None)
    def generate_shot_video(
        project_id: str, shot_id: str, confirm_uncertain: bool = False
    ) -> dict | JSONResponse:
        result = run(
            workflow.generate_shot_video,
            project_id,
            shot_id,
            confirm_uncertain=confirm_uncertain,
        )
        shot = next(
            item for item in result["workflow"]["video"]["shots"]
            if item.get("shot_id") == shot_id
        )
        if shot.get("video", {}).get("status") == "queued":
            return JSONResponse(status_code=202, content=result)
        return result

    @application.put("/api/projects/{project_id}/content/{stage}")
    def update_content(project_id: str, stage: str, payload: ContentUpdate) -> dict:
        return run(workflow.update_content, project_id, stage, payload.content)

    @application.post("/api/projects/{project_id}/rollback/{stage}")
    def rollback_stage(project_id: str, stage: str) -> dict:
        return run(workflow.rollback_to_stage, project_id, stage)

    @application.post("/api/projects/{project_id}/approve/{stage}")
    def approve(project_id: str, stage: str) -> dict:
        return run(workflow.approve, project_id, stage)

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

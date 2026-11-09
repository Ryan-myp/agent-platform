from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from app.providers.catalog import ModelProfile


class MediaProviderError(RuntimeError):
    pass


class VideoSubmissionUncertainError(MediaProviderError):
    """The provider may have accepted a request, but no task ID was received."""


class AssetStore:
    def __init__(self, directory: Path, public_prefix: str = "/media"):
        self.directory = directory
        self.public_prefix = public_prefix.rstrip("/")
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, content: bytes, suffix: str) -> str:
        safe_suffix = suffix if suffix.startswith(".") else f".{suffix}"
        filename = f"{uuid4().hex}{safe_suffix}"
        (self.directory / filename).write_bytes(content)
        return f"{self.public_prefix}/{filename}"

    def save_base64(self, data: str, mime_type: str | None = None) -> str:
        content = base64.b64decode(data)
        suffix = mimetypes.guess_extension(mime_type or "image/png") or ".png"
        return self.save(content, suffix)


class ImageProvider(Protocol):
    def generate_image(self, *, prompt: str, aspect_ratio: str = "9:16") -> dict[str, Any]: ...


class VideoProvider(Protocol):
    @property
    def ready(self) -> bool: ...

    def generate_video(self, *, project: dict[str, Any]) -> dict[str, Any]: ...


class AudioProvider(Protocol):
    def generate_speech(self, *, text: str, voice: str | None = None) -> dict[str, Any]: ...

    def transcribe(self, *, filename: str, content: bytes) -> str: ...


class UnconfiguredVideoProvider:
    @property
    def ready(self) -> bool:
        return False

    def generate_video(self, *, project: dict[str, Any]) -> dict[str, Any]:
        raise MediaProviderError("No video generation provider is configured")


class OpenAICompatibleImageProvider:
    def __init__(
        self,
        profile: ModelProfile,
        api_key: str,
        assets: AssetStore,
        *,
        client: httpx.Client | None = None,
    ):
        self.profile = profile
        self.api_key = api_key
        self.assets = assets
        self.client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))

    def generate_image(self, *, prompt: str, aspect_ratio: str = "9:16") -> dict[str, Any]:
        if not self.api_key:
            raise MediaProviderError("图像模型缺少 API Key，请在模型配置中填写。")
        params = dict(self.profile.params)
        body: dict[str, Any] = {
            "model": self.profile.model_id,
            "prompt": prompt,
            **{key: value for key, value in params.items() if key != "extra_body"},
        }
        if self.profile.adapter == "agnes_image":
            body["ratio"] = aspect_ratio or params.get("ratio", "1:1")
            body["extra_body"] = dict(params.get("extra_body", {"response_format": "url"}))
        else:
            body.setdefault("size", params.get("size", "1024x1024"))
            if params.get("quality"):
                body["quality"] = params["quality"]
        try:
            response = self.client.post(
                f"{self.profile.base_url.rstrip('/')}/images/generations",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
        except httpx.HTTPError as exc:
            raise MediaProviderError(f"图像服务连接失败：{exc}") from exc
        self._raise_for_response(response)
        data = response.json().get("data", [{}])[0]
        if data.get("url"):
            return {"asset_url": data["url"], "mime_type": data.get("mime_type", "image/png")}
        if data.get("b64_json"):
            url = self.assets.save_base64(data["b64_json"], data.get("mime_type", "image/png"))
            return {"asset_url": url, "mime_type": data.get("mime_type", "image/png")}
        raise MediaProviderError("图像服务返回成功，但没有图片 URL 或 Base64 数据。")

    @staticmethod
    def _raise_for_response(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MediaProviderError(
                f"图像服务返回 HTTP {exc.response.status_code}: {exc.response.text[:1000]}"
            ) from exc


class AgnesVideoProvider:
    def __init__(
        self,
        profile: ModelProfile,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        sleep=time.sleep,
    ):
        self.profile = profile
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=15.0))
        self.sleep = sleep

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def _video_request_body(self, project: dict[str, Any]) -> dict[str, Any]:
        brief = project.get("brief", {})
        params = dict(self.profile.params)
        requested_seconds = int(brief.get("duration_seconds", params.get("seconds", 5)))
        if requested_seconds < 4 or requested_seconds > 12:
            raise MediaProviderError(
                "Agnes Video 2.5 Flash 单次视频时长为 4–12 秒；当前项目时长需要先拆成多个镜头任务。"
            )
        scenes = project.get("workflow", {}).get("scenes", {})
        prompt = project.get("video_prompt") or scenes.get("content") or brief.get("product")
        if not prompt:
            raise MediaProviderError("视频生成缺少提示词。")
        images = project.get("reference_images") or []
        mode = "reference" if images else params.get("mode", "text")
        body: dict[str, Any] = {
            "model": self.profile.model_id,
            "prompt": prompt,
            "seconds": str(requested_seconds),
            "mode": mode,
            "size": params.get("size", "720P"),
            "aspect_ratio": project.get("aspect_ratio") or params.get("aspect_ratio", "16:9"),
        }
        if images:
            body["images"] = images[:5]
        return body

    def submit_video(self, *, project: dict[str, Any]) -> dict[str, str]:
        if not self.api_key:
            raise MediaProviderError("视频模型缺少 API Key，请在模型配置中填写。")
        body = self._video_request_body(project)
        try:
            created = self.client.post(
                f"{self.profile.base_url.rstrip('/')}/videos",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
        except httpx.HTTPError as exc:
            raise VideoSubmissionUncertainError(
                "视频任务提交请求异常，供应商是否已受理未知。请先核对供应商任务，再决定是否重试。"
            ) from exc
        self._raise_for_response(created, "创建视频任务", uncertain_submission=True)
        try:
            task = created.json()
        except ValueError as exc:
            raise VideoSubmissionUncertainError(
                "供应商已响应，但返回内容无法识别，任务是否已受理未知。请核对供应商任务后再决定是否重试。"
            ) from exc
        if not isinstance(task, dict) or not task.get("video_id"):
            raise VideoSubmissionUncertainError(
                "Agnes 响应中没有 video_id，任务是否已受理未知。请核对供应商任务后再决定是否重试。"
            )
        video_id = str(task["video_id"])
        return {
            "provider_task_id": video_id,
            "task_id": str(task.get("id") or video_id),
        }

    def poll_video(self, *, provider_task_id: str) -> dict[str, Any]:
        if not self.api_key:
            raise MediaProviderError("视频模型缺少 API Key，请在模型配置中填写。")
        root_url = self.profile.base_url.rstrip("/")
        if root_url.endswith("/v1"):
            root_url = root_url[:-3]
        try:
            response = self.client.get(
                f"{root_url}/agnesapi",
                params={"video_id": provider_task_id, "model_name": self.profile.model_id},
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except httpx.HTTPError as exc:
            raise MediaProviderError("查询 Agnes 视频任务暂时失败。") from exc
        self._raise_for_response(response, "查询视频任务")
        try:
            payload = response.json()
        except ValueError as exc:
            raise MediaProviderError("Agnes 视频任务状态响应无法识别。") from exc
        if not isinstance(payload, dict):
            raise MediaProviderError("Agnes 视频任务状态响应格式无效。")
        raw_status = str(payload.get("status") or "processing")
        normalized_status = raw_status.lower()
        if normalized_status in {"completed", "complete", "success", "succeeded"}:
            url = payload.get("url")
            if not url:
                return {"status": "failed", "error": "视频任务完成，但服务没有返回成片 URL。"}
            return {"status": "completed", "provider_status": raw_status, "asset_url": url}
        if normalized_status in {"failed", "error", "cancelled", "canceled"}:
            return {
                "status": "failed",
                "provider_status": raw_status,
                "error": str(payload.get("error") or "未知错误")[:1000],
            }
        if normalized_status in {"queued", "pending", "created", "waiting"}:
            status = "queued"
        else:
            status = "processing"
        return {"status": status, "provider_status": raw_status}

    def generate_video(self, *, project: dict[str, Any]) -> dict[str, Any]:
        task = self.submit_video(project=project)
        timeout = int(self.profile.params.get("timeout_seconds", 300))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.poll_video(provider_task_id=task["provider_task_id"])
            if status["status"] == "completed":
                return {
                    "asset_url": status["asset_url"],
                    "summary": "Agnes 视频生成完成",
                    "task_id": task["task_id"],
                }
            if status["status"] == "failed":
                raise MediaProviderError(f"Agnes 视频任务失败：{status.get('error') or '未知错误'}")
            self.sleep(1.5)
        raise MediaProviderError(
            f"Agnes 视频生成超过 {timeout} 秒仍未完成，任务 ID：{task['task_id']}"
        )

    @staticmethod
    def _raise_for_response(
        response: httpx.Response,
        action: str,
        *,
        uncertain_submission: bool = False,
    ) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                payload = exc.response.json()
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            nested_error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            code = payload.get("code") or nested_error.get("code")
            message = payload.get("message") or nested_error.get("message") or ""
            message = str(message)
            request_id = (
                payload.get("request_id")
                or payload.get("requestId")
                or exc.response.headers.get("x-request-id")
                or exc.response.headers.get("request-id")
            )
            if not request_id and message:
                match = re.search(r"\brequest\s*id\s*[:=]\s*([A-Za-z0-9._-]+)", message, re.IGNORECASE)
                request_id = match.group(1) if match else None
            if code == "video_queue_full":
                detail = (
                    "Agnes 视频生成队列已满，供应商暂时无法受理新任务。"
                    "请稍后手动重试；平台不会自动重复提交。"
                )
                if request_id:
                    detail += f" 请求 ID：{request_id}。"
                raise MediaProviderError(detail) from exc

            detail = f"{code}: {message}" if code and message else message or exc.response.text[:1000]
            if request_id and str(request_id) not in detail:
                detail += f"（请求 ID：{request_id}）"
            status_code = exc.response.status_code
            if uncertain_submission and (status_code == 408 or status_code >= 500):
                raise VideoSubmissionUncertainError(
                    f"{action}返回 HTTP {status_code}，供应商是否已受理未知：{detail} "
                    "请先核对供应商任务，再决定是否重试。"
                ) from exc
            raise MediaProviderError(
                f"{action}失败（HTTP {status_code}）：{detail}"
            ) from exc


class OpenAICompatibleAudioProvider:
    def __init__(
        self,
        profile: ModelProfile,
        api_key: str,
        assets: AssetStore,
        *,
        client: httpx.Client | None = None,
    ):
        self.profile = profile
        self.api_key = api_key
        self.assets = assets
        self.client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))

    def generate_speech(self, *, text: str, voice: str | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise MediaProviderError("音频模型缺少 API Key，请在模型配置中填写。")
        params = self.profile.params
        if params.get("operation", "speech") != "speech":
            raise MediaProviderError("所选音频模型配置为转写模型，不能用于语音生成。")
        try:
            response = self.client.post(
                f"{self.profile.base_url.rstrip('/')}/audio/speech",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.profile.model_id,
                    "input": text,
                    "voice": voice or params.get("voice", "alloy"),
                    "response_format": params.get("format", "mp3"),
                },
            )
        except httpx.HTTPError as exc:
            raise MediaProviderError(f"音频服务连接失败：{exc}") from exc
        self._raise_for_response(response)
        suffix = "." + str(params.get("format", "mp3"))
        return {"asset_url": self.assets.save(response.content, suffix), "mime_type": response.headers.get("content-type", "audio/mpeg")}

    def transcribe(self, *, filename: str, content: bytes) -> str:
        if not self.api_key:
            raise MediaProviderError("音频模型缺少 API Key，请在模型配置中填写。")
        if self.profile.params.get("operation") != "transcription":
            raise MediaProviderError("所选音频模型配置为语音生成模型，不能用于转写。")
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            response = self.client.post(
                f"{self.profile.base_url.rstrip('/')}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                data={"model": self.profile.model_id},
                files={"file": (Path(filename).name, content, mime_type)},
            )
        except httpx.HTTPError as exc:
            raise MediaProviderError(f"音频服务连接失败：{exc}") from exc
        self._raise_for_response(response)
        return str(response.json().get("text", ""))

    @staticmethod
    def _raise_for_response(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MediaProviderError(
                f"音频服务返回 HTTP {exc.response.status_code}: {exc.response.text[:1000]}"
            ) from exc


def create_image_provider(profile: ModelProfile, api_key: str, assets: AssetStore) -> ImageProvider:
    if profile.adapter in {"agnes_image", "openai_images"}:
        return OpenAICompatibleImageProvider(profile, api_key, assets)
    raise MediaProviderError(f"图像模型适配器未实现：{profile.adapter}")


def create_video_provider(profile: ModelProfile, api_key: str) -> VideoProvider:
    if profile.adapter == "agnes_video":
        return AgnesVideoProvider(profile, api_key)
    raise MediaProviderError(f"视频模型适配器未实现：{profile.adapter}")


def create_audio_provider(profile: ModelProfile, api_key: str, assets: AssetStore) -> AudioProvider:
    if profile.adapter == "openai_audio":
        return OpenAICompatibleAudioProvider(profile, api_key, assets)
    raise MediaProviderError(f"音频模型适配器未实现：{profile.adapter}")

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from app.config import Settings
from app.providers.catalog import ModelProfile
from app.providers.media import (
    AssetStore,
    MediaProviderError,
    create_audio_provider,
    create_image_provider,
    create_video_provider,
)
from app.providers.text import TextProvider, create_text_provider
from app.store import ProjectStore


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate an image when the user explicitly asks for an image or visual asset.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed image-generation prompt."},
                    "aspect_ratio": {"type": "string", "enum": ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"]},
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_video",
            "description": "Generate a video only when the user explicitly asks to generate a video.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed video-generation prompt."},
                    "seconds": {"type": "integer", "minimum": 4, "maximum": 12},
                    "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]},
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_speech",
            "description": "Create spoken audio when the user explicitly requests voice or audio generation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to speak."},
                    "voice": {"type": "string", "description": "Optional configured voice name."},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChatService:
    def __init__(self, store: ProjectStore, settings: Settings, assets: AssetStore):
        self.store = store
        self.settings = settings
        self.assets = assets

    def history(self, project_id: str) -> list[dict[str, Any]]:
        if not self.store.get(project_id):
            raise KeyError("找不到这个创作项目。")
        return self.store.chat_history(project_id)

    def send(
        self,
        project_id: str,
        content: str,
        model_id: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        project = self.store.get(project_id)
        if not project:
            raise KeyError("找不到这个创作项目。")
        message_text = content.strip()
        if not message_text:
            raise ValueError("消息不能为空。")
        selected_id = model_id or self.settings.model_catalog.defaults["text"]
        try:
            profile = self.settings.model_catalog.resolve(selected_id, "text")
        except ValueError as error:
            raise ValueError(str(error)) from error
        if not self.settings.profile_is_configured(profile):
            raise RuntimeError(f"模型 {profile.name} 未配置 API Key，请在模型配置中填写。")
        if project.get("mode") == "chat" and not self.store.chat_history(project_id):
            project["title"] = message_text[:48].strip() or "新对话"
            project["updated_at"] = now_iso()
            self.store.save(project)
        self._save(project_id, {"role": "user", "content": message_text})
        api_key = self.settings.api_key_for(profile)
        provider = create_text_provider(profile, api_key)
        history = self.store.chat_history(project_id)
        context = self._context(project)
        messages: list[dict[str, Any]] = [{"role": "system", "content": context}]
        messages.extend(self._provider_messages(history[-30:]))
        if on_event:
            on_event({"type": "status", "message": f"正在连接 {profile.name}…"})
        result = self._request_chat(provider, messages, on_event)
        attachments: list[dict[str, Any]] = []
        for _ in range(3):
            if not result.tool_calls:
                break
            assistant_calls = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
                    },
                }
                for call in result.tool_calls
            ]
            self._save(project_id, {
                "role": "assistant",
                "content": result.text,
                "tool_calls": assistant_calls,
                "model": selected_id,
                "internal": True,
            })
            messages.append({"role": "assistant", "content": result.text or None, "tool_calls": assistant_calls})
            for call in result.tool_calls:
                if on_event:
                    labels = {
                        "generate_image": "正在生成图片…",
                        "generate_video": "正在生成视频；任务可能需要一些时间…",
                        "generate_speech": "正在生成语音…",
                    }
                    on_event({"type": "status", "message": labels.get(call["name"], "正在处理媒体任务…")})
                try:
                    output = self._execute_tool(call["name"], call.get("arguments", {}), project)
                    attachments.extend(output.get("attachments", []))
                    tool_content = output.get("message", "已完成。")
                except Exception as error:  # expose provider errors as tool results for the model to explain
                    tool_content = f"工具执行失败：{error}"
                    output = {"attachments": []}
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": tool_content,
                })
                self._save(project_id, {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": tool_content,
                    "attachments": output.get("attachments", []),
                    "internal": True,
                })
            result = self._request_chat(provider, messages, on_event)
        self._save(project_id, {
            "role": "assistant",
            "content": result.text or "已处理。",
            "attachments": attachments,
            "model": selected_id,
        })
        return self.history(project_id)

    @staticmethod
    def _request_chat(
        provider: TextProvider,
        messages: list[dict[str, Any]],
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ):
        try:
            stream_chat = getattr(provider, "chat_stream", None)
            if on_event and callable(stream_chat):
                return stream_chat(
                    messages=messages,
                    tools=TOOLS,
                    on_delta=lambda text: on_event({"type": "delta", "text": text}),
                )
            return provider.chat(messages=messages, tools=TOOLS)
        except Exception as error:
            raise RuntimeError(f"文本模型请求失败：{error}") from error

    def _execute_tool(self, name: str, arguments: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
        if name == "generate_image":
            profile = self._default_profile("image")
            provider = create_image_provider(profile, self.settings.api_key_for(profile), self.assets)
            result = provider.generate_image(
                prompt=str(arguments.get("prompt", "")),
                aspect_ratio=str(arguments.get("aspect_ratio", "9:16")),
            )
            return {
                "message": "图片已生成。",
                "attachments": [{"type": "image", "url": result["asset_url"], "model": profile.name}],
            }
        if name == "generate_video":
            profile = self._default_profile("video")
            video_project = dict(project)
            video_project["video_prompt"] = str(arguments.get("prompt", ""))
            seconds = int(arguments.get("seconds", 5))
            video_project["brief"] = {**project.get("brief", {}), "duration_seconds": seconds}
            video_project["aspect_ratio"] = str(arguments.get("aspect_ratio", "9:16"))
            provider = create_video_provider(profile, self.settings.api_key_for(profile))
            result = provider.generate_video(project=video_project)
            return {
                "message": "视频已生成。",
                "attachments": [{"type": "video", "url": result["asset_url"], "model": profile.name}],
            }
        if name == "generate_speech":
            profile = self._default_profile("audio")
            provider = create_audio_provider(profile, self.settings.api_key_for(profile), self.assets)
            result = provider.generate_speech(
                text=str(arguments.get("text", "")),
                voice=arguments.get("voice"),
            )
            return {
                "message": "语音已生成。",
                "attachments": [{"type": "audio", "url": result["asset_url"], "model": profile.name}],
            }
        raise MediaProviderError(f"未知的工具：{name}")

    def _default_profile(self, modality: str) -> ModelProfile:
        profile_id = self.settings.model_catalog.defaults.get(modality)
        if not profile_id:
            raise MediaProviderError(f"尚未配置默认{modality}模型，请编辑模型目录配置。")
        profile = self.settings.model_catalog.resolve(profile_id, modality)
        if not self.settings.profile_is_configured(profile):
            raise MediaProviderError(f"模型 {profile.name} 未配置 API Key，请先在模型配置中填写。")
        return profile

    @staticmethod
    def _provider_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        messages = []
        for item in history:
            if item.get("internal"):
                if item.get("role") == "assistant" and item.get("tool_calls"):
                    messages.append({
                        "role": "assistant",
                        "content": item.get("content") or None,
                        "tool_calls": item["tool_calls"],
                    })
                elif item.get("role") == "tool":
                    messages.append({
                        "role": "tool",
                        "tool_call_id": item.get("tool_call_id", ""),
                        "content": item.get("content", ""),
                    })
                continue
            if item.get("role") in {"user", "assistant"}:
                messages.append({"role": item["role"], "content": item.get("content", "")})
        return messages

    @staticmethod
    def _context(project: dict[str, Any]) -> str:
        workflow = project.get("workflow", {})
        approved = {
            stage: item.get("content")
            for stage, item in workflow.items()
            if item.get("status") == "approved"
        }
        instructions = (
            "你是一个真实可用的多模态内容创作助手。用户可以在对话中讨论方案，也可以明确要求生成图片、视频或语音。"
            "需要生成媒体时，只有在用户明确提出后才调用对应工具；没有工具结果时绝不可声称媒体已生成。"
            "修改已经确认的工作流内容前先征求用户同意。生成视频前明确说明当前视频模型的时长限制。"
        )
        if project.get("mode") == "chat":
            return (
                instructions
                + "这是普通对话，没有预设的项目 brief。只依据用户在对话中提供的信息回答；缺少关键信息时再提问，不要假设产品事实。"
            )
        return (
            instructions
            + "遵守用户 brief，不要捏造产品事实、效果数据或证言。\n"
            + f"项目 brief：{json.dumps(project.get('brief', {}), ensure_ascii=False)}\n"
            + f"已确认的工作流内容：{json.dumps(approved, ensure_ascii=False)}"
        )

    def _save(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        message = {"id": str(uuid4()), "created_at": now_iso(), **payload}
        return self.store.append_chat_message(project_id, message)

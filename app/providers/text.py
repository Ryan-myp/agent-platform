from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import httpx

from app.providers.catalog import ModelProfile


@dataclass
class ChatResult:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class TextProvider(Protocol):
    def generate(self, *, system: str, user: str) -> str: ...

    def chat(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> ChatResult: ...


class OpenAICompatibleTextProvider:
    """Chat Completions adapter for Agnes and other OpenAI-compatible endpoints."""

    def __init__(self, profile: ModelProfile, api_key: str, *, client: httpx.Client | None = None):
        self.profile = profile
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=httpx.Timeout(90.0, connect=15.0))

    def _request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ChatResult:
        if not self.api_key:
            raise RuntimeError("模型缺少 API Key，请在模型配置中填写。")
        payload: dict[str, Any] = {
            "model": self.profile.model_id,
            "messages": messages,
            **self.profile.params,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            response = self.client.post(
                f"{self.profile.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"模型服务连接失败：{exc}") from exc
        self._raise_for_response(response)
        data = response.json()
        choice = data.get("choices", [{}])[0].get("message", {})
        content = choice.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        calls = []
        for call in choice.get("tool_calls", []) or []:
            function = call.get("function", {})
            raw_arguments = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            except json.JSONDecodeError:
                arguments = {}
            calls.append({
                "id": call.get("id", ""),
                "name": function.get("name", ""),
                "arguments": arguments,
            })
        return ChatResult(str(content), calls)

    @staticmethod
    def _raise_for_response(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.text[:1000]
            except httpx.ResponseNotRead:
                detail = exc.response.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"模型服务返回 HTTP {exc.response.status_code}: {detail}") from exc

    def generate(self, *, system: str, user: str) -> str:
        result = self._request([{"role": "system", "content": system}, {"role": "user", "content": user}])
        return result.text.strip()

    def chat(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ChatResult:
        return self._request(messages, tools)

    def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_delta: Callable[[str], None],
    ) -> ChatResult:
        if not self.api_key:
            raise RuntimeError("模型缺少 API Key")
        payload: dict[str, Any] = {
            "model": self.profile.model_id,
            "messages": messages,
            "stream": True,
            **self.profile.params,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        url = f"{self.profile.base_url.rstrip('/')}/chat/completions"
        text_parts: list[str] = []
        tool_parts: dict[int, dict[str, Any]] = {}
        try:
            with self.client.stream(
                "POST",
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            ) as response:
                self._raise_for_response(response)
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_line = line[5:].strip()
                    if data_line == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_line)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        text_parts.append(text)
                        on_delta(text)
                    for part in delta.get("tool_calls") or []:
                        index = int(part.get("index", 0))
                        target = tool_parts.setdefault(index, {
                            "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                        })
                        if part.get("id"):
                            target["id"] = part["id"]
                        function = part.get("function") or {}
                        target["function"]["name"] += function.get("name", "")
                        target["function"]["arguments"] += function.get("arguments", "")
        except httpx.HTTPError as exc:
            raise RuntimeError(f"模型服务连接失败：{exc}") from exc
        calls = []
        for part in tool_parts.values():
            raw_arguments = part["function"]["arguments"] or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {}
            calls.append({"id": part["id"], "name": part["function"]["name"], "arguments": arguments})
        return ChatResult("".join(text_parts), calls)


class AnthropicTextProvider:
    def __init__(self, profile: ModelProfile, api_key: str):
        from anthropic import Anthropic

        self.profile = profile
        self.client = Anthropic(api_key=api_key)

    @staticmethod
    def _convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for item in messages:
            role = item.get("role")
            if role == "system":
                continue
            if role == "tool":
                result = {
                    "type": "tool_result",
                    "tool_use_id": item.get("tool_call_id", ""),
                    "content": str(item.get("content", "")),
                }
                if converted and converted[-1]["role"] == "user":
                    converted[-1]["content"].append(result)
                else:
                    converted.append({"role": "user", "content": [result]})
                continue
            blocks: list[dict[str, Any]] = []
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text" and part.get("text"):
                        blocks.append({"type": "text", "text": str(part["text"])})
                    elif part.get("type") == "image_url":
                        image_url = part.get("image_url", {}).get("url", "")
                        match = re.fullmatch(r"data:(image/(?:jpeg|png|gif|webp));base64,(.+)", image_url)
                        if match:
                            blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": match.group(1),
                                    "data": match.group(2),
                                },
                            })
            elif content:
                blocks.append({"type": "text", "text": str(content)})
            for call in item.get("tool_calls", []) or []:
                function = call.get("function", {})
                raw_args = function.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        raw_args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": call.get("id", ""),
                    "name": function.get("name", ""),
                    "input": raw_args,
                })
            converted.append({"role": role, "content": blocks or ""})
        return converted

    @staticmethod
    def _anthropic_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return [
            {
                "name": item["function"]["name"],
                "description": item["function"].get("description", ""),
                "input_schema": item["function"].get("parameters", {"type": "object", "properties": {}}),
            }
            for item in (tools or [])
        ]

    def _request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ChatResult:
        if not self.profile.api_key_env:
            raise RuntimeError("Claude 模型未配置 API Key。")
        system = "\n".join(
            str(item.get("content", "")) for item in messages if item.get("role") == "system"
        )
        kwargs: dict[str, Any] = {
            "model": self.profile.model_id,
            "max_tokens": int(self.profile.params.get("max_tokens", 3000)),
            "system": system,
            "messages": self._convert_messages(messages),
        }
        if tools:
            kwargs["tools"] = self._anthropic_tools(tools)
        try:
            response = self.client.messages.create(**kwargs)
        except Exception as exc:
            raise RuntimeError(f"Claude 服务请求失败：{exc}") from exc
        text_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
            elif getattr(block, "type", None) == "tool_use":
                calls.append({"id": block.id, "name": block.name, "arguments": block.input})
        return ChatResult("\n".join(text_parts).strip(), calls)

    def generate(self, *, system: str, user: str) -> str:
        return self._request([{"role": "system", "content": system}, {"role": "user", "content": user}]).text

    def chat(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ChatResult:
        return self._request(messages, tools)

    def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_delta: Callable[[str], None],
    ) -> ChatResult:
        system = "\n".join(
            str(item.get("content", "")) for item in messages if item.get("role") == "system"
        )
        kwargs: dict[str, Any] = {
            "model": self.profile.model_id,
            "max_tokens": int(self.profile.params.get("max_tokens", 3000)),
            "system": system,
            "messages": self._convert_messages(messages),
        }
        if tools:
            kwargs["tools"] = self._anthropic_tools(tools)
        try:
            with self.client.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    on_delta(text)
                response = stream.get_final_message()
        except Exception as exc:
            raise RuntimeError(f"Claude 服务请求失败：{exc}") from exc
        text_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
            elif getattr(block, "type", None) == "tool_use":
                calls.append({"id": block.id, "name": block.name, "arguments": block.input})
        return ChatResult("\n".join(text_parts).strip(), calls)


def create_text_provider(profile: ModelProfile, api_key: str) -> TextProvider:
    if profile.adapter == "openai_compatible":
        return OpenAICompatibleTextProvider(profile, api_key)
    if profile.adapter == "anthropic":
        if not api_key:
            raise RuntimeError("模型缺少 API Key，请在模型配置中填写。")
        return AnthropicTextProvider(profile, api_key)
    raise RuntimeError(f"文本模型适配器未实现：{profile.adapter}")

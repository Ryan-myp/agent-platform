from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    business: str = Field(min_length=1, max_length=300)
    product: str = Field(min_length=1, max_length=300)
    audience: str = Field(min_length=1, max_length=500)
    platform: str = Field(default="抖音 / Reels / Shorts", max_length=100)
    style: str = Field(default="真实、清晰、有记忆点", max_length=300)
    duration_seconds: int = Field(default=30, ge=6, le=180)
    model: str | None = None


class ChatSessionCreate(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=100)
    model: str | None = None


class GenerateRequest(BaseModel):
    model: str | None = None


class ModelUpdate(BaseModel):
    model: str


class ModelProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=100)
    modality: Literal["text", "image", "video", "audio"]
    adapter: str = Field(min_length=1, max_length=80)
    model_id: str = Field(min_length=1, max_length=200)
    base_url: str = Field(default="", max_length=2000)
    api_key_env: str = Field(default="", max_length=100, pattern=r"^$|^[A-Z][A-Z0-9_]*$")
    api_key: str | None = Field(default=None, max_length=4096)
    params: dict[str, Any] = Field(default_factory=dict)


class ModelCatalogUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaults: dict[Literal["text", "image", "video", "audio"], str]
    models: list[ModelProfileConfig] = Field(min_length=1, max_length=64)


class ContentUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=50000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    model: str | None = None

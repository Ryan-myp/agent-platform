from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    track: Literal["marketing", "drama"] = "marketing"
    business: str = Field(min_length=1, max_length=300)
    product: str = Field(min_length=1, max_length=300)
    audience: str = Field(min_length=1, max_length=500)
    platform: str = Field(default="抖音 / Reels / Shorts", max_length=100)
    style: str = Field(default="真实、清晰、有记忆点", max_length=300)
    duration_seconds: int = Field(default=30, ge=6, le=180)
    model: str | None = None
    series_id: str | None = Field(default=None, max_length=64)
    episode_number: int | None = Field(default=None, ge=1, le=9999)


class ChatSessionCreate(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=100)
    model: str | None = None


class GenerateRequest(BaseModel):
    model: str | None = None


class ModelUpdate(BaseModel):
    model: str


class ProjectTitleUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=100)


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
    message: str = Field(default="", max_length=10000)
    model: str | None = None
    attachment_ids: list[str] = Field(default_factory=list, max_length=8)


class SeriesCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    logline: str = Field(default="", max_length=3000)
    genre: str = Field(default="", max_length=300)
    world_setting: str = Field(default="", max_length=5000)
    central_theme: str = Field(default="", max_length=2000)
    season_arc: str = Field(default="", max_length=5000)
    character_arcs: str = Field(default="", max_length=5000)
    ending: str = Field(default="", max_length=3000)
    tone: str = Field(default="", max_length=1000)
    content_boundaries: str = Field(default="", max_length=2000)
    visual_style: str = Field(default="", max_length=2000)
    notes: str = Field(default="", max_length=5000)


class SeriesUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    logline: str | None = Field(default=None, max_length=3000)
    genre: str | None = Field(default=None, max_length=300)
    world_setting: str | None = Field(default=None, max_length=5000)
    central_theme: str | None = Field(default=None, max_length=2000)
    season_arc: str | None = Field(default=None, max_length=5000)
    character_arcs: str | None = Field(default=None, max_length=5000)
    ending: str | None = Field(default=None, max_length=3000)
    tone: str | None = Field(default=None, max_length=1000)
    content_boundaries: str | None = Field(default=None, max_length=2000)
    visual_style: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=5000)
    archived: bool | None = None


class SeriesContinuityCreate(BaseModel):
    kind: Literal["fact", "event", "relationship", "clue", "open_thread", "constraint"]
    summary: str = Field(min_length=1, max_length=1200)
    status: Literal["confirmed", "open", "resolved", "archived"] = "confirmed"
    source_project_id: str | None = Field(default=None, max_length=64)
    source_stage: Literal["script", "prompt", "storyboard", "scenes"] | None = None
    source_excerpt: str | None = Field(default=None, max_length=1000)


class SeriesContinuityUpdate(BaseModel):
    summary: str | None = Field(default=None, min_length=1, max_length=1200)
    status: Literal["confirmed", "open", "resolved", "archived"] | None = None


class SeriesAssetCreate(BaseModel):
    kind: Literal["character", "location", "prop"]
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=3000)
    visual_prompt: str = Field(default="", max_length=3000)
    continuity_notes: str = Field(default="", max_length=3000)


class SeriesAssetUpdate(BaseModel):
    kind: Literal["character", "location", "prop"] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=3000)
    visual_prompt: str | None = Field(default=None, max_length=3000)
    continuity_notes: str | None = Field(default=None, max_length=3000)
    archived: bool | None = None


class StorageCleanupRequest(BaseModel):
    confirm: bool = False
    attachment_ids: list[str] = Field(default_factory=list, max_length=1000)
    storage_keys: list[str] = Field(default_factory=list, max_length=1000)

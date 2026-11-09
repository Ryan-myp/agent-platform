from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODALITIES = ("text", "image", "video", "audio")
SUPPORTED_ADAPTERS = {
    "text": frozenset({"openai_compatible", "anthropic"}),
    "image": frozenset({"agnes_image", "openai_images"}),
    "video": frozenset({"agnes_video"}),
    "audio": frozenset({"openai_audio"}),
}
_PRIVATE_PARAM_KEYS = frozenset({"api_key", "apikey", "authorization", "access_token", "refresh_token", "secret"})


def _public_params(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _public_params(item)
            for key, item in value.items()
            if str(key).lower().replace("-", "_") not in _PRIVATE_PARAM_KEYS
        }
    if isinstance(value, list):
        return [_public_params(item) for item in value]
    return value


@dataclass(frozen=True)
class ModelProfile:
    id: str
    name: str
    modality: str
    adapter: str
    model_id: str
    base_url: str
    api_key_env: str
    params: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelProfile":
        profile = cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            modality=str(data["modality"]),
            adapter=str(data["adapter"]),
            model_id=str(data["model_id"]),
            base_url=str(data.get("base_url") or ""),
            api_key_env=str(data.get("api_key_env") or ""),
            params=dict(data.get("params") or {}),
        )
        if profile.modality not in MODALITIES:
            raise ValueError(f"Unsupported modality for {profile.id}: {profile.modality}")
        if not profile.id or not profile.adapter or not profile.model_id:
            raise ValueError("Each model profile requires id, adapter, and model_id")
        return profile

    def is_configured(
        self,
        api_keys: dict[str, str],
        model_credentials: dict[str, str] | None = None,
    ) -> bool:
        credentials = model_credentials or {}
        return bool(credentials.get(self.id) or api_keys.get(self.api_key_env))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "modality": self.modality,
            "adapter": self.adapter,
            "model_id": self.model_id,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "params": dict(self.params),
        }

    def public_dict(
        self,
        api_keys: dict[str, str],
        model_credentials: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "modality": self.modality,
            "adapter": self.adapter,
            "model_id": self.model_id,
            "base_url": self.base_url,
            "configured": self.is_configured(api_keys, model_credentials),
            "params": _public_params(self.params),
        }


class ModelCatalog:
    def __init__(self, profiles: list[ModelProfile], defaults: dict[str, str]):
        self.profiles = {profile.id: profile for profile in profiles}
        if len(self.profiles) != len(profiles):
            raise ValueError("Model profile IDs must be unique")
        self.defaults = {modality: defaults.get(modality, "") for modality in MODALITIES}
        for modality, profile_id in self.defaults.items():
            if profile_id and profile_id not in self.profiles:
                raise ValueError(f"Unknown default model profile for {modality}: {profile_id}")
            if profile_id and self.profiles[profile_id].modality != modality:
                raise ValueError(f"Default model {profile_id} does not support {modality}")

    @classmethod
    def load(cls, path: Path) -> "ModelCatalog":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelCatalog":
        profiles = [ModelProfile.from_dict(item) for item in data.get("models", [])]
        return cls(profiles, data.get("defaults", {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "defaults": dict(self.defaults),
            "models": [profile.to_dict() for profile in self.profiles.values()],
        }

    def resolve(self, profile_id: str, modality: str | None = None) -> ModelProfile:
        profile = self.profiles.get(profile_id)
        if profile is None:
            raise ValueError(f"Unknown model profile: {profile_id}")
        if modality and profile.modality != modality:
            raise ValueError(f"Model {profile_id} is {profile.modality}, not {modality}")
        return profile

    def list(self, modality: str | None = None) -> list[ModelProfile]:
        return [
            profile for profile in self.profiles.values()
            if modality is None or profile.modality == modality
        ]


def env_api_keys(catalog: ModelCatalog) -> dict[str, str]:
    names = {profile.api_key_env for profile in catalog.profiles.values() if profile.api_key_env}
    return {name: os.getenv(name, "") for name in names}

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from app.providers.catalog import ModelCatalog, ModelProfile, env_api_keys

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database_path: Path
    model_catalog_path: Path
    model_catalog: ModelCatalog
    api_keys: dict[str, str]
    model_credentials: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        catalog_path = Path(os.getenv("MODEL_CATALOG_PATH", str(ROOT / "config" / "models.json")))
        catalog = ModelCatalog.load(catalog_path)
        return cls(
            database_path=Path(os.getenv("DATABASE_PATH", "data/agent-platform.db")),
            model_catalog_path=catalog_path,
            model_catalog=catalog,
            api_keys=env_api_keys(catalog),
        )

    def api_key(self, env_name: str) -> str:
        return self.api_keys.get(env_name, "")

    def api_key_for(self, profile: ModelProfile) -> str:
        return self.model_credentials.get(profile.id) or self.api_key(profile.api_key_env)

    def profile_is_configured(self, profile: ModelProfile) -> bool:
        return bool(self.api_key_for(profile))

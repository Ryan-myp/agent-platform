from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from app.models import StoryboardCanvasCard
from app.store import ProjectStore


MAX_CANVAS_CARDS = 24


class StoryboardCanvasError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class StoryboardCanvasService:
    """Persist a manual storyboard board without calling any model provider."""

    def __init__(self, store: ProjectStore):
        self.store = store

    @staticmethod
    def normalize_cards(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > MAX_CANVAS_CARDS:
            raise StoryboardCanvasError(f"分镜画布最多支持 {MAX_CANVAS_CARDS} 个镜头。", 422)
        cards: list[dict[str, Any]] = []
        ids: set[str] = set()
        shot_ids: set[str] = set()
        for index, item in enumerate(value, start=1):
            try:
                card = StoryboardCanvasCard.model_validate(item)
            except ValidationError as error:
                raise StoryboardCanvasError(f"第 {index} 个镜头卡片格式无效，请检查编号、时长和文本长度。", 422) from error
            if card.id in ids:
                raise StoryboardCanvasError(f"画布卡片 ID {card.id!r} 重复。", 422)
            if card.shot_id in shot_ids:
                raise StoryboardCanvasError(f"镜头编号 {card.shot_id!r} 重复，请修改后保存。", 422)
            ids.add(card.id)
            shot_ids.add(card.shot_id)
            cards.append(card.model_dump())
        return cards

    @classmethod
    def validate_record(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise StoryboardCanvasError("项目中的分镜画布格式无效。", 422)
        version = value.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise StoryboardCanvasError("项目中的分镜画布版本不受支持。", 422)
        cards = cls.normalize_cards(value.get("cards", []))
        updated_at = value.get("updated_at")
        if updated_at is not None and not isinstance(updated_at, str):
            raise StoryboardCanvasError("项目中的分镜画布时间无效。", 422)
        return {"version": 1, "cards": cards, "updated_at": updated_at}

    def update(self, project_id: str, expected_updated_at: str, cards: list[StoryboardCanvasCard]) -> dict[str, Any]:
        project = self.store.get(project_id)
        if project is None:
            raise StoryboardCanvasError("找不到这个创作项目。", 404)
        if project.get("mode") != "workflow":
            raise StoryboardCanvasError("分镜画布仅适用于创作流程项目。", 409)
        if project.get("updated_at") != expected_updated_at:
            raise StoryboardCanvasError("项目已在其他位置更新；画布尚未保存，请刷新项目后再试。", 409)

        normalized_cards = self.normalize_cards([card.model_dump() for card in cards])
        now = datetime.now(timezone.utc).isoformat()
        project["storyboard_canvas"] = {
            "version": 1,
            "cards": normalized_cards,
            "updated_at": now,
        }
        project["updated_at"] = now
        if not self.store.save_if_updated_at(project, expected_updated_at):
            raise StoryboardCanvasError("保存时项目发生并发更新；画布未覆盖最新项目，请刷新后重试。", 409)
        return project

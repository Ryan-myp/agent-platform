from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import threading
from typing import Any
from uuid import uuid4

from app.store import ProjectStore

MEDIA_MODALITIES = {"image", "video", "audio"}


class UsageLimitExceeded(RuntimeError):
    pass


class UsageService:
    def __init__(self, store: ProjectStore, daily_media_limit: int = 0):
        self.store = store
        self.daily_media_limit = max(0, int(daily_media_limit))
        self._limit_lock = threading.Lock()

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    def today_count(self) -> int:
        now = self._utc_now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return self.store.count_usage_events_since(start, modalities=MEDIA_MODALITIES)

    def ensure_media_allowed(self) -> None:
        if not self.daily_media_limit:
            return
        used = self.today_count()
        if used >= self.daily_media_limit:
            raise UsageLimitExceeded(
                f"已达到本地每日媒体生成上限（{self.daily_media_limit} 次）；本次未提交给供应商。"
            )

    def begin_media_call(
        self,
        *,
        project_id: str | None,
        modality: str,
        model_id: str | None,
        operation: str,
        event_id: str | None = None,
        created_at: str | None = None,
        status: str = "started",
    ) -> dict[str, Any]:
        if modality not in MEDIA_MODALITIES:
            raise ValueError("媒体用量只能使用 image/video/audio 模态。")
        with self._limit_lock:
            self.ensure_media_allowed()
            return self.record(
                project_id=project_id,
                modality=modality,
                model_id=model_id,
                operation=operation,
                status=status,
                event_id=event_id,
                created_at=created_at,
            )

    def record(
        self,
        *,
        project_id: str | None,
        modality: str,
        model_id: str | None,
        operation: str,
        status: str,
        duration_ms: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost: float | None = None,
        error_category: str | None = None,
        provider_task_id: str | None = None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        event = {
            "id": event_id or str(uuid4()),
            "project_id": project_id,
            "created_at": created_at or self._utc_now().isoformat(),
            "modality": modality,
            "model_id": model_id,
            "operation": operation,
            "status": status,
            "duration_ms": max(0, int(duration_ms)) if duration_ms is not None else None,
            "input_tokens": max(0, int(input_tokens)) if input_tokens is not None else None,
            "output_tokens": max(0, int(output_tokens)) if output_tokens is not None else None,
            "cost": float(cost) if cost is not None else None,
            "error_category": error_category,
            "provider_task_id": provider_task_id,
        }
        return self.store.record_usage_event(event)

    def summary(self, *, limit: int = 100) -> dict[str, Any]:
        events = self.store.list_usage_events(limit=limit)
        grouped: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
            lambda: {
                "modality": "",
                "model_id": "",
                "operation": "",
                "calls": 0,
                "succeeded": 0,
                "failed": 0,
                "duration_ms": 0,
                "known_cost": 0.0,
                "cost_events": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            }
        )
        for event in events:
            key = (event.get("modality", "unknown"), event.get("model_id") or "unknown", event.get("operation", "unknown"))
            row = grouped[key]
            row.update({"modality": key[0], "model_id": key[1], "operation": key[2]})
            if event.get("status") not in {"cancelled", "blocked_limit"}:
                row["calls"] += 1
            if event.get("status") in {"succeeded", "completed", "ready"}:
                row["succeeded"] += 1
            elif event.get("status") in {"failed", "timed_out", "stalled", "interrupted", "uncertain"}:
                row["failed"] += 1
            row["duration_ms"] += int(event.get("duration_ms") or 0)
            row["input_tokens"] += int(event.get("input_tokens") or 0)
            row["output_tokens"] += int(event.get("output_tokens") or 0)
            if event.get("cost") is not None:
                row["known_cost"] += float(event["cost"])
                row["cost_events"] += 1
        rows = sorted(grouped.values(), key=lambda item: (item["modality"], item["model_id"], item["operation"]))
        today_count = self.today_count()
        return {
            "daily_media_limit": self.daily_media_limit,
            "media_calls_today": today_count,
            "media_calls_remaining_today": max(0, self.daily_media_limit - today_count) if self.daily_media_limit else None,
            "events": events,
            "totals": rows,
        }

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.store import ProjectRevisionConflict, ProjectStore

REVIEW_DECISIONS = {"approved", "revision_requested", "comment"}
REVIEW_CATEGORIES = {"general", "story", "continuity", "visual", "audio", "editing", "subtitle", "technical", "other"}
_TIME_RE = re.compile(r"^(\d{1,3}):([0-5]\d)$")


class ReviewError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class ReviewService:
    """Version-bound human review notes for workflow artifacts and generated media."""

    def __init__(self, store: ProjectStore):
        self.store = store

    def decorate(self, project: dict[str, Any], *, include_records: bool = True) -> dict[str, Any]:
        result = copy.deepcopy(project)
        states = self.target_states(result)
        result["review_targets"] = states
        if include_records:
            records = self.store.list_project_reviews(str(result["id"]))
            for record in records:
                state = states.get(self._target_key(record.get("target_type"), record.get("target_id")))
                record["is_current"] = bool(
                    state and state["available"] and record.get("target_revision") == state["revision"]
                )
            result["review_records"] = records
            result["review_count"] = len(records)
        else:
            result["review_count"] = self.store.count_project_reviews(str(result["id"]))
        return result

    def target_states(self, project: dict[str, Any]) -> dict[str, dict[str, Any]]:
        workflow = project.get("workflow") if isinstance(project.get("workflow"), dict) else {}
        if project.get("mode") != "workflow":
            return {}
        states: dict[str, dict[str, Any]] = {}
        for target_type, label in (("script", "脚本"), ("storyboard", "分镜")):
            item = workflow.get(target_type) if isinstance(workflow.get(target_type), dict) else {}
            content = item.get("content") if isinstance(item.get("content"), str) else ""
            revision = self._digest({
                "target": target_type,
                "content": content,
                "updated_at": item.get("updated_at"),
            })
            states[target_type] = {
                "target_type": target_type,
                "target_id": "",
                "label": label,
                "revision": revision,
                "available": bool(content.strip()) and item.get("status") in {"ready", "approved"} and not item.get("stale_reason"),
                "reason": "仅可审阅当前版本的已生成脚本或分镜。",
                "max_timecode_seconds": None,
            }

        video = workflow.get("video") if isinstance(workflow.get("video"), dict) else {}
        shots = video.get("shots") if isinstance(video.get("shots"), list) else []
        for shot in shots:
            if not isinstance(shot, dict) or not isinstance(shot.get("shot_id"), str):
                continue
            shot_id = shot["shot_id"]
            asset = shot.get("video") if isinstance(shot.get("video"), dict) else {}
            revision = self._digest({
                "target": "clip",
                "shot_id": shot_id,
                "completed_at": asset.get("completed_at"),
                "asset_version": asset.get("version"),
                "sha256": asset.get("sha256"),
            })
            key = self._target_key("clip", shot_id)
            states[key] = {
                "target_type": "clip",
                "target_id": shot_id,
                "label": f"镜头片段 · {shot_id}",
                "revision": revision,
                "available": bool(
                    asset.get("status") == "ready" and asset.get("asset_url")
                    and not asset.get("stale_reason") and not shot.get("stale")
                ),
                "reason": "仅可审阅当前生成完成且未过期的镜头片段。",
                "max_timecode_seconds": self._positive_seconds(shot.get("duration_seconds")),
            }

        final = video.get("final_video") if isinstance(video.get("final_video"), dict) else {}
        try:
            from app.services.video_assembly import VideoAssemblyService

            signature_matches = bool(
                final.get("source_signature")
                and final.get("source_signature") == VideoAssemblyService._signature(project)
            )
        except (KeyError, TypeError, ValueError):
            signature_matches = False
        final_revision = self._digest({
            "target": "final_video",
            "source_signature": final.get("source_signature"),
            "rendered_at": final.get("rendered_at"),
            "sha256": final.get("sha256"),
        })
        states["final_video"] = {
            "target_type": "final_video",
            "target_id": "",
            "label": "最终视频",
            "revision": final_revision,
            "available": bool(
                final.get("status") == "ready" and final.get("asset_url")
                and final.get("sha256") and signature_matches
            ),
            "reason": "仅可审阅与当前镜头及合成设置匹配的最终视频。",
            "max_timecode_seconds": self._positive_seconds(final.get("duration_seconds")),
        }
        return states

    def add_review(self, project: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        target_type = payload.get("target_type")
        target_id = payload.get("target_id") or ""
        if target_type not in {"script", "storyboard", "clip", "final_video"}:
            raise ReviewError("不支持的审阅目标。")
        if target_type == "clip" and not target_id:
            raise ReviewError("镜头片段审阅必须指定镜头 ID。")
        if target_type != "clip" and target_id:
            raise ReviewError("此类审阅目标不能包含镜头 ID。")
        decision = payload.get("decision")
        category = payload.get("category", "general")
        note = payload.get("note", "")
        if decision not in REVIEW_DECISIONS or category not in REVIEW_CATEGORIES:
            raise ReviewError("审阅结论或问题类型无效。")
        if not isinstance(note, str) or len(note) > 2000:
            raise ReviewError("审阅意见最多 2000 个字符。")
        note = note.strip()
        if decision != "approved" and not note:
            raise ReviewError("请填写审阅意见或修改要求。")

        states = self.target_states(project)
        state = states.get(self._target_key(target_type, target_id))
        if not state or not state["available"]:
            raise ReviewError("目标内容当前不可审阅；请先完成生成或处理过期状态。", status_code=409)
        if payload.get("target_revision") != state["revision"]:
            raise ReviewError("目标内容已更新，请刷新后再提交审阅。", status_code=409)

        timecode = payload.get("timecode")
        if timecode:
            if target_type not in {"clip", "final_video"}:
                raise ReviewError("只有视频片段和最终视频支持时间码。")
            timecode, total_seconds = self._parse_timecode(timecode)
            duration = state.get("max_timecode_seconds")
            if duration is not None and total_seconds > duration:
                raise ReviewError("时间码超出当前媒体时长。")
        else:
            timecode = None

        record = {
            "id": str(uuid4()),
            "project_id": str(project["id"]),
            "target_type": target_type,
            "target_id": target_id,
            "target_label": state["label"],
            "target_revision": state["revision"],
            "decision": decision,
            "category": category,
            "note": note,
            "timecode": timecode,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            return self.store.add_project_review(
                record, expected_updated_at=project.get("updated_at")
            )
        except ProjectRevisionConflict as error:
            raise ReviewError(str(error), status_code=409) from error

    @staticmethod
    def _target_key(target_type: Any, target_id: Any = "") -> str:
        return f"clip:{target_id}" if target_type == "clip" else str(target_type or "")

    @staticmethod
    def _digest(value: dict[str, Any]) -> str:
        canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _positive_seconds(value: Any) -> int | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value + 0.999)
        return None

    @staticmethod
    def _parse_timecode(value: Any) -> tuple[str, int]:
        if not isinstance(value, str):
            raise ReviewError("时间码格式应为 分:秒，例如 00:12。")
        match = _TIME_RE.fullmatch(value.strip())
        if not match:
            raise ReviewError("时间码格式应为 分:秒，例如 00:12。")
        minutes, seconds = (int(part) for part in match.groups())
        total = minutes * 60 + seconds
        if total > 1800:
            raise ReviewError("时间码不能超过 30 分钟。")
        return f"{minutes:02d}:{seconds:02d}", total

from __future__ import annotations

import json
import re
from typing import Any


ALLOWED_ASPECT_RATIOS = frozenset({"9:16", "16:9", "1:1", "4:3", "3:4", "2:3", "3:2", "21:9"})
MAX_SHOTS = 24
MAX_TEXT_LENGTH = 6000


class ScenePlanError(ValueError):
    pass


def _reject_json_constant(value: str) -> None:
    raise ScenePlanError(f"分镜任务 JSON 包含无效数值：{value}")


def _decode_json(content: str) -> dict[str, Any]:
    source = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", source, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        source = fenced.group(1)
    try:
        value = json.loads(source, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ScenePlanError) as exc:
        raise ScenePlanError("场景任务必须是有效 JSON。请在预览中检查格式，或重新生成任务。") from exc
    if not isinstance(value, dict):
        raise ScenePlanError("场景任务 JSON 顶层必须是对象。")
    return value


def _text(value: Any, field: str, *, required: bool = False, limit: int = MAX_TEXT_LENGTH) -> str:
    if value is None:
        result = ""
    elif isinstance(value, str):
        result = value.strip()
    else:
        raise ScenePlanError(f"字段 {field} 必须是文本。")
    if required and not result:
        raise ScenePlanError(f"每个镜头都需要填写 {field}。")
    if len(result) > limit:
        raise ScenePlanError(f"字段 {field} 不能超过 {limit} 个字符。")
    return result


def normalize_scene_plan(content: str) -> dict[str, Any]:
    """Parse and validate model-authored per-shot prompts and the AI edit plan."""
    payload = _decode_json(content)
    raw_shots = payload.get("shots")
    if not isinstance(raw_shots, list) or not raw_shots:
        raise ScenePlanError(
            "模型未返回可用镜头任务（shots 为空）。请检查已确认的脚本、视觉设定和分镜；"
            "若其中有“待确认/待补充/信息不足”等阻塞项，请回退补齐信息后再生成。"
        )
    if len(raw_shots) > MAX_SHOTS:
        raise ScenePlanError(f"一次最多支持 {MAX_SHOTS} 个镜头任务。")

    shots: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_shots, start=1):
        if not isinstance(item, dict):
            raise ScenePlanError(f"第 {index} 个镜头必须是对象。")
        shot_id = _text(item.get("shot_id") or f"S{index:02d}", "shot_id", required=True, limit=40)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", shot_id):
            raise ScenePlanError(f"镜头编号 {shot_id!r} 只能包含英文字母、数字、下划线和短横线。")
        if shot_id in seen_ids:
            raise ScenePlanError(f"镜头编号 {shot_id} 重复。")
        seen_ids.add(shot_id)

        duration = item.get("duration_seconds", 5)
        if isinstance(duration, bool) or not isinstance(duration, int):
            raise ScenePlanError(f"镜头 {shot_id} 的 duration_seconds 必须是整数。")
        if not 4 <= duration <= 12:
            raise ScenePlanError(f"镜头 {shot_id} 时长必须是 4–12 秒，以符合当前视频模型的单次限制。")

        ratio = _text(item.get("aspect_ratio") or "9:16", "aspect_ratio", required=True, limit=8)
        if ratio not in ALLOWED_ASPECT_RATIOS:
            raise ScenePlanError(f"镜头 {shot_id} 的画幅 {ratio!r} 不受支持。")

        shots.append({
            "shot_id": shot_id,
            "duration_seconds": int(duration),
            "aspect_ratio": ratio,
            "purpose": _text(item.get("purpose"), "purpose", limit=500),
            "visual_description": _text(item.get("visual_description"), "visual_description", limit=2000),
            "voiceover": _text(item.get("voiceover"), "voiceover", limit=1200),
            "screen_text": _text(item.get("screen_text"), "screen_text", limit=500),
            "transition": _text(item.get("transition"), "transition", limit=300),
            "continuity": _text(item.get("continuity"), "continuity", limit=1200),
            "image_prompt": _text(item.get("image_prompt"), "image_prompt", required=True),
            "video_prompt": _text(item.get("video_prompt"), "video_prompt", required=True),
            "negative_prompt": _text(item.get("negative_prompt"), "negative_prompt", limit=1200),
        })

    raw_edit_plan = payload.get("edit_plan") or {}
    if not isinstance(raw_edit_plan, dict):
        raise ScenePlanError("edit_plan 必须是对象。")
    raw_sequence = raw_edit_plan.get("sequence")
    sequence = [shot["shot_id"] for shot in shots]
    if isinstance(raw_sequence, list):
        requested = [str(value) for value in raw_sequence if str(value) in seen_ids]
        sequence = list(dict.fromkeys(requested + sequence))
    edit_plan = {
        "sequence": sequence,
        "pacing": _text(raw_edit_plan.get("pacing"), "edit_plan.pacing", limit=1200),
        "transitions": _text(raw_edit_plan.get("transitions"), "edit_plan.transitions", limit=1200),
        "music_direction": _text(raw_edit_plan.get("music_direction"), "edit_plan.music_direction", limit=1200),
        "sound_design": _text(raw_edit_plan.get("sound_design"), "edit_plan.sound_design", limit=1200),
        "subtitle_direction": _text(raw_edit_plan.get("subtitle_direction"), "edit_plan.subtitle_direction", limit=1200),
        "notes": _text(raw_edit_plan.get("notes"), "edit_plan.notes", limit=2000),
    }
    return {"shots": shots, "edit_plan": edit_plan}


def serialize_scene_plan(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False)


def new_shot_record(shot: dict[str, Any]) -> dict[str, Any]:
    empty_asset = {
        "status": "pending",
        "asset_url": None,
        "model": None,
        "requested_at": None,
        "completed_at": None,
        "task_id": None,
        "cost": None,
        "error": None,
        "versions": [],
    }
    return {
        **shot,
        "image": dict(empty_asset),
        "video": dict(empty_asset),
    }

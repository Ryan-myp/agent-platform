from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from app.services.production import ScenePlanError, normalize_scene_plan
from app.stages import STAGES
from app.store import ProjectStore

_PLACEHOLDERS = re.compile(r"\b(?:TODO|TBD|FIXME)\b|待补充|待填写|待确认|[（(]待定[）)]", re.IGNORECASE)
_BLOCKING = re.compile(r"无法(?:直接|继续)?(?:撰写|生成)|信息不足.{0,20}无法|必须补充.{0,20}才能继续")


class QualityService:
    """Deterministic editorial checks; these are review aids, not a quality guarantee."""

    def __init__(self, store: ProjectStore):
        self.store = store

    def check(self, project_id: str, stage: str) -> dict[str, Any]:
        project = self.store.get(project_id)
        if project is None:
            raise KeyError("找不到这个创作项目。")
        if stage not in {item["id"] for item in STAGES}:
            raise ValueError("未知的创作步骤。")
        item = project.get("workflow", {}).get(stage, {})
        content = str(item.get("content") or "")
        findings: list[dict[str, str]] = []

        def add(code: str, severity: str, title: str, message: str) -> None:
            findings.append({"code": code, "severity": severity, "title": title, "message": message})

        if not content.strip():
            add("empty_content", "error", "没有可检查的内容", "先生成或填写本步骤内容，再运行检查。")
        else:
            if len(content.strip()) < 40:
                add("very_short", "warning", "内容较短", "当前文本不足 40 个字符；请确认已覆盖本步骤需要的信息。")
            if _PLACEHOLDERS.search(content):
                add("placeholder", "warning", "发现待补充占位内容", "检查待确认、待填写或 TODO 标记，避免将占位文本带入后续制作。")
            if _BLOCKING.search(content):
                add("blocking_language", "warning", "可能包含阻塞说明", "文本包含信息不足或无法继续等表述；请核实是否需要补充真实信息。")
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            duplicates = [line for line, count in Counter(lines).items() if count > 1 and len(line) >= 12]
            if duplicates:
                add("repeated_lines", "warning", "存在重复行", f"发现 {len(duplicates)} 条重复的非空文本行，请检查是否为有意重复。")

            if stage == "scenes":
                try:
                    plan = normalize_scene_plan(content)
                    shots = plan["shots"]
                    target = int((project.get("brief") or {}).get("duration_seconds") or 0)
                    total = sum(shot["duration_seconds"] for shot in shots)
                    if target and abs(total - target) > max(5, target * 0.2):
                        add("duration_mismatch", "warning", "镜头总时长与目标时长差异较大", f"镜头合计 {total} 秒，项目目标 {target} 秒；可调整镜头时长或目标。")
                    if any(not shot.get("purpose") for shot in shots):
                        add("missing_shot_purpose", "info", "部分镜头没有明确目的", "为镜头补充叙事或传播目的，有助于后续审阅。")
                    add("scene_plan_valid", "info", "镜头任务结构有效", f"已检查 {len(shots)} 个镜头的结构、时长与提示词必填项。")
                except ScenePlanError as error:
                    add("scene_plan_invalid", "error", "镜头任务结构无效", str(error))
            elif stage == "script":
                if len(lines) < 3:
                    add("script_structure", "info", "脚本分段较少", "确认脚本是否包含清晰的场景、动作或对白层次。")
                if not re.search(r"[：:]|对白|旁白|镜头|场景", content):
                    add("script_readability", "info", "缺少明显的脚本结构标记", "可考虑标注场景、对白、旁白或动作，便于后续拆镜。")
            elif stage == "prompt" and not re.search(r"光线|色彩|风格|质感|构图|画面|视觉", content, re.IGNORECASE):
                add("visual_specificity", "info", "视觉描述可能较笼统", "可补充色彩、光线、构图或材质等可执行的视觉信息。")

        deductions = sum({"error": 35, "warning": 12, "info": 2}.get(item["severity"], 0) for item in findings)
        score = max(0, 100 - deductions) if content.strip() else 0
        return {
            "project_id": project_id,
            "stage": stage,
            "stage_label": next((item["label"] for item in STAGES if item["id"] == stage), stage),
            "score": score,
            "grade": "需修订" if any(item["severity"] == "error" for item in findings) else "建议复核" if any(item["severity"] == "warning" for item in findings) else "未发现明显问题",
            "heuristic_only": True,
            "findings": findings,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

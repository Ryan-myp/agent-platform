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
_STORY_SIGNALS = {
    "hook": re.compile(r"突(然|发)|没想到|竟然|就在.{0,16}(?!$)|发现.{0,18}(尸体|秘密|录音|线索|真相|失踪|异常)|收到.{0,16}(信|消息|电话|录音)|失踪|死亡|被困|被抓|必须.{0,16}(离开|逃|找|救|阻止)", re.IGNORECASE),
    "goal": re.compile(r"(?:想要|想|决定|必须|目标是|计划|试图|发誓|承诺|为了|要去|寻找|追查|救出|阻止).{0,36}", re.IGNORECASE),
    "obstacle": re.compile(r"但是|然而|却|阻止|拒绝|阻碍|无法|不允许|受阻|代价|威胁|失败|被迫|陷入|否则", re.IGNORECASE),
    "stakes": re.compile(r"如果.{0,40}(?:就|将|会|否则)|否则.{0,40}(?:就|将|会)|一旦.{0,40}(?:就|将|会)|失去|危及|代价|性命|生命|崩溃|暴露|毁掉|毁灭", re.IGNORECASE),
    "turn": re.compile(r"突然|没想到|原来|真相|反转|揭开|揭露|暴露|背叛|竟然|却发现|身份.{0,12}(?:是|竟)|计划.{0,12}(?:失败|被识破)", re.IGNORECASE),
}
_STORY_LABELS = {
    "hook": "开场钩子",
    "goal": "人物目标",
    "obstacle": "阻碍",
    "stakes": "风险 / 代价",
    "turn": "转折",
}


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
        narrative_review = None
        if stage == "script" and project.get("track") == "drama" and content.strip():
            dimensions = []
            for key, pattern in _STORY_SIGNALS.items():
                search_text = content[:800] if key == "hook" else content
                match = pattern.search(search_text)
                excerpt = ""
                if match:
                    start = max(0, match.start() - 32)
                    end = min(len(search_text), match.end() + 48)
                    excerpt = re.sub(r"\s+", " ", search_text[start:end]).strip()[:120]
                dimensions.append({
                    "key": key,
                    "label": _STORY_LABELS[key],
                    "status": "signal_found" if match else "not_detected",
                    "evidence": excerpt,
                })
            narrative_review = {
                "heuristic_only": True,
                "note": "仅扫描常见叙事信号；未检出不代表缺失，检出也不代表有效，需人工判断因果、人物动机与情绪力度。",
                "dimensions": dimensions,
            }
        return {
            "project_id": project_id,
            "stage": stage,
            "stage_label": next((item["label"] for item in STAGES if item["id"] == stage), stage),
            "score": score,
            "grade": "需修订" if any(item["severity"] == "error" for item in findings) else "建议复核" if any(item["severity"] == "warning" for item in findings) else "未发现明显问题",
            "heuristic_only": True,
            "findings": findings,
            "narrative_review": narrative_review,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

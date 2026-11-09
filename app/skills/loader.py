from __future__ import annotations

import re
from pathlib import Path


class SkillLoader:
    """Read local Agent Skills-style SKILL.md files; never execute skill contents."""

    def __init__(self, root: Path):
        self.root = root

    @staticmethod
    def _metadata_and_body(text: str) -> tuple[dict[str, str], str]:
        metadata: dict[str, str] = {}
        body = text
        if text.startswith("---\n"):
            parts = text.split("---", 2)
            if len(parts) == 3:
                for line in parts[1].splitlines():
                    match = re.match(r"^([a-zA-Z_-]+):\s*(.*)$", line)
                    if match:
                        metadata[match.group(1)] = match.group(2).strip().strip("\"'")
                body = parts[2].strip()
        return metadata, body

    def list_skills(self) -> list[dict[str, str]]:
        skills: list[dict[str, str]] = []
        if not self.root.exists():
            return skills
        for skill_file in sorted(self.root.glob("*/SKILL.md")):
            metadata, _ = self._metadata_and_body(skill_file.read_text(encoding="utf-8"))
            skills.append(
                {
                    "id": skill_file.parent.name,
                    "name": metadata.get("name", skill_file.parent.name),
                    "description": metadata.get("description", "本地创作技能"),
                }
            )
        return skills

    @staticmethod
    def _selected_references(skill_dir: Path, metadata: dict[str, str]) -> str:
        references: list[str] = []
        references_dir = (skill_dir / "references").resolve()
        for name in metadata.get("references", "").split(","):
            name = name.strip()
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                continue
            reference_file = references_dir / f"{name}.md"
            try:
                reference_file.resolve().relative_to(references_dir)
            except ValueError:
                continue
            if reference_file.is_file():
                references.append(
                    f"### Reference: {name}\n{reference_file.read_text(encoding='utf-8')}"
                )
        return "\n\n".join(references)

    def for_stage(self, stage: str, track: str = "marketing") -> str:
        selected: list[str] = []
        if not self.root.exists():
            return ""
        for skill_file in sorted(self.root.glob("*/SKILL.md")):
            metadata, body = self._metadata_and_body(skill_file.read_text(encoding="utf-8"))
            stages = {item.strip() for item in metadata.get("stages", "*").split(",")}
            tracks = {item.strip() for item in metadata.get("tracks", "marketing").split(",")}
            if ("*" in stages or stage in stages) and ("*" in tracks or track in tracks):
                references = self._selected_references(skill_file.parent, metadata)
                if references:
                    body = f"{body}\n\n## Selected reference material\n{references}"
                selected.append(f"## Skill: {metadata.get('name', skill_file.parent.name)}\n{body}")
        return "\n\n".join(selected)

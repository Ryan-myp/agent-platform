from pathlib import Path

from app.skills.loader import SkillLoader


def test_stage_loader_inlines_only_explicitly_selected_references(tmp_path: Path):
    skill_dir = tmp_path / "video"
    references_dir = skill_dir / "references"
    references_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: video\nstages: prompt,scenes\nreferences: prompting\n---\n\nBase guidance.",
        encoding="utf-8",
    )
    (references_dir / "prompting.md").write_text("Prompting details.", encoding="utf-8")
    (references_dir / "unlisted.md").write_text("Do not load this.", encoding="utf-8")

    loader = SkillLoader(tmp_path)

    prompt_context = loader.for_stage("prompt")
    assert "Base guidance." in prompt_context
    assert "Prompting details." in prompt_context
    assert "Do not load this." not in prompt_context
    assert loader.for_stage("script") == ""


def test_skills_are_isolated_by_track_with_legacy_skills_defaulting_to_marketing(tmp_path: Path):
    for skill_id, track, content in (
        ("marketing", "marketing", "Marketing guidance."),
        ("drama", "drama", "Drama guidance."),
    ):
        skill_dir = tmp_path / skill_id
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill_id}\nstages: script\ntracks: {track}\n---\n\n{content}",
            encoding="utf-8",
        )
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "SKILL.md").write_text(
        "---\nname: legacy\nstages: script\n---\n\nLegacy marketing guidance.",
        encoding="utf-8",
    )

    loader = SkillLoader(tmp_path)

    marketing_context = loader.for_stage("script", "marketing")
    drama_context = loader.for_stage("script", "drama")
    assert "Marketing guidance." in marketing_context
    assert "Legacy marketing guidance." in marketing_context
    assert "Drama guidance." not in marketing_context
    assert "Drama guidance." in drama_context
    assert "Marketing guidance." not in drama_context
    assert "Legacy marketing guidance." not in drama_context


def test_reference_metadata_cannot_escape_skill_reference_directory(tmp_path: Path):
    skill_dir = tmp_path / "video"
    (skill_dir / "references").mkdir(parents=True)
    (tmp_path / "outside.md").write_text("Outside content.", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: video\nstages: prompt\nreferences: ../outside\n---\n\nBase guidance.",
        encoding="utf-8",
    )

    assert "Outside content." not in SkillLoader(tmp_path).for_stage("prompt")

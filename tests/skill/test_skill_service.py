# -*- coding: utf-8 -*-
"""SkillService 领域服务单元测试。"""
from __future__ import annotations

from pathlib import Path

from agent_runtime.skill.infrastructure.filesystem_skill_repository import (
    FilesystemSkillRepository,
)
from agent_runtime.skill.skill_service import SkillService


def _write_skill(skills_dir: Path, name: str, description: str) -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n",
        encoding="utf-8",
    )


class TestSkillService:

    def test_list_skills_empty(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        repo = FilesystemSkillRepository(str(skills_dir))
        service = SkillService(repo)
        assert service.list_skills() == []

    def test_list_skills_returns_dicts(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        _write_skill(skills_dir, "my-skill", "Do something.")
        repo = FilesystemSkillRepository(str(skills_dir))
        service = SkillService(repo)
        result = service.list_skills()
        assert len(result) == 1
        assert result[0]["skill_id"] == "my-skill"
        assert result[0]["name"] == "my-skill"
        assert result[0]["description"] == "Do something."
        assert "directory_path" in result[0]

    def test_list_skills_omits_none_optional_fields(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        _write_skill(skills_dir, "simple", "Simple skill.")
        repo = FilesystemSkillRepository(str(skills_dir))
        service = SkillService(repo)
        result = service.list_skills()
        assert "license" not in result[0]
        assert "compatibility" not in result[0]
        assert "metadata" not in result[0]
        assert "allowed_tools" not in result[0]

    def test_refresh_picks_up_changes(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        repo = FilesystemSkillRepository(str(skills_dir))
        service = SkillService(repo)
        assert service.list_skills() == []

        _write_skill(skills_dir, "added", "Added later.")
        service.refresh()
        assert len(service.list_skills()) == 1

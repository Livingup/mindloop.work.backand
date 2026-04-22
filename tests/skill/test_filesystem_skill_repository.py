# -*- coding: utf-8 -*-
"""FilesystemSkillRepository 单元测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.skill.infrastructure.filesystem_skill_repository import (
    FilesystemSkillRepository,
)


def _write_skill(skills_dir: Path, name: str, frontmatter: str) -> Path:
    """在 skills_dir 下创建 skill 目录和 SKILL.md。"""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(frontmatter, encoding="utf-8")
    return skill_dir


class TestFilesystemSkillRepository:
    """文件系统 Skill 仓储测试。"""

    def test_scan_empty_dir(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        repo = FilesystemSkillRepository(str(skills_dir))
        assert repo.list_all() == []

    def test_scan_nonexistent_dir(self, tmp_path: Path) -> None:
        repo = FilesystemSkillRepository(str(tmp_path / "nonexistent"))
        assert repo.list_all() == []

    def test_scan_discovers_valid_skill(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        _write_skill(
            skills_dir,
            "pdf-processing",
            "---\nname: pdf-processing\ndescription: Extract PDF text.\n---\nBody",
        )
        repo = FilesystemSkillRepository(str(skills_dir))
        skills = repo.list_all()
        assert len(skills) == 1
        assert skills[0].skill_id == "pdf-processing"
        assert skills[0].name == "pdf-processing"
        assert skills[0].description == "Extract PDF text."
        assert skills[0].directory_path == str((skills_dir / "pdf-processing").resolve())

    def test_scan_discovers_multiple_skills(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        _write_skill(skills_dir, "alpha", "---\nname: alpha\ndescription: Alpha skill.\n---\n")
        _write_skill(skills_dir, "beta", "---\nname: beta\ndescription: Beta skill.\n---\n")
        repo = FilesystemSkillRepository(str(skills_dir))
        names = sorted(s.name for s in repo.list_all())
        assert names == ["alpha", "beta"]

    def test_scan_parses_optional_fields(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        _write_skill(
            skills_dir,
            "data-analysis",
            (
                "---\n"
                "name: data-analysis\n"
                "description: Analyze data.\n"
                "license: Apache-2.0\n"
                "compatibility: Requires Python 3.14+\n"
                "metadata:\n"
                "  author: test-org\n"
                "  version: '1.0'\n"
                "allowed-tools: Bash(git:*) Read\n"
                "---\n"
                "Body\n"
            ),
        )
        repo = FilesystemSkillRepository(str(skills_dir))
        skills = repo.list_all()
        assert len(skills) == 1
        s = skills[0]
        assert s.license == "Apache-2.0"
        assert s.compatibility == "Requires Python 3.14+"
        assert s.metadata == {"author": "test-org", "version": "1.0"}
        assert s.allowed_tools == "Bash(git:*) Read"

    def test_scan_skips_dir_without_skill_md(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        (skills_dir / "no-skill-md").mkdir(parents=True)
        _write_skill(skills_dir, "valid", "---\nname: valid\ndescription: Valid.\n---\n")
        repo = FilesystemSkillRepository(str(skills_dir))
        assert len(repo.list_all()) == 1
        assert repo.list_all()[0].name == "valid"

    def test_scan_skips_name_mismatch(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        _write_skill(
            skills_dir,
            "actual-dir",
            "---\nname: wrong-name\ndescription: Mismatch.\n---\n",
        )
        repo = FilesystemSkillRepository(str(skills_dir))
        assert repo.list_all() == []

    def test_scan_skips_invalid_frontmatter(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skill_dir = skills_dir / "bad-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("No frontmatter here", encoding="utf-8")
        repo = FilesystemSkillRepository(str(skills_dir))
        assert repo.list_all() == []

    def test_scan_skips_missing_description(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        _write_skill(
            skills_dir, "no-desc", "---\nname: no-desc\n---\n"
        )
        repo = FilesystemSkillRepository(str(skills_dir))
        assert repo.list_all() == []

    def test_refresh_picks_up_new_skill(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        repo = FilesystemSkillRepository(str(skills_dir))
        assert repo.list_all() == []

        _write_skill(skills_dir, "new-skill", "---\nname: new-skill\ndescription: New.\n---\n")
        repo.scan()
        assert len(repo.list_all()) == 1
        assert repo.list_all()[0].name == "new-skill"

    def test_scan_ignores_files_in_root(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "README.md").write_text("Not a skill", encoding="utf-8")
        repo = FilesystemSkillRepository(str(skills_dir))
        assert repo.list_all() == []

# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_runtime.skill.infrastructure.filesystem_skill_repository import (
    FilesystemSkillRepository,
    _parse_skill_md,
)
from agent_runtime.skill.skill_definition import SkillDefinition


def _write_skill(skill_dir: Path, *, name: str, description: str = "测试 Skill") -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


class SkillDefinitionValidationTests(unittest.TestCase):
    def test_accepts_non_lowercase_safe_skill_name(self) -> None:
        definition = SkillDefinition(
            skill_id="Software-Engineering",
            name="Software-Engineering",
            description="用于软件工程任务。",
            directory_path="C:/skills/Software-Engineering",
        )

        definition.validate()

    def test_rejects_unsafe_skill_name_characters(self) -> None:
        unsafe_names = (
            " has-leading-space",
            "has trailing-space ",
            "contains space",
            "contains/slash",
            "contains\\backslash",
            "contains\ncontrol",
        )

        for name in unsafe_names:
            with self.subTest(name=name):
                definition = SkillDefinition(
                    skill_id=name,
                    name=name,
                    description="用于软件工程任务。",
                    directory_path="C:/skills/unsafe",
                )
                with self.assertRaises(ValueError):
                    definition.validate()


class FilesystemSkillRepositoryTests(unittest.TestCase):
    def test_scan_uses_frontmatter_name_as_skill_id_without_directory_name_match(self) -> None:
        with TemporaryDirectory() as tmp:
            skills_dir = Path(tmp)
            _write_skill(
                skills_dir / "Display-Style-Directory",
                name="DevOps",
                description="用于 DevOps 任务。",
            )

            repo = FilesystemSkillRepository(lambda: str(skills_dir))
            skills = repo.list_all()

        self.assertEqual(len(skills), 1)
        skill = skills[0]
        self.assertEqual(skill.skill_id, "DevOps")
        self.assertEqual(skill.name, "DevOps")
        self.assertTrue(skill.directory_path.endswith("Display-Style-Directory"))

    def test_scan_skips_later_duplicate_frontmatter_names_deterministically(self) -> None:
        with TemporaryDirectory() as tmp:
            skills_dir = Path(tmp)
            _write_skill(skills_dir / "A-first", name="SharedSkill", description="第一个 Skill。")
            _write_skill(skills_dir / "Z-second", name="SharedSkill", description="重复 Skill。")

            with self.assertLogs(
                "agent_runtime.skill.infrastructure.filesystem_skill_repository",
                level="WARNING",
            ) as logs:
                repo = FilesystemSkillRepository(lambda: str(skills_dir))
            skills = repo.list_all()

        self.assertEqual([skill.skill_id for skill in skills], ["SharedSkill"])
        self.assertTrue(skills[0].directory_path.endswith("A-first"))
        self.assertIn("发现重复 Skill ID 'SharedSkill'", "\n".join(logs.output))
        self.assertIn("跳过目录", "\n".join(logs.output))

    def test_parse_allows_frontmatter_name_that_differs_from_physical_directory_name(self) -> None:
        with TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "Physical-Container"
            _write_skill(skill_dir, name="RuntimeSkill", description="用于运行时任务。")

            skill = _parse_skill_md(skill_dir / "SKILL.md", skill_dir)
            skill.validate()

        self.assertEqual(skill.skill_id, "RuntimeSkill")
        self.assertEqual(skill.name, "RuntimeSkill")
        self.assertTrue(skill.directory_path.endswith("Physical-Container"))


if __name__ == "__main__":
    unittest.main()

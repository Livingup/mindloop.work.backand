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
    def test_accepts_directory_name_safe_skill_name(self) -> None:
        definition = SkillDefinition(
            skill_id="Software Engineering",
            name="Software Engineering",
            description="用于软件工程任务。",
            directory_path="C:/skills/Software Engineering",
        )

        definition.validate()

    def test_rejects_directory_name_unsafe_skill_name_characters(self) -> None:
        unsafe_names = (
            ".",
            "..",
            "has trailing-space ",
            "ends-with-dot.",
            "contains/slash",
            "contains\\backslash",
            "contains:colon",
            "contains*star",
            'contains"quote',
            "contains<less",
            "contains>greater",
            "contains|pipe",
            "contains?question",
            "NUL",
            "COM1",
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
                name="DevOps Skill",
                description="用于 DevOps 任务。",
            )

            repo = FilesystemSkillRepository(lambda: str(skills_dir))
            skills = repo.list_all()

        self.assertEqual(len(skills), 1)
        skill = skills[0]
        self.assertEqual(skill.skill_id, "DevOps Skill")
        self.assertEqual(skill.name, "DevOps Skill")
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
            _write_skill(skill_dir, name="Runtime Skill", description="用于运行时任务。")

            skill = _parse_skill_md(skill_dir / "SKILL.md", skill_dir)
            skill.validate()

        self.assertEqual(skill.skill_id, "Runtime Skill")
        self.assertEqual(skill.name, "Runtime Skill")
        self.assertTrue(skill.directory_path.endswith("Physical-Container"))

    def test_list_all_auto_refreshes_created_modified_and_deleted_skills(self) -> None:
        with TemporaryDirectory() as tmp:
            skills_dir = Path(tmp)
            _write_skill(skills_dir / "A", name="SkillA", description="旧描述")
            repo = FilesystemSkillRepository(
                lambda: str(skills_dir),
                snapshot_check_interval_seconds_accessor=lambda: 0.0,
            )

            self.assertEqual([skill.skill_id for skill in repo.list_all()], ["SkillA"])

            _write_skill(skills_dir / "B", name="SkillB", description="新增 Skill")
            self.assertEqual([skill.skill_id for skill in repo.list_all()], ["SkillA", "SkillB"])

            _write_skill(skills_dir / "A", name="SkillA", description="更新后的长描述")
            skill_a = repo.get_by_id("SkillA")
            self.assertEqual(skill_a.description, "更新后的长描述")

            (skills_dir / "B" / "SKILL.md").unlink()
            self.assertEqual([skill.skill_id for skill in repo.list_all()], ["SkillA"])
            with self.assertRaises(KeyError):
                repo.get_by_id("SkillB")

    def test_auto_refresh_can_be_disabled_for_cached_reads(self) -> None:
        with TemporaryDirectory() as tmp:
            skills_dir = Path(tmp)
            _write_skill(skills_dir / "A", name="SkillA", description="旧描述")
            repo = FilesystemSkillRepository(
                lambda: str(skills_dir),
                auto_refresh_enabled_accessor=lambda: False,
                snapshot_check_interval_seconds_accessor=lambda: 0.0,
            )

            _write_skill(skills_dir / "B", name="SkillB", description="新增 Skill")
            self.assertEqual([skill.skill_id for skill in repo.list_all()], ["SkillA"])

            repo.scan()
            self.assertEqual([skill.skill_id for skill in repo.list_all()], ["SkillA", "SkillB"])


if __name__ == "__main__":
    unittest.main()

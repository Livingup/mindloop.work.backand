# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_runtime.skill.builtin_skill_installer import (
    BUILTIN_CONFIG_MANAGEMENT_SKILL_ID,
    BuiltinSkillInstaller,
)


def _write_source_skill(source_root: Path, *, body: str = "initial") -> Path:
    skill_dir = source_root / BUILTIN_CONFIG_MANAGEMENT_SKILL_ID
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {BUILTIN_CONFIG_MANAGEMENT_SKILL_ID}\n"
        "description: Built-in config management skill.\n"
        "---\n\n"
        f"# {body}\n",
        encoding="utf-8",
    )
    return skill_dir


class BuiltinSkillInstallerTests(unittest.TestCase):
    def test_installs_and_then_skips_existing_skill_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "builtin-skills"
            target_root = root / "user-skills"
            _write_source_skill(source_root)
            installer = BuiltinSkillInstaller(source_root=source_root)

            first = installer.install_all(target_root)[0]
            second = installer.install_all(target_root)[0]

            target = target_root / BUILTIN_CONFIG_MANAGEMENT_SKILL_ID
            skill_md = (target / "SKILL.md").read_text(encoding="utf-8")
            manifest_exists = (target / "manifest.json").exists()

        self.assertEqual(first.status, "installed")
        self.assertTrue(first.changed)
        self.assertIsNone(first.version)
        self.assertIsNone(first.source_hash)
        self.assertEqual(second.status, "unchanged")
        self.assertFalse(second.changed)
        self.assertIn("# initial", skill_md)
        self.assertFalse(manifest_exists)

    def test_skips_existing_skill_directory_when_source_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "builtin-skills"
            target_root = root / "user-skills"
            _write_source_skill(source_root, body="old")
            installer = BuiltinSkillInstaller(source_root=source_root)
            installer.install_all(target_root)

            _write_source_skill(source_root, body="new")
            result = installer.install_all(target_root)[0]
            target = target_root / BUILTIN_CONFIG_MANAGEMENT_SKILL_ID
            skill_md = (target / "SKILL.md").read_text(encoding="utf-8")
            manifest_exists = (target / "manifest.json").exists()

        self.assertEqual(result.status, "unchanged")
        self.assertFalse(result.changed)
        self.assertIn("# old", skill_md)
        self.assertNotIn("# new", skill_md)
        self.assertFalse(manifest_exists)

    def test_skips_existing_user_directory_without_overwrite(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "builtin-skills"
            target_root = root / "user-skills"
            _write_source_skill(source_root)
            user_dir = target_root / BUILTIN_CONFIG_MANAGEMENT_SKILL_ID
            user_dir.mkdir(parents=True)
            (user_dir / "SKILL.md").write_text("user content", encoding="utf-8")
            installer = BuiltinSkillInstaller(source_root=source_root)

            result = installer.install_all(target_root)[0]
            content = (user_dir / "SKILL.md").read_text(encoding="utf-8")
            manifest_exists = (user_dir / "manifest.json").exists()

        self.assertEqual(result.status, "unchanged")
        self.assertFalse(result.changed)
        self.assertEqual(content, "user content")
        self.assertFalse(manifest_exists)

    def test_skips_conflict_when_target_path_is_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "builtin-skills"
            target_root = root / "user-skills"
            _write_source_skill(source_root)
            target_root.mkdir(parents=True)
            target = target_root / BUILTIN_CONFIG_MANAGEMENT_SKILL_ID
            target.write_text("not a directory", encoding="utf-8")
            installer = BuiltinSkillInstaller(source_root=source_root)

            result = installer.install_all(target_root)[0]
            content = target.read_text(encoding="utf-8")

        self.assertEqual(result.status, "skipped_conflict")
        self.assertFalse(result.changed)
        self.assertEqual(content, "not a directory")
        self.assertFalse(target.is_dir())

    def test_concurrent_first_install_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "builtin-skills"
            target_root = root / "user-skills"
            _write_source_skill(source_root)
            installer = BuiltinSkillInstaller(source_root=source_root)

            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda _: installer.install_all(target_root)[0], range(4)))

            target = target_root / BUILTIN_CONFIG_MANAGEMENT_SKILL_ID
            skill_md_exists = (target / "SKILL.md").exists()
            manifest_exists = (target / "manifest.json").exists()

        statuses = [result.status for result in results]
        self.assertEqual(statuses.count("installed"), 1)
        self.assertEqual(statuses.count("unchanged"), 3)
        self.assertTrue(skill_md_exists)
        self.assertFalse(manifest_exists)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Installer for Spread AI built-in Skills."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BUILTIN_CONFIG_MANAGEMENT_SKILL_ID = "spread-ai-config-management"
BUILTIN_SKILL_MANAGED_BY = "spread-ai-runtime"
_BUILTIN_SKILLS_DIR_ENV = "SPREAD_AI_BUILTIN_SKILLS_DIR"


@dataclass(frozen=True, slots=True)
class BuiltinSkillInstallResult:
    """Result of installing or checking one built-in Skill."""

    skill_id: str
    target_path: str
    status: str
    message: str
    version: str | None = None
    source_hash: str | None = None

    @property
    def changed(self) -> bool:
        return self.status in {"installed", "upgraded"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "target_path": self.target_path,
            "status": self.status,
            "message": self.message,
            "version": self.version,
            "source_hash": self.source_hash,
            "changed": self.changed,
        }


class BuiltinSkillInstaller:
    """Install release-bundled Skills into the user configured Skill repository.

    Built-in Skills are copied as managed packages. Existing user directories are never
    overwritten unless their manifest declares the same package as managed by this runtime.
    """

    def __init__(self, *, source_root: Path | None = None) -> None:
        self._source_root = source_root

    def install_all(self, skills_dir: str | Path) -> list[BuiltinSkillInstallResult]:
        """Install every built-in Skill into the given user Skill repository."""
        return [self.install_skill(BUILTIN_CONFIG_MANAGEMENT_SKILL_ID, skills_dir)]

    def install_skill(self, skill_id: str, skills_dir: str | Path) -> BuiltinSkillInstallResult:
        """Install one built-in Skill idempotently."""
        target_root = Path(skills_dir).expanduser()
        target = target_root / skill_id
        source = self._resolve_source_root() / skill_id
        if not source.is_dir():
            result = BuiltinSkillInstallResult(
                skill_id=skill_id,
                target_path=str(target),
                status="source_missing",
                message=f"内置 Skill 源目录不存在: {source}",
            )
            logger.warning(result.message)
            return result

        manifest = _read_json_object(source / "manifest.json")
        version = str(manifest.get("version") or "0.0.0")
        managed_by = str(manifest.get("managed_by") or "")
        package_id = str(manifest.get("package_id") or skill_id)
        if managed_by != BUILTIN_SKILL_MANAGED_BY or package_id != skill_id:
            result = BuiltinSkillInstallResult(
                skill_id=skill_id,
                target_path=str(target),
                status="source_invalid",
                message="内置 Skill manifest 缺少正确的 managed_by/package_id 标记。",
                version=version,
            )
            logger.error(result.message)
            return result

        source_hash = _directory_hash(source)
        target_root.mkdir(parents=True, exist_ok=True)

        if target.exists():
            if not target.is_dir():
                return BuiltinSkillInstallResult(
                    skill_id=skill_id,
                    target_path=str(target),
                    status="skipped_conflict",
                    message="目标路径已存在但不是目录，已跳过内置 Skill 安装。",
                    version=version,
                    source_hash=source_hash,
                )
            target_manifest = _read_json_object(target / "manifest.json")
            target_managed_by = str(target_manifest.get("managed_by") or "")
            target_package_id = str(target_manifest.get("package_id") or "")
            if target_managed_by != BUILTIN_SKILL_MANAGED_BY or target_package_id != skill_id:
                return BuiltinSkillInstallResult(
                    skill_id=skill_id,
                    target_path=str(target),
                    status="skipped_conflict",
                    message="同名 Skill 目录不是 Spread AI 托管内置包，已保留用户目录并跳过覆盖。",
                    version=version,
                    source_hash=source_hash,
                )
            if (
                str(target_manifest.get("source_hash") or "") == source_hash
                and str(target_manifest.get("version") or "") == version
            ):
                return BuiltinSkillInstallResult(
                    skill_id=skill_id,
                    target_path=str(target),
                    status="unchanged",
                    message="内置 Skill 已是最新版本。",
                    version=version,
                    source_hash=source_hash,
                )
            self._replace_managed_skill(source, target, version=version, source_hash=source_hash)
            return BuiltinSkillInstallResult(
                skill_id=skill_id,
                target_path=str(target),
                status="upgraded",
                message="内置 Skill 已升级。",
                version=version,
                source_hash=source_hash,
            )

        self._replace_managed_skill(source, target, version=version, source_hash=source_hash)
        return BuiltinSkillInstallResult(
            skill_id=skill_id,
            target_path=str(target),
            status="installed",
            message="内置 Skill 已安装。",
            version=version,
            source_hash=source_hash,
        )

    def _replace_managed_skill(
        self,
        source: Path,
        target: Path,
        *,
        version: str,
        source_hash: str,
    ) -> None:
        target_parent = target.parent
        backup: Path | None = None
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        with tempfile.TemporaryDirectory(prefix=f".{target.name}.", dir=target_parent) as temp_dir:
            staged = Path(temp_dir) / target.name
            shutil.copytree(source, staged, ignore=_copy_ignore)
            _write_installed_manifest(staged / "manifest.json", version=version, source_hash=source_hash)
            if target.exists():
                backup = target_parent / f".{target.name}.backup-{timestamp}"
                target.rename(backup)
            try:
                staged.rename(target)
            except Exception:
                if backup is not None and backup.exists() and not target.exists():
                    backup.rename(target)
                raise
            finally:
                if backup is not None and backup.exists():
                    shutil.rmtree(backup, ignore_errors=True)

    def _resolve_source_root(self) -> Path:
        if self._source_root is not None:
            return self._source_root

        candidates: list[Path] = []
        env_path = os.environ.get(_BUILTIN_SKILLS_DIR_ENV)
        if env_path:
            candidates.append(Path(env_path).expanduser())

        frozen_root = getattr(sys, "_MEIPASS", None)
        if frozen_root:
            candidates.append(Path(frozen_root) / "builtin-skills")

        repo_root = Path(__file__).resolve().parents[3]
        candidates.append(repo_root / "builtin-skills")

        package_root = Path(__file__).resolve().parents[1]
        candidates.append(package_root / "builtin_skills")

        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        return candidates[0] if candidates else Path("builtin-skills")


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in {".git", "__pycache__"} or name.endswith(".pyc") or name.endswith(".pyo"):
            ignored.add(name)
    return ignored


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_installed_manifest(path: Path, *, version: str, source_hash: str) -> None:
    manifest = _read_json_object(path)
    manifest.update(
        {
            "package_id": BUILTIN_CONFIG_MANAGEMENT_SKILL_ID,
            "managed_by": BUILTIN_SKILL_MANAGED_BY,
            "version": version,
            "source_hash": source_hash,
            "installed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _directory_hash(root: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()

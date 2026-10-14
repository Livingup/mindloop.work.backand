# -*- coding: utf-8 -*-
"""Installer for Spread AI built-in Skills."""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

logger = logging.getLogger(__name__)

BUILTIN_CONFIG_MANAGEMENT_SKILL_ID = "spread-ai-config-management"
_BUILTIN_SKILLS_DIR_ENV = "SPREAD_AI_BUILTIN_SKILLS_DIR"
_INSTALL_LOCK_TIMEOUT_SECONDS = 10.0
_INSTALL_LOCK_STALE_SECONDS = 120.0
_INSTALL_LOCK_POLL_SECONDS = 0.05

_INSTALL_PROCESS_LOCK = RLock()


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

    Built-in Skills are installed only when the corresponding target Skill directory is
    absent. Existing target directories are skipped and never overwritten. The installer
    does not read, write, or copy Skill manifest metadata. A process lock plus a
    target-root lockfile protects concurrent first installs.
    """

    def __init__(self, *, source_root: Path | None = None) -> None:
        self._source_root = source_root

    def install_all(self, skills_dir: str | Path) -> list[BuiltinSkillInstallResult]:
        """Ensure every built-in Skill directory exists in the user Skill repository."""
        return [self.install_skill(BUILTIN_CONFIG_MANAGEMENT_SKILL_ID, skills_dir)]

    def install_skill(self, skill_id: str, skills_dir: str | Path) -> BuiltinSkillInstallResult:
        """Install one built-in Skill if its target directory is absent."""
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

        try:
            target_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            result = BuiltinSkillInstallResult(
                skill_id=skill_id,
                target_path=str(target),
                status="target_unavailable",
                message=f"Skill 目标目录不可用: {target_root}: {exc}",
            )
            logger.warning(result.message)
            return result

        lock_path = target_root / f".{skill_id}.install.lock"
        with _INSTALL_PROCESS_LOCK:
            try:
                with _install_lockfile(lock_path):
                    return self._install_skill_locked(skill_id, source, target)
            except TimeoutError as exc:
                result = BuiltinSkillInstallResult(
                    skill_id=skill_id,
                    target_path=str(target),
                    status="skipped_locked",
                    message=f"另一个进程正在安装内置 Skill，已跳过本次安装: {exc}",
                )
                logger.warning(result.message)
                return result

    def _install_skill_locked(
        self,
        skill_id: str,
        source: Path,
        target: Path,
    ) -> BuiltinSkillInstallResult:
        """Install after process/file lock acquisition; re-check target state inside lock."""
        if target.exists():
            if target.is_dir():
                return BuiltinSkillInstallResult(
                    skill_id=skill_id,
                    target_path=str(target),
                    status="unchanged",
                    message="目标 Skill 目录已存在，跳过安装。",
                )
            return BuiltinSkillInstallResult(
                skill_id=skill_id,
                target_path=str(target),
                status="skipped_conflict",
                message="目标路径已存在但不是目录，已跳过内置 Skill 安装。",
            )

        self._replace_managed_skill(source, target)
        return BuiltinSkillInstallResult(
            skill_id=skill_id,
            target_path=str(target),
            status="installed",
            message="内置 Skill 已安装。",
        )

    def _replace_managed_skill(
        self,
        source: Path,
        target: Path,
    ) -> None:
        target_parent = target.parent
        backup: Path | None = None
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        with tempfile.TemporaryDirectory(prefix=f".{target.name}.", dir=target_parent) as temp_dir:
            staged = Path(temp_dir) / target.name
            shutil.copytree(source, staged, ignore=_copy_ignore)
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


@contextmanager
def _install_lockfile(lock_path: Path) -> Iterator[None]:
    """Acquire a best-effort cross-process install lock using an exclusive lockfile."""
    fd: int | None = None
    deadline = time.monotonic() + _INSTALL_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
            )
            os.write(fd, payload.encode("utf-8"))
            break
        except FileExistsError:
            if _lockfile_is_stale(lock_path):
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(lock_path)
            time.sleep(_INSTALL_LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("删除内置 Skill 安装锁文件失败: %s", lock_path, exc_info=True)


def _lockfile_is_stale(lock_path: Path) -> bool:
    try:
        age_seconds = time.time() - lock_path.stat().st_mtime
    except OSError:
        return False
    return age_seconds > _INSTALL_LOCK_STALE_SECONDS


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if (
            name in {".git", "__pycache__", "manifest.json"}
            or name.endswith(".pyc")
            or name.endswith(".pyo")
            or name.endswith(".lock")
        ):
            ignored.add(name)
    return ignored

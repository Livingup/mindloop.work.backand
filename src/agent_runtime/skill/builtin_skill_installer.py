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
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

logger = logging.getLogger(__name__)

BUILTIN_CONFIG_MANAGEMENT_SKILL_ID = "spread-ai-config-management"
BUILTIN_SKILL_MANAGED_BY = "spread-ai-runtime"
_BUILTIN_SKILLS_DIR_ENV = "SPREAD_AI_BUILTIN_SKILLS_DIR"
_SOURCE_METADATA_CACHE_TTL_SECONDS = 30.0
_INSTALL_LOCK_TIMEOUT_SECONDS = 10.0
_INSTALL_LOCK_STALE_SECONDS = 120.0
_INSTALL_LOCK_POLL_SECONDS = 0.05

_INSTALL_PROCESS_LOCK = RLock()
_SOURCE_METADATA_CACHE_LOCK = RLock()
_SOURCE_METADATA_CACHE: dict[str, tuple[float, "_BuiltinSkillSourceMetadata"]] = {}


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


@dataclass(frozen=True, slots=True)
class _BuiltinSkillSourceMetadata:
    package_id: str
    managed_by: str
    version: str
    source_hash: str
    manifest_state: tuple[int, int]


class BuiltinSkillInstaller:
    """Install release-bundled Skills into the user configured Skill repository.

    Built-in Skills are copied as managed packages. Existing user directories are never
    overwritten unless their manifest declares the same package as managed by this runtime.
    A process lock plus a target-root lockfile protects concurrent first installs/upgrades.
    """

    def __init__(self, *, source_root: Path | None = None) -> None:
        self._source_root = source_root

    def install_all(self, skills_dir: str | Path) -> list[BuiltinSkillInstallResult]:
        """Install every built-in Skill into the given user Skill repository."""
        return [self.install_skill(BUILTIN_CONFIG_MANAGEMENT_SKILL_ID, skills_dir)]

    def install_skill(self, skill_id: str, skills_dir: str | Path) -> BuiltinSkillInstallResult:
        """Install one built-in Skill idempotently and conflict-safely."""
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
        """Install after process/file lock acquisition; re-checks target state inside lock."""
        source_metadata = _load_source_metadata(source)
        version = source_metadata.version
        source_hash = source_metadata.source_hash
        if (
            source_metadata.managed_by != BUILTIN_SKILL_MANAGED_BY
            or source_metadata.package_id != skill_id
        ):
            result = BuiltinSkillInstallResult(
                skill_id=skill_id,
                target_path=str(target),
                status="source_invalid",
                message="内置 Skill manifest 缺少正确的 managed_by/package_id 标记。",
                version=version,
                source_hash=source_hash,
            )
            logger.error(result.message)
            return result

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


def _load_source_metadata(source: Path) -> _BuiltinSkillSourceMetadata:
    manifest_path = source / "manifest.json"
    manifest = _read_json_object(manifest_path)
    try:
        manifest_stat = manifest_path.stat()
        manifest_state = (manifest_stat.st_mtime_ns, manifest_stat.st_size)
    except OSError:
        manifest_state = (0, 0)

    cache_key = str(source.resolve())
    now = time.monotonic()
    with _SOURCE_METADATA_CACHE_LOCK:
        cached = _SOURCE_METADATA_CACHE.get(cache_key)
        if cached is not None:
            cached_at, metadata = cached
            if (
                now - cached_at < _SOURCE_METADATA_CACHE_TTL_SECONDS
                and metadata.manifest_state == manifest_state
            ):
                return metadata

    metadata = _BuiltinSkillSourceMetadata(
        package_id=str(manifest.get("package_id") or source.name),
        managed_by=str(manifest.get("managed_by") or ""),
        version=str(manifest.get("version") or "0.0.0"),
        source_hash=_directory_hash(source),
        manifest_state=manifest_state,
    )
    with _SOURCE_METADATA_CACHE_LOCK:
        _SOURCE_METADATA_CACHE[cache_key] = (now, metadata)
    return metadata


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if (
            name in {".git", "__pycache__"}
            or name.endswith(".pyc")
            or name.endswith(".pyo")
            or name.endswith(".lock")
        ):
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

# -*- coding: utf-8 -*-
"""Skill 仓储文件系统实现。

扫描指定目录下的子目录，解析每个子目录中 SKILL.md 的 YAML frontmatter，
构建 SkillDefinition 实体列表并缓存。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable

import yaml

from agent_runtime.skill.skill_definition import SkillDefinition

logger = logging.getLogger(__name__)

# SKILL.md 文件名（大小写敏感）。
_SKILL_MD = "SKILL.md"


@dataclass(frozen=True, slots=True)
class _SkillDirectorySnapshot:
    """Skill 仓库目录的轻量快照。

    只记录目录结构和 SKILL.md 的 stat 信息，避免无变化时读取 Markdown 正文。
    """

    skills_dir: str
    root_state: str
    entries: tuple[tuple[str, bool, int | None, int | None], ...]


class FilesystemSkillRepository:
    """基于文件系统的 Skill 仓储实现。

    启动时扫描 skills_dir 下的一级子目录，解析 SKILL.md frontmatter。
    读取时先执行低成本目录快照检查；仅当目录结构或 SKILL.md stat 变化时才重新扫描正文。
    """

    def __init__(
        self,
        skills_dir_accessor: Callable[[], str],
        *,
        auto_refresh_enabled_accessor: Callable[[], bool] | None = None,
        snapshot_check_interval_seconds_accessor: Callable[[], float] | None = None,
    ) -> None:
        self._skills_dir_accessor = skills_dir_accessor
        self._auto_refresh_enabled_accessor = auto_refresh_enabled_accessor
        self._snapshot_check_interval_seconds_accessor = snapshot_check_interval_seconds_accessor
        self._skills: list[SkillDefinition] = []
        self._skills_by_id: dict[str, SkillDefinition] = {}
        self._snapshot: _SkillDirectorySnapshot | None = None
        self._last_snapshot_checked_at = 0.0
        self._lock = RLock()
        self.scan()

    @property
    def _skills_dir(self) -> Path:
        """实时从配置中心读取 Skill 仓库根目录路径（避免配置数据冗余）。"""
        return Path(self._skills_dir_accessor()).expanduser()

    def list_all(self) -> list[SkillDefinition]:
        """列出所有已发现的 Skill；返回前自动检查仓库元数据是否需要刷新。"""
        self._ensure_fresh()
        with self._lock:
            return list(self._skills)

    def get_by_id(self, skill_id: str) -> SkillDefinition:
        """根据 skill_id 获取 Skill；返回前自动检查仓库元数据是否需要刷新。"""
        self._ensure_fresh()
        with self._lock:
            try:
                return self._skills_by_id[skill_id]
            except KeyError as exc:
                raise KeyError(f"Skill '{skill_id}' 不存在") from exc

    def scan(self) -> None:
        """强制重新扫描 Skill 仓库目录，刷新内部缓存。

        扫描逻辑：
        1. 遍历 skills_dir 下的一级子目录
        2. 检查子目录中是否存在 SKILL.md
        3. 解析 SKILL.md 的 YAML frontmatter
        4. 构建 SkillDefinition 并校验
        5. 校验失败的 Skill 跳过并记录警告
        6. 若多个目录声明同一 frontmatter name/skill_id，保留排序最靠前目录并跳过后续重复项
        """
        skills_dir = self._skills_dir
        snapshot = self._build_snapshot(skills_dir)
        with self._lock:
            self._replace_cache_from_scan(skills_dir, snapshot)

    def _ensure_fresh(self) -> None:
        """在读路径上执行轻量快照检查，必要时刷新 Skill 元数据缓存。"""
        if not self._auto_refresh_enabled():
            return

        now = time.monotonic()
        with self._lock:
            if (
                self._snapshot is not None
                and now - self._last_snapshot_checked_at < self._snapshot_check_interval_seconds()
            ):
                return

        skills_dir = self._skills_dir
        snapshot = self._build_snapshot(skills_dir)
        with self._lock:
            if self._snapshot is not None and snapshot == self._snapshot:
                self._last_snapshot_checked_at = time.monotonic()
                return
            self._replace_cache_from_scan(skills_dir, snapshot)

    def _auto_refresh_enabled(self) -> bool:
        if self._auto_refresh_enabled_accessor is None:
            return True
        try:
            return bool(self._auto_refresh_enabled_accessor())
        except Exception:
            logger.warning("读取 Skill 自动刷新配置失败，默认启用自动刷新。", exc_info=True)
            return True

    def _snapshot_check_interval_seconds(self) -> float:
        if self._snapshot_check_interval_seconds_accessor is None:
            return 1.0
        try:
            return max(0.0, float(self._snapshot_check_interval_seconds_accessor()))
        except Exception:
            logger.warning("读取 Skill 快照检查间隔配置失败，默认使用 1 秒。", exc_info=True)
            return 1.0

    def _build_snapshot(self, skills_dir: Path) -> _SkillDirectorySnapshot:
        resolved_dir = str(skills_dir.resolve())
        if not skills_dir.exists():
            return _SkillDirectorySnapshot(resolved_dir, "missing", tuple())
        if not skills_dir.is_dir():
            return _SkillDirectorySnapshot(resolved_dir, "not_dir", tuple())

        entries: list[tuple[str, bool, int | None, int | None]] = []
        try:
            children = sorted(skills_dir.iterdir(), key=lambda item: item.name)
        except OSError:
            logger.warning("读取 Skill 仓库目录失败: %s", skills_dir, exc_info=True)
            return _SkillDirectorySnapshot(resolved_dir, "unreadable", tuple())

        for entry in children:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue

            skill_md = entry / _SKILL_MD
            try:
                stat_result = skill_md.stat()
            except FileNotFoundError:
                entries.append((entry.name, False, None, None))
            except OSError:
                entries.append((entry.name, False, None, None))
            else:
                entries.append((entry.name, True, stat_result.st_mtime_ns, stat_result.st_size))
        return _SkillDirectorySnapshot(resolved_dir, "directory", tuple(entries))

    def _replace_cache_from_scan(
        self,
        skills_dir: Path,
        snapshot: _SkillDirectorySnapshot,
    ) -> None:
        if snapshot.root_state == "missing":
            logger.warning("Skill 仓库目录不存在: %s", skills_dir)
            self._replace_cache([], snapshot)
            return

        if snapshot.root_state == "not_dir":
            logger.warning("Skill 仓库路径不是目录: %s", skills_dir)
            self._replace_cache([], snapshot)
            return

        if snapshot.root_state == "unreadable":
            self._replace_cache([], snapshot)
            return

        discovered = self._scan_directory(skills_dir)
        self._replace_cache(discovered, snapshot)
        logger.info(
            "Skill 仓库扫描完成: %s，共发现 %d 个 Skill",
            skills_dir,
            len(discovered),
        )

    def _replace_cache(
        self,
        skills: list[SkillDefinition],
        snapshot: _SkillDirectorySnapshot,
    ) -> None:
        self._skills = skills
        self._skills_by_id = {skill.skill_id: skill for skill in skills}
        self._snapshot = snapshot
        self._last_snapshot_checked_at = time.monotonic()

    def _scan_directory(self, skills_dir: Path) -> list[SkillDefinition]:
        discovered: list[SkillDefinition] = []
        discovered_by_id: dict[str, SkillDefinition] = {}
        for entry in sorted(skills_dir.iterdir(), key=lambda item: item.name):
            if not entry.is_dir():
                continue

            skill_md = entry / _SKILL_MD
            if not skill_md.exists():
                logger.debug("跳过目录 %s：未找到 %s", entry.name, _SKILL_MD)
                continue

            try:
                skill = _parse_skill_md(skill_md, entry)
                skill.validate()
                existing = discovered_by_id.get(skill.skill_id)
                if existing is not None:
                    logger.warning(
                        "发现重复 Skill ID '%s'：保留目录 %s，跳过目录 %s",
                        skill.skill_id,
                        existing.directory_path,
                        skill.directory_path,
                    )
                    continue
                discovered_by_id[skill.skill_id] = skill
                discovered.append(skill)
            except Exception:
                logger.warning("解析 Skill '%s' 失败，已跳过", entry.name, exc_info=True)
        return discovered


def _parse_skill_md(skill_md: Path, skill_dir: Path) -> SkillDefinition:
    """解析 SKILL.md 的 YAML frontmatter，构建 SkillDefinition。

    Args:
        skill_md: SKILL.md 文件路径。
        skill_dir: Skill 目录路径。

    Returns:
        解析后的 SkillDefinition 实体。

    Raises:
        ValueError: frontmatter 格式无效或缺少必填字段。
    """
    content = skill_md.read_text(encoding="utf-8")
    frontmatter = _extract_frontmatter(content)
    if frontmatter is None:
        raise ValueError(f"{skill_md} 中未找到有效的 YAML frontmatter")

    data = yaml.safe_load(frontmatter)
    if not isinstance(data, dict):
        raise ValueError(f"{skill_md} 的 YAML frontmatter 不是对象结构")

    name = data.get("name")
    if not name:
        raise ValueError(f"{skill_md} 缺少必填字段 name")

    description = data.get("description")
    if not description:
        raise ValueError(f"{skill_md} 缺少必填字段 description")

    # 解析可选字段。
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"{skill_md} 的 metadata 字段必须是字符串映射")
    # 确保 metadata 值都是字符串。
    metadata = {str(k): str(v) for k, v in metadata.items()}

    return SkillDefinition(
        skill_id=name,
        name=name,
        description=str(description),
        directory_path=str(skill_dir.resolve()),
        license=_opt_str(data.get("license")),
        compatibility=_opt_str(data.get("compatibility")),
        metadata=metadata,
        allowed_tools=_opt_str(data.get("allowed-tools")),
    )


def _extract_frontmatter(content: str) -> str | None:
    """从 Markdown 内容中提取 YAML frontmatter。

    frontmatter 格式：文件以 '---' 开头，以第二个 '---' 结束。
    """
    stripped = content.lstrip()
    if not stripped.startswith("---"):
        return None

    # 跳过第一个 '---' 行。
    after_first = stripped[3:]
    if after_first.startswith("\n"):
        after_first = after_first[1:]
    elif after_first.startswith("\r\n"):
        after_first = after_first[2:]
    else:
        return None

    end_idx = after_first.find("\n---")
    if end_idx == -1:
        return None

    return after_first[:end_idx]


def _opt_str(value: object) -> str | None:
    """将值转为可选字符串。"""
    if value is None:
        return None
    return str(value)

# -*- coding: utf-8 -*-
"""Skill 仓储文件系统实现。

扫描指定目录下的子目录，解析每个子目录中 SKILL.md 的 YAML frontmatter，
构建 SkillDefinition 实体列表并缓存。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import yaml

from agent_runtime.skill.skill_definition import SkillDefinition

logger = logging.getLogger(__name__)

# SKILL.md 文件名（大小写敏感）。
_SKILL_MD = "SKILL.md"


class FilesystemSkillRepository:
    """基于文件系统的 Skill 仓储实现。

    启动时扫描 skills_dir 下的一级子目录，解析 SKILL.md frontmatter。
    扫描结果缓存在内存中，调用 scan() 可刷新。
    """

    def __init__(self, skills_dir_accessor: Callable[[], str]) -> None:
        self._skills_dir_accessor = skills_dir_accessor
        self._skills: list[SkillDefinition] = []
        self.scan()

    @property
    def _skills_dir(self) -> Path:
        """实时从配置中心读取 Skill 仓库根目录路径（避免配置数据冗余）。"""
        return Path(self._skills_dir_accessor()).expanduser()

    def list_all(self) -> list[SkillDefinition]:
        """列出所有已发现的 Skill。"""
        return list(self._skills)

    def scan(self) -> None:
        """重新扫描 Skill 仓库目录，刷新内部缓存。

        扫描逻辑：
        1. 遍历 skills_dir 下的一级子目录
        2. 检查子目录中是否存在 SKILL.md
        3. 解析 SKILL.md 的 YAML frontmatter
        4. 构建 SkillDefinition 并校验
        5. 校验失败的 Skill 跳过并记录警告
        """
        if not self._skills_dir.exists():
            logger.warning("Skill 仓库目录不存在: %s", self._skills_dir)
            self._skills = []
            return

        if not self._skills_dir.is_dir():
            logger.warning("Skill 仓库路径不是目录: %s", self._skills_dir)
            self._skills = []
            return

        discovered: list[SkillDefinition] = []
        for entry in sorted(self._skills_dir.iterdir()):
            if not entry.is_dir():
                continue

            skill_md = entry / _SKILL_MD
            if not skill_md.exists():
                logger.debug("跳过目录 %s：未找到 %s", entry.name, _SKILL_MD)
                continue

            try:
                skill = _parse_skill_md(skill_md, entry)
                skill.validate()
                discovered.append(skill)
            except Exception:
                logger.warning("解析 Skill '%s' 失败，已跳过", entry.name, exc_info=True)

        self._skills = discovered
        logger.info(
            "Skill 仓库扫描完成: %s，共发现 %d 个 Skill",
            self._skills_dir,
            len(discovered),
        )


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

    dir_name = skill_dir.name
    if name != dir_name:
        raise ValueError(
            f"{skill_md} 中 name '{name}' 与目录名 '{dir_name}' 不一致"
        )

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

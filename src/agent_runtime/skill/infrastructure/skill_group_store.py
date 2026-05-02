# -*- coding: utf-8 -*-
"""Skill 分组映射存储。

应用层扩展：Skill 标准协议（SKILL.md）不包含分组概念，
本文件负责在独立 YAML 文件中维护 skill_id → group 的映射关系。

存储格式（~/.spread-ai/skill-groups.yaml）：
    skill_id_1: "组名A"
    skill_id_2: "组名B"
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import yaml

logger = logging.getLogger(__name__)


class SkillGroupStore:
    """Skill 分组映射的 YAML 文件存储。

    提供 skill_id → group 的读写能力。
    文件不存在时视为空映射，所有 Skill 默认分组为 'default'。
    """

    DEFAULT_GROUP = "default"

    def __init__(self, group_file_accessor: Callable[[], str]) -> None:
        """初始化。

        Args:
            group_file_accessor: 分组文件路径访问器，每次调用时实时从配置中心读取。
        """
        self._group_file_accessor = group_file_accessor

    @property
    def _group_file(self) -> Path:
        """实时获取分组文件路径。"""
        return Path(self._group_file_accessor())

    def get_all(self) -> dict[str, str]:
        """读取所有分组映射。

        Returns:
            {skill_id: group} 字典，文件不存在或解析失败时返回空字典。
        """
        path = self._group_file
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
            return {}
        except Exception:
            logger.warning("读取 Skill 分组文件失败: %s", path, exc_info=True)
            return {}

    def get_group(self, skill_id: str) -> str:
        """获取指定 Skill 的分组。"""
        return self.get_all().get(skill_id, self.DEFAULT_GROUP)

    def set_group(self, skill_id: str, group: str) -> None:
        """设置指定 Skill 的分组。

        Args:
            skill_id: Skill 唯一标识。
            group: 分组名称。传入 'default' 时删除该条目（恢复默认）。
        """
        groups = self.get_all()
        if group == self.DEFAULT_GROUP:
            groups.pop(skill_id, None)
        else:
            groups[skill_id] = group
        self._save(groups)

    def _save(self, groups: dict[str, str]) -> None:
        """持久化分组映射到 YAML 文件。"""
        path = self._group_file
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(groups, f, default_flow_style=False, allow_unicode=True)

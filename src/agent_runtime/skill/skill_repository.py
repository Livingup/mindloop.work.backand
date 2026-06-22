# -*- coding: utf-8 -*-
"""Skill 仓储接口（Protocol）。

定义在领域层，实现在基础设施层（依赖反转）。
"""
from __future__ import annotations

from typing import Protocol

from agent_runtime.skill.skill_definition import SkillDefinition


class SkillRepository(Protocol):
    """Skill 仓储接口。

    集合语义，提供 Skill 定义的查询与刷新能力。
    """

    def list_all(self) -> list[SkillDefinition]:
        """列出所有已发现的 Skill。实现应保证返回前完成必要的新鲜度检查。"""
        ...

    def get_by_id(self, skill_id: str) -> SkillDefinition:
        """根据 skill_id 获取 Skill。实现应保证返回前完成必要的新鲜度检查。"""
        ...

    def scan(self) -> None:
        """重新扫描 Skill 仓库目录，刷新内部缓存。"""
        ...

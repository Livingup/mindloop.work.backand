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
        """列出所有已发现的 Skill。"""
        ...

    def scan(self) -> None:
        """重新扫描 Skill 仓库目录，刷新内部缓存。"""
        ...

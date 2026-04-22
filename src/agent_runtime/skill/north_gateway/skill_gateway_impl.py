# -*- coding: utf-8 -*-
"""Skill 北向网关实现。"""
from __future__ import annotations

from agent_runtime.skill.north_gateway.skill_contracts import SkillInfo
from agent_runtime.skill.skill_service import SkillService


class SkillGatewayImpl:
    """Skill 北向网关实现。

    将领域服务的返回值转换为北向网关 DTO。
    """

    def __init__(self, service: SkillService) -> None:
        self._service = service

    def list_skills(self) -> list[SkillInfo]:
        """列出所有已发现的 Skill。"""
        items = self._service.list_skills()
        return [
            SkillInfo(
                skill_id=item["skill_id"],
                name=item["name"],
                description=item["description"],
                directory_path=item["directory_path"],
                license=item.get("license"),
                compatibility=item.get("compatibility"),
                metadata=item.get("metadata", {}),
                allowed_tools=item.get("allowed_tools"),
            )
            for item in items
        ]

    def refresh(self) -> None:
        """触发重新扫描 Skill 仓库目录。"""
        self._service.refresh()

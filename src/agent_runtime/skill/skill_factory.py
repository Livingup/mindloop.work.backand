# -*- coding: utf-8 -*-
"""Skill 模块依赖装配。"""
from __future__ import annotations

from agent_runtime.skill.infrastructure.filesystem_skill_repository import (
    FilesystemSkillRepository,
)
from agent_runtime.skill.north_gateway.skill_gateway_impl import SkillGatewayImpl
from agent_runtime.skill.skill_config import SkillConfig
from agent_runtime.skill.skill_service import SkillService


class SkillFactory:
    """Skill 模块工厂。"""

    @staticmethod
    def create(config: SkillConfig) -> SkillGatewayImpl:
        """根据配置创建 Skill 北向网关实例。

        装配链路：文件系统仓储 → 领域服务 → 北向网关。
        """
        repo = FilesystemSkillRepository(config.skills_dir)
        service = SkillService(repo)
        return SkillGatewayImpl(service)

# -*- coding: utf-8 -*-
"""Skill 模块依赖装配。"""
from __future__ import annotations

from agent_runtime.skill.infrastructure.filesystem_skill_repository import (
    FilesystemSkillRepository,
)
from agent_runtime.skill.infrastructure.skill_group_store import SkillGroupStore
from agent_runtime.skill.north_gateway.skill_gateway_impl import SkillGatewayImpl
from agent_runtime.skill.skill_service import SkillService


class SkillFactory:
    """Skill 模块工厂。"""

    @staticmethod
    def create() -> SkillGatewayImpl:
        """创建 Skill 北向网关实例。

        配置通过访问器从 ConfigRegistry 实时读取，不存储配置副本。
        装配链路：文件系统仓储 → 领域服务 → 北向网关（+ 分组存储）。
        """
        from agent_runtime.config import ConfigRegistry

        repo = FilesystemSkillRepository(
            skills_dir_accessor=lambda: ConfigRegistry.instance().skill_service.skills_dir,
        )
        group_store = SkillGroupStore(
            group_file_accessor=lambda: ConfigRegistry.instance().skill_service.skill_groups_file,
        )
        service = SkillService(repo)
        return SkillGatewayImpl(service, group_store)

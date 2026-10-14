# -*- coding: utf-8 -*-
"""Agent 定义领域服务。

负责 Agent 静态模板定义的增删改查、启用禁用和列表筛选业务规则。
服务只依赖仓储接口，不直接操作 YAML 文件系统。
"""
from __future__ import annotations

import logging

from agent_runtime.agent_definition.agent_definition import AgentDefinition
from agent_runtime.agent_definition.agent_definition_repository import (
    AgentDefinitionRepository,
)
from agent_runtime.agent_definition.north_gateway.agent_definition_contracts import (
    AgentDefinitionCreateRequest,
    AgentDefinitionUpdateRequest,
)

logger = logging.getLogger(__name__)


class AgentDefinitionService:
    """Agent 定义领域服务。

    该服务承担 Agent 定义管理用例的业务编排：
    - 创建时确保 agent_id 全局唯一；
    - 更新时只替换调用方明确传入的字段；
    - 启用/禁用仅调整 disabled 标记，不删除定义文件；
    - 列表查询支持按分组和禁用状态筛选，供管理后台、CLI 和 Agent 发现流程共用。
    """

    def __init__(self, repository: AgentDefinitionRepository) -> None:
        # Agent 定义仓储。该仓储负责静态 Agent 模板 YAML 的持久化与查询，
        # 是 Agent 执行引擎后续按 agent_id 装配系统提示词、工具、Skill 和子 Agent 的数据基础。
        self._repository = repository

    def list_definitions(
        self,
        *,
        group: str | None = None,
        include_disabled: bool = True,
    ) -> list[AgentDefinition]:
        """列出 Agent 定义。

        Args:
            group: 可选分组过滤。管理后台可用该条件只展示某一业务域的 Agent。
            include_disabled: 是否包含已禁用定义。用户选择 Agent 时通常传 False，管理后台通常传 True。
        """
        definitions = self._repository.find_all()
        if group is not None:
            definitions = [item for item in definitions if item.group == group]
        if not include_disabled:
            definitions = [item for item in definitions if not item.disabled]
        return definitions

    def get_definition(self, agent_id: str) -> AgentDefinition:
        """按 agent_id 获取 Agent 定义。不存在时抛出 KeyError。"""
        definition = self._repository.find_by_id(agent_id)
        if definition is None:
            raise KeyError(f"Agent 定义不存在: {agent_id}")
        return definition

    def create_definition(
        self,
        request: AgentDefinitionCreateRequest,
    ) -> AgentDefinition:
        """创建 Agent 定义。agent_id 已存在时抛出 ValueError。"""
        if self._repository.contains(request.agent_id):
            raise ValueError(f"Agent 定义已存在: {request.agent_id}")
        definition = request.to_definition()
        self._repository.save(definition)
        logger.info("创建 Agent 定义: %s", request.agent_id)
        return definition

    def update_definition(
        self,
        agent_id: str,
        request: AgentDefinitionUpdateRequest,
    ) -> AgentDefinition:
        """部分更新 Agent 定义。目标定义不存在时抛出 KeyError。"""
        current = self.get_definition(agent_id)
        updated = request.apply_to(current)
        self._repository.save(updated)
        logger.info("更新 Agent 定义: %s", agent_id)
        return updated

    def delete_definition(self, agent_id: str) -> None:
        """删除 Agent 定义。不存在时静默跳过，便于幂等清理。"""
        if self._repository.delete(agent_id):
            logger.info("删除 Agent 定义: %s", agent_id)

    def enable_definition(self, agent_id: str) -> AgentDefinition:
        """启用 Agent 定义。

        启用只修改 disabled=False，不改变其它能力配置，适合管理后台恢复某个暂时下线的 Agent。
        """
        current = self.get_definition(agent_id)
        updated = AgentDefinition(
            agent_id=current.agent_id,
            name=current.name,
            description=current.description,
            system_prompt=current.system_prompt,
            tool_ids=current.tool_ids,
            mcp_tools=current.mcp_tools,
            skill_ids=current.skill_ids,
            available_sub_agent_ids=current.available_sub_agent_ids,
            default_llm_policy_group_id=current.default_llm_policy_group_id,
            allowed_llm_policy_group_ids=current.allowed_llm_policy_group_ids,
            group=current.group,
            builtin_background_agent=current.builtin_background_agent,
            disabled=False,
        )
        self._repository.save(updated)
        logger.info("启用 Agent 定义: %s", agent_id)
        return updated

    def disable_definition(self, agent_id: str) -> AgentDefinition:
        """禁用 Agent 定义。

        禁用后定义仍保留在仓储中，供管理后台查看和后续恢复，但默认不应被 Agent 执行流程选用。
        """
        current = self.get_definition(agent_id)
        updated = AgentDefinition(
            agent_id=current.agent_id,
            name=current.name,
            description=current.description,
            system_prompt=current.system_prompt,
            tool_ids=current.tool_ids,
            mcp_tools=current.mcp_tools,
            skill_ids=current.skill_ids,
            available_sub_agent_ids=current.available_sub_agent_ids,
            default_llm_policy_group_id=current.default_llm_policy_group_id,
            allowed_llm_policy_group_ids=current.allowed_llm_policy_group_ids,
            group=current.group,
            builtin_background_agent=current.builtin_background_agent,
            disabled=True,
        )
        self._repository.save(updated)
        logger.info("禁用 Agent 定义: %s", agent_id)
        return updated

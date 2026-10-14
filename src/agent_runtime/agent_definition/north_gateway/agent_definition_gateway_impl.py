# -*- coding: utf-8 -*-
"""Agent 定义管理北向网关实现。"""
from __future__ import annotations

from agent_runtime.agent_definition.agent_definition import AgentDefinition
from agent_runtime.agent_definition.agent_definition_service import AgentDefinitionService
from agent_runtime.agent_definition.north_gateway.agent_definition_contracts import (
    AgentDefinitionCreateRequest,
    AgentDefinitionUpdateRequest,
)


class AgentDefinitionGatewayImpl:
    """Agent 定义管理北向网关。

    网关是 Agent 定义服务的唯一对外入口。CLI、REST 和后续 Agent 执行引擎统一通过本网关访问
    Agent 静态模板数据，避免外部调用方直接依赖领域服务或仓储实现。
    """

    def __init__(self, service: AgentDefinitionService) -> None:
        # Agent 定义领域服务。网关只做北向入口转发，具体业务规则由领域服务与聚合根承担。
        self._service = service

    def list_agent_definitions(
        self,
        *,
        group: str | None = None,
        include_disabled: bool = True,
    ) -> list[AgentDefinition]:
        """列出 Agent 定义，可按分组和禁用状态筛选。"""
        return self._service.list_definitions(
            group=group,
            include_disabled=include_disabled,
        )

    def get_agent_definition(self, agent_id: str) -> AgentDefinition:
        """根据 agent_id 获取 Agent 定义。未找到时抛出 KeyError。"""
        return self._service.get_definition(agent_id)

    def create_agent_definition(
        self,
        request: AgentDefinitionCreateRequest,
    ) -> AgentDefinition:
        """创建 Agent 定义。agent_id 已存在时抛出 ValueError。"""
        return self._service.create_definition(request)

    def update_agent_definition(
        self,
        agent_id: str,
        request: AgentDefinitionUpdateRequest,
    ) -> AgentDefinition:
        """更新 Agent 定义。目标定义不存在时抛出 KeyError。"""
        return self._service.update_definition(agent_id, request)

    def delete_agent_definition(self, agent_id: str) -> None:
        """删除 Agent 定义。不存在时静默跳过。"""
        self._service.delete_definition(agent_id)

    def enable_agent_definition(self, agent_id: str) -> AgentDefinition:
        """启用 Agent 定义。"""
        return self._service.enable_definition(agent_id)

    def disable_agent_definition(self, agent_id: str) -> AgentDefinition:
        """禁用 Agent 定义。"""
        return self._service.disable_definition(agent_id)

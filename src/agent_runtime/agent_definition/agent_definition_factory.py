# -*- coding: utf-8 -*-
"""Agent 定义服务依赖装配。"""
from __future__ import annotations

from agent_runtime.agent_definition.agent_definition_service import AgentDefinitionService
from agent_runtime.agent_definition.infrastructure.yaml_agent_definition_repository import (
    YamlAgentDefinitionRepository,
)
from agent_runtime.agent_definition.north_gateway.agent_definition_gateway_impl import (
    AgentDefinitionGatewayImpl,
)


class AgentDefinitionFactory:
    """Agent 定义服务工厂。

    工厂是唯一知道具体仓储实现和 ConfigRegistry 的装配位置。
    领域层只依赖 Callable 目录访问器和仓储接口，不感知全局配置注册中心。
    """

    @staticmethod
    def create() -> AgentDefinitionGatewayImpl:
        """创建 Agent 定义北向网关实例。

        配置通过访问器从 ConfigRegistry 实时读取，不存储配置副本。
        装配链路：YAML 文件仓储 → 领域服务 → 北向网关。
        """
        from agent_runtime.config import ConfigRegistry

        if ConfigRegistry.instance().cloud_user_config_database.is_postgresql_enabled:
            from agent_runtime.agent_definition.infrastructure.postgres_agent_definition_repository import (
                PostgresAgentDefinitionRepository,
            )
            from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore

            store = PostgresUserConfigStore(
                lambda: ConfigRegistry.instance().cloud_user_config_database,
            )
            repository = PostgresAgentDefinitionRepository(lambda: store)
        else:
            repository = YamlAgentDefinitionRepository(
                definitions_dir_accessor=(
                    lambda: ConfigRegistry.instance().agent_definition.definitions_dir
                ),
            )
        service = AgentDefinitionService(repository)
        return AgentDefinitionGatewayImpl(service)

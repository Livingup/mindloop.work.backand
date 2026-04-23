# -*- coding: utf-8 -*-
"""MCP 客户端子模块工厂。

负责复杂对象创建和依赖装配，是唯一知道所有具体实现类的地方。
"""

from __future__ import annotations

from agent_runtime.mcp.mcp_client.mcp_client_service import McpClientService
from agent_runtime.mcp.mcp_client.infrastructure.mcp_session_manager import (
    McpSessionManager,
)
from agent_runtime.mcp.mcp_client.infrastructure.yaml_mcp_service_repository import (
    YamlMcpServiceRepository,
)
from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_gateway_impl import (
    McpClientGatewayImpl,
)


class McpClientFactory:
    """MCP 客户端子模块工厂。

    组装仓储、会话管理器、领域服务和北向网关。
    """

    @staticmethod
    def create() -> McpClientGatewayImpl:
        """创建 MCP 客户端北向网关实例。

        配置通过访问器从 ConfigRegistry 实时读取，不存储配置副本。

        Returns:
            McpClientGatewayImpl 北向网关实例（满足 McpClientGateway 契约）。
        """
        from agent_runtime.config import ConfigRegistry

        repository = YamlMcpServiceRepository(
            config_dir_accessor=lambda: ConfigRegistry.instance().mcp_client.config_dir,
        )
        session_manager = McpSessionManager()
        service = McpClientService(repository, session_manager)
        gateway = McpClientGatewayImpl(service, session_manager)

        return gateway

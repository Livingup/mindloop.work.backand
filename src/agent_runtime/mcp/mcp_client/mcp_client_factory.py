# -*- coding: utf-8 -*-
"""MCP 客户端子模块工厂。

负责复杂对象创建和依赖装配，是唯一知道所有具体实现类的地方。
"""

from __future__ import annotations

from pathlib import Path

from agent_runtime.mcp.mcp_client.mcp_client_config import McpClientConfig
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
    def create(config: McpClientConfig) -> McpClientGatewayImpl:
        """根据配置创建 MCP 客户端北向网关实例。

        Args:
            config: MCP 客户端服务配置。

        Returns:
            McpClientGatewayImpl 北向网关实例（满足 McpClientGateway 契约）。
        """
        config_dir = Path(config.config_dir)
        repository = YamlMcpServiceRepository(config_dir)
        session_manager = McpSessionManager()
        service = McpClientService(repository, session_manager)
        gateway = McpClientGatewayImpl(service, session_manager)

        return gateway

# -*- coding: utf-8 -*-
"""MCP 客户端子模块工厂。

负责复杂对象创建和依赖装配，是唯一知道所有具体实现类的地方。
"""

from __future__ import annotations

from pathlib import Path

from agent_runtime.mcp.mcp_client.mcp_client_service import McpClientService
from agent_runtime.mcp.mcp_client.infrastructure.mcp_session_manager import (
    McpSessionManager,
)
from agent_runtime.mcp.mcp_client.infrastructure.yaml_mcp_service_repository import (
    YamlMcpServiceRepository,
)
from agent_runtime.mcp.mcp_client.mcp_metadata_cache import (
    JsonMcpMetadataSnapshotRepository,
    global_mcp_metadata_registry,
)
from agent_runtime.mcp.mcp_client.mcp_metadata_refresh_manager import (
    McpMetadataRefreshManager,
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

        local_repository = YamlMcpServiceRepository(
            config_dir_accessor=lambda: ConfigRegistry.instance().mcp_client.config_dir,
        )
        if ConfigRegistry.instance().cloud_user_config_database.is_postgresql_enabled:
            from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore
            from agent_runtime.mcp.mcp_client.infrastructure.postgres_mcp_service_repository import (
                PostgresStreamableHttpMcpServiceRepository,
                TransportRoutingMcpServiceRepository,
            )

            store = PostgresUserConfigStore(
                lambda: ConfigRegistry.instance().cloud_user_config_database,
            )
            repository = TransportRoutingMcpServiceRepository(
                local_repository=local_repository,
                shared_repository=PostgresStreamableHttpMcpServiceRepository(lambda: store),
            )
        else:
            repository = local_repository

        metadata_repository = JsonMcpMetadataSnapshotRepository(
            cache_dir_accessor=lambda: str(
                Path(ConfigRegistry.instance().mcp_client.config_dir).expanduser() / "metadata-cache"
            ),
        )
        metadata_registry = global_mcp_metadata_registry()
        metadata_registry.hydrate(metadata_repository.load_all())

        session_manager = McpSessionManager()
        service = McpClientService(repository, session_manager)
        metadata_refresh_manager = McpMetadataRefreshManager(
            service=service,
            registry=metadata_registry,
            snapshot_repository=metadata_repository,
        )
        gateway = McpClientGatewayImpl(
            service,
            session_manager,
            metadata_registry=metadata_registry,
            snapshot_repository=metadata_repository,
            metadata_refresh_manager=metadata_refresh_manager,
        )

        return gateway

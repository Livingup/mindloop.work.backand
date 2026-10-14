# -*- coding: utf-8 -*-
"""MCP 客户端北向网关实现。

将 McpClientService 领域服务适配为北向网关契约（McpClientGateway），
负责将内部领域对象转换为对外 DTO 数据模型。
"""

from __future__ import annotations

from typing import Any

from agent_runtime.mcp.mcp_client.mcp_client_service import McpClientService
from agent_runtime.mcp.mcp_client.mcp_service_definition import McpServiceDefinition
from agent_runtime.mcp.mcp_client.mcp_transport_config import (
    StdioTransportConfig,
    StreamableHttpTransportConfig,
    TransportConfig,
)
from agent_runtime.mcp.mcp_client.infrastructure.mcp_session_manager import (
    McpSessionManager,
)
from agent_runtime.mcp.mcp_client.mcp_metadata_cache import (
    JsonMcpMetadataSnapshotRepository,
    McpMetadataRegistry,
)
from agent_runtime.mcp.mcp_client.mcp_metadata_refresh_manager import (
    McpMetadataRefreshManager,
)
from agent_runtime.mcp.mcp_client.north_gateway.mcp_client_contracts import (
    McpResourceContentDTO,
    McpResourceInfoDTO,
    McpServiceAddRequest,
    McpServiceUpdateRequest,
    McpServiceDetailResult,
    McpServiceInfo,
    McpServiceSummary,
    McpTestResult,
    McpToolInfoDTO,
    McpToolResultDTO,
    McpToolSummaryDTO,
)


class McpClientGatewayImpl:
    """McpClientGateway 契约的具体实现。

    防腐层：外部看到的是 DTO 数据模型，内部自由使用领域对象。
    """

    def __init__(
        self,
        service: McpClientService,
        session_manager: McpSessionManager,
        *,
        metadata_registry: McpMetadataRegistry,
        snapshot_repository: JsonMcpMetadataSnapshotRepository,
        metadata_refresh_manager: McpMetadataRefreshManager,
    ) -> None:
        self._service = service
        self._session_manager = session_manager
        self._metadata_registry = metadata_registry
        self._snapshot_repository = snapshot_repository
        self._metadata_refresh_manager = metadata_refresh_manager

    # ══════════════════════════════════════════
    # 配置管理
    # ══════════════════════════════════════════

    def list_services(self) -> list[McpServiceSummary]:
        """列出所有已配置的 MCP 服务。"""
        definitions = self._service.list_services()
        items: list[McpServiceSummary] = []
        for d in definitions:
            status = self._service.get_connection_status(d.service_id)
            items.append(
                McpServiceSummary(
                    service_id=d.service_id,
                    name=d.name,
                    description=d.description,
                    transport_type=d.transport_type,
                    enabled=d.enabled,
                    connected=status.connected,
                    connection_status=status.status,
                    last_error=status.last_error,
                    retry_attempt=status.retry_attempt,
                    next_retry_at=status.next_retry_at,
                    group=d.group,
                )
            )
        return items

    def get_service(self, service_id: str) -> McpServiceInfo:
        """获取服务配置详情。"""
        d = self._service.get_service(service_id)
        status = self._service.get_connection_status(service_id)
        return McpServiceInfo(
            service_id=d.service_id,
            name=d.name,
            description=d.description,
            transport=d.transport.to_dict(),
            enabled=d.enabled,
            connected=status.connected,
            connection_status=status.status,
            last_error=status.last_error,
            retry_attempt=status.retry_attempt,
            next_retry_at=status.next_retry_at,
            group=d.group,
            usage_guide=d.usage_guide,
        )

    def add_service(self, config: McpServiceAddRequest) -> None:
        """添加 MCP 服务配置。"""
        transport = self._build_transport_config(config.transport)
        definition = McpServiceDefinition(
            service_id=config.service_id,
            name=config.name,
            transport=transport,
            description=config.description,
            enabled=config.enabled,
            group=config.group,
            usage_guide=config.usage_guide,
        )
        self._service.add_service(definition)

    def update_service(self, service_id: str, update: McpServiceUpdateRequest) -> None:
        """更新 MCP 服务配置（部分更新）。"""
        kwargs: dict[str, Any] = {}
        if update.name is not None:
            kwargs["name"] = update.name
        if update.description is not None:
            kwargs["description"] = update.description
        if update.transport is not None:
            kwargs["transport"] = self._build_transport_config(update.transport)
        if update.group is not None:
            kwargs["group"] = update.group
        if update.usage_guide is not None:
            kwargs["usage_guide"] = update.usage_guide
        self._service.update_service(service_id, **kwargs)
        self._remove_metadata_snapshot(service_id)

    @staticmethod
    def _build_transport_config(transport_dict: dict) -> TransportConfig:
        """从嵌套字典构建 TransportConfig 值对象。"""
        transport_type = transport_dict.get("type", "stdio")
        if transport_type == "stdio":
            return StdioTransportConfig.from_dict(transport_dict)
        elif transport_type == "streamable-http":
            return StreamableHttpTransportConfig.from_dict(transport_dict)
        else:
            raise ValueError(f"不支持的传输方式: {transport_type}")

    def _remove_metadata_snapshot(self, service_id: str) -> None:
        """清理本地缓存中的指定服务元数据快照。"""
        self._metadata_registry.remove(service_id)
        self._snapshot_repository.delete(service_id)

    async def remove_service(self, service_id: str) -> None:
        """删除 MCP 服务配置并断开连接。"""
        await self._service.remove_service(service_id)
        self._remove_metadata_snapshot(service_id)

    def enable_service(self, service_id: str) -> None:
        """启用 MCP 服务。"""
        self._service.enable_service(service_id)

    def disable_service(self, service_id: str) -> None:
        """禁用 MCP 服务。"""
        self._service.disable_service(service_id)
        self._remove_metadata_snapshot(service_id)

    async def test_connection(self, service_id: str) -> McpTestResult:
        """测试连接。"""
        try:
            caps = await self._service.test_connection(service_id)
            return McpTestResult(
                success=True,
                server_name=caps.server_name,
                server_version=caps.server_version,
                supports_tools=caps.supports_tools,
                supports_resources=caps.supports_resources,
            )
        except Exception as e:
            return McpTestResult(success=False, error=str(e))

    # ══════════════════════════════════════════
    # 协议操作
    # ══════════════════════════════════════════

    async def connect_all_enabled(self) -> dict[str, str | None]:
        """启动时尝试连接所有已启用的 MCP 服务。

        单个服务连接失败不影响其他服务。

        Returns:
            {service_id: None} 成功，{service_id: "错误信息"} 失败。
        """
        return await self._service.connect_all_enabled()

    async def connect_service(self, service_id: str) -> None:
        """手动连接指定 MCP 服务。"""
        await self._service.connect_service(service_id)

    async def reconcile_connections(self) -> None:
        """根据当前配置状态对齐连接池。

        断开已删除/禁用的连接，连接新增已启用的服务。
        """
        await self._service.reconcile_connections()

    async def reconcile_if_config_changed(self) -> None:
        """若配置目录 mtime 变化，则 reconcile 连接池。

        懒检测：REST 调用前执行，感知 CLI / 手动编辑导致的配置变更。
        """
        await self._service.reconcile_if_config_changed()

    async def load_cached_metadata(self) -> None:
        """从本地持久化缓存加载 MCP 元数据到内存。"""
        await self._metadata_refresh_manager.load_persisted_metadata()

    def start_metadata_refresh(self) -> None:
        """启动 MCP 元数据后台刷新轮询。"""
        self._metadata_refresh_manager.start()

    async def stop_metadata_refresh(self) -> None:
        """停止 MCP 元数据后台刷新轮询。"""
        await self._metadata_refresh_manager.stop()

    async def refresh_metadata_once(self) -> None:
        """立即执行一轮 MCP 元数据刷新。"""
        await self._metadata_refresh_manager.refresh_once()

    async def ensure_connected(self, service_id: str) -> None:
        """确保与指定 MCP 服务的连接已建立。"""
        await self._service.ensure_connected(service_id)

    async def get_service_detail(self, service_id: str) -> McpServiceDetailResult:
        """从内存缓存获取服务能力和 instructions。"""
        definition = self._service.get_service(service_id)
        snapshot = self._metadata_registry.get_snapshot(service_id)
        if snapshot is None:
            return McpServiceDetailResult(
                supports_tools=False,
                supports_resources=False,
                server_name="",
                server_version="",
                instructions=definition.usage_guide,
            )
        return McpServiceDetailResult(
            supports_tools=snapshot.supports_tools,
            supports_resources=snapshot.supports_resources,
            server_name=snapshot.server_name,
            server_version=snapshot.server_version,
            instructions=snapshot.instructions if snapshot.instructions is not None else definition.usage_guide,
        )

    async def list_tools(self, service_id: str) -> list[McpToolSummaryDTO]:
        """从内存缓存列出指定服务的所有工具。"""
        self._service.get_service(service_id)
        tools = self._metadata_registry.get_tool_summaries(service_id)
        return [
            McpToolSummaryDTO(name=t.name, description=t.description) for t in tools
        ]

    async def get_tool_detail(
        self, service_id: str, tool_names: list[str]
    ) -> list[McpToolInfoDTO]:
        """从内存缓存批量获取工具的 inputSchema。"""
        self._service.get_service(service_id)
        tools = self._metadata_registry.get_tool_details(service_id, tool_names)
        return [
            McpToolInfoDTO(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
            )
            for t in tools
        ]

    async def call_tool(
        self, service_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> McpToolResultDTO:
        """执行 MCP 工具。"""
        result = await self._service.call_tool(service_id, tool_name, arguments)
        return McpToolResultDTO(
            content=result.content,
            content_blocks=result.content_blocks,
            structured_content=result.structured_content,
            is_error=result.is_error,
        )

    async def list_resources(self, service_id: str) -> list[McpResourceInfoDTO]:
        """从内存缓存列出指定服务的资源。"""
        self._service.get_service(service_id)
        resources = self._metadata_registry.get_resources(service_id)
        return [
            McpResourceInfoDTO(
                uri=r.uri,
                name=r.name,
                description=r.description,
                mime_type=r.mime_type,
            )
            for r in resources
        ]

    async def read_resource(self, service_id: str, uri: str) -> McpResourceContentDTO:
        """读取指定资源内容。"""
        content = await self._service.read_resource(service_id, uri)
        return McpResourceContentDTO(
            uri=content.uri,
            mime_type=content.mime_type,
            text=content.text,
            blob=content.blob,
        )

    # ── 生命周期 ──

    async def shutdown(self) -> None:
        """停止元数据刷新并断开所有 MCP 连接。"""
        await self.stop_metadata_refresh()
        await self._session_manager.disconnect_all()

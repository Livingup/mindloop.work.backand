# -*- coding: utf-8 -*-
"""MCP 客户端领域服务。

无状态，协调仓储和会话管理器完成业务用例。
分为两类用例：配置管理（同步）和协议操作（异步）。
"""

from __future__ import annotations

from typing import Any

from agent_runtime.mcp.mcp_client.mcp_service_definition import McpServiceDefinition
from agent_runtime.mcp.mcp_client.mcp_service_repository import McpServiceRepository
from agent_runtime.mcp.mcp_client.mcp_value_objects import (
    McpResourceContent,
    McpResourceInfo,
    McpServiceCapabilities,
    McpToolInfo,
    McpToolResult,
    McpToolSummary,
)
from agent_runtime.mcp.mcp_client.infrastructure.mcp_session_manager import (
    McpSessionManager,
)


class McpClientService:
    """MCP 客户端领域服务。

    协调 McpServiceRepository（配置持久化）和 McpSessionManager（连接管理）
    完成所有 MCP 客户端业务用例。
    """

    def __init__(
        self,
        repository: McpServiceRepository,
        session_manager: McpSessionManager,
    ) -> None:
        self._repository = repository
        self._session_manager = session_manager

    # ══════════════════════════════════════════
    # 配置管理用例
    # ══════════════════════════════════════════

    def list_services(self) -> list[McpServiceDefinition]:
        """列出所有已配置的 MCP 服务。"""
        return self._repository.find_all()

    def get_service(self, service_id: str) -> McpServiceDefinition:
        """获取指定服务的配置定义。

        Raises:
            KeyError: 服务不存在。
        """
        definition = self._repository.find_by_id(service_id)
        if definition is None:
            raise KeyError(f"MCP 服务 '{service_id}' 不存在")
        return definition

    def add_service(self, definition: McpServiceDefinition) -> None:
        """添加 MCP 服务配置。

        校验配置完整性后持久化。

        Raises:
            ValueError: 配置校验失败或 service_id 已存在。
        """
        definition.validate()

        if self._repository.contains(definition.service_id):
            raise ValueError(
                f"MCP 服务 '{definition.service_id}' 已存在，"
                "请使用其他 service_id 或先删除已有配置"
            )

        self._repository.save(definition)

    def update_service(self, service_id: str, **kwargs: Any) -> None:
        """更新 MCP 服务配置（部分更新）。

        仅更新传入的非 None 字段，其余保持不变。

        Raises:
            KeyError: 服务不存在。
            ValueError: 更新后的配置校验失败。
        """
        definition = self.get_service(service_id)

        for field_name, value in kwargs.items():
            if value is not None and hasattr(definition, field_name):
                setattr(definition, field_name, value)

        definition.validate()
        self._repository.save(definition)

    def enable_service(self, service_id: str) -> None:
        """启用 MCP 服务。

        Raises:
            KeyError: 服务不存在。
        """
        definition = self.get_service(service_id)
        definition.enabled = True
        self._repository.save(definition)

    def disable_service(self, service_id: str) -> None:
        """禁用 MCP 服务。

        Raises:
            KeyError: 服务不存在。
        """
        definition = self.get_service(service_id)
        definition.enabled = False
        self._repository.save(definition)

    async def remove_service(self, service_id: str) -> None:
        """删除 MCP 服务配置并断开连接。

        Raises:
            KeyError: 服务不存在。
        """
        if not self._repository.contains(service_id):
            raise KeyError(f"MCP 服务 '{service_id}' 不存在")

        # 先断开连接，再删除配置
        await self._session_manager.disconnect(service_id)
        self._repository.delete(service_id)

    # ══════════════════════════════════════════
    # 协议操作用例
    # ══════════════════════════════════════════

    async def ensure_connected(self, service_id: str) -> None:
        """确保与指定 MCP 服务的连接已建立。

        如果未连接，按配置建立连接并完成 initialize 握手。

        Raises:
            KeyError: 服务不存在。
            ConnectionError: 连接失败。
        """
        if self._session_manager.is_connected(service_id):
            return

        definition = self.get_service(service_id)
        if not definition.enabled:
            raise ValueError(f"MCP 服务 '{service_id}' 已禁用")

        await self._session_manager.connect(definition)

    async def test_connection(self, service_id: str) -> McpServiceCapabilities:
        """测试连接：临时建立 → initialize → 获取能力 → 断开。

        用于验证服务配置是否正确、服务是否可达。

        Returns:
            协商后的服务能力信息。

        Raises:
            KeyError: 服务不存在。
            ConnectionError: 连接或初始化失败。
        """
        definition = self.get_service(service_id)

        # 使用独立的 session manager 做临时连接
        temp_manager = McpSessionManager()
        try:
            capabilities = await temp_manager.connect(definition)
            return capabilities
        finally:
            await temp_manager.disconnect_all()

    async def get_service_detail(self, service_id: str) -> dict[str, Any]:
        """获取服务能力和 instructions。

        连接后返回 capabilities 和合并的 instructions。

        Returns:
            {
                "supports_tools": bool,
                "supports_resources": bool,
                "server_name": str,
                "server_version": str,
                "instructions": str | None,
            }
        """
        await self.ensure_connected(service_id)
        definition = self.get_service(service_id)
        capabilities = self._session_manager.get_capabilities(service_id)

        if capabilities is None:
            raise ConnectionError(f"MCP 服务 '{service_id}' 能力信息不可用")

        # 合并 instructions：MCP 服务自带 + 本地 usage_guide
        instructions = self._merge_instructions(
            capabilities.instructions, definition.usage_guide
        )

        return {
            "supports_tools": capabilities.supports_tools,
            "supports_resources": capabilities.supports_resources,
            "server_name": capabilities.server_name,
            "server_version": capabilities.server_version,
            "instructions": instructions,
        }

    async def list_tools(self, service_id: str) -> list[McpToolSummary]:
        """列出指定服务的所有工具（名称 + 描述）。"""
        await self.ensure_connected(service_id)
        return await self._session_manager.list_tools(service_id)

    async def get_tool_detail(
        self, service_id: str, tool_names: list[str]
    ) -> list[McpToolInfo]:
        """批量获取工具的 inputSchema。"""
        await self.ensure_connected(service_id)
        return await self._session_manager.get_tool_detail(service_id, tool_names)

    async def call_tool(
        self, service_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> McpToolResult:
        """执行 MCP 工具。"""
        await self.ensure_connected(service_id)
        return await self._session_manager.call_tool(service_id, tool_name, arguments)

    async def list_resources(self, service_id: str) -> list[McpResourceInfo]:
        """列出指定服务的资源。"""
        await self.ensure_connected(service_id)
        return await self._session_manager.list_resources(service_id)

    async def read_resource(self, service_id: str, uri: str) -> McpResourceContent:
        """读取指定资源内容。"""
        await self.ensure_connected(service_id)
        return await self._session_manager.read_resource(service_id, uri)

    # ── 内部方法 ──

    @staticmethod
    def _merge_instructions(
        server_instructions: str | None,
        local_usage_guide: str | None,
    ) -> str | None:
        """合并 MCP 服务自带的 instructions 和本地 usage_guide。

        两者都有时用换行拼接，服务自带的在前。
        """
        parts = []
        if server_instructions:
            parts.append(server_instructions.strip())
        if local_usage_guide:
            parts.append(local_usage_guide.strip())

        return "\n\n".join(parts) if parts else None

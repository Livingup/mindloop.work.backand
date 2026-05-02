# -*- coding: utf-8 -*-
"""MCP 客户端领域服务。

无状态，协调仓储和会话管理器完成业务用例。
分为两类用例：配置管理（同步）和协议操作（异步）。
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


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
        # mtime 快照：用于检测外部进程（CLI / 手动编辑）对配置文件的修改
        self._last_config_mtime: float = 0.0

    def _snapshot_mtime(self) -> None:
        """更新 mtime 快照为当前值（本进程写操作后调用，避免触发不必要的 reconcile）。"""
        self._last_config_mtime = self._repository.config_dir_mtime()

    def _is_config_changed(self) -> bool:
        """检查配置目录 mtime 是否发生变化（外部进程修改了 YAML 文件）。"""
        current_mtime = self._repository.config_dir_mtime()
        return current_mtime != self._last_config_mtime

    async def reconcile_if_config_changed(self) -> None:
        """若配置目录 mtime 变化，则 reconcile 连接池。

        懒检测：仅在被调用时检查 mtime，无额外线程。
        用于 REST 进程感知 CLI / 手动编辑导致的配置变更。
        """
        if self._is_config_changed():
            logger.info("检测到 MCP 配置目录变更（mtime 变化），执行 reconcile…")
            await self.reconcile_connections()
            self._snapshot_mtime()

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
        self._snapshot_mtime()

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
        self._snapshot_mtime()

    def enable_service(self, service_id: str) -> None:
        """启用 MCP 服务。

        Raises:
            KeyError: 服务不存在。
        """
        definition = self.get_service(service_id)
        definition.enabled = True
        self._repository.save(definition)
        self._snapshot_mtime()

    def disable_service(self, service_id: str) -> None:
        """禁用 MCP 服务。

        Raises:
            KeyError: 服务不存在。
        """
        definition = self.get_service(service_id)
        definition.enabled = False
        self._repository.save(definition)
        self._snapshot_mtime()

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
        self._snapshot_mtime()

    # ══════════════════════════════════════════
    # 启动连接
    # ══════════════════════════════════════════

    async def connect_all_enabled(self) -> dict[str, str | None]:
        """尝试连接所有已启用的 MCP 服务。

        逐个连接，单个服务连接失败只记录日志，不影响其他服务。
        用于应用启动时批量建立连接。

        Returns:
            {service_id: None} 表示连接成功，{service_id: "错误信息"} 表示连接失败。
        """
        results: dict[str, str | None] = {}
        definitions = self._repository.find_all()

        for definition in definitions:
            if not definition.enabled:
                continue
            try:
                await self._session_manager.connect(definition)
                results[definition.service_id] = None
                logger.info("启动连接 MCP 服务成功: %s", definition.service_id)
            except Exception as e:
                results[definition.service_id] = str(e)
                logger.warning(
                    "启动连接 MCP 服务失败: %s — %s", definition.service_id, e
                )

        self._snapshot_mtime()
        return results

    async def reconcile_connections(self) -> None:
        """根据当前配置状态对齐连接池。

        对比仓储中的服务定义与 McpSessionManager 中的活跃连接：
        - 已删除或已禁用的服务 → 断开连接
        - 已启用但未连接的服务 → 尝试连接（失败只记日志）

        用于配置文件被外部修改后（CLI / 手动编辑 YAML）自动恢复一致性。
        """
        definitions = {d.service_id: d for d in self._repository.find_all()}
        connected_ids = self._session_manager.connected_service_ids()

        # 断开已删除或已禁用的服务
        for sid in connected_ids:
            if sid not in definitions or not definitions[sid].enabled:
                logger.info("Reconcile: 断开不再需要的 MCP 服务连接: %s", sid)
                await self._session_manager.disconnect(sid)

        # 连接已启用但未连接的服务
        for sid, defn in definitions.items():
            if defn.enabled and not self._session_manager.is_connected(sid):
                try:
                    await self._session_manager.connect(defn)
                    logger.info("Reconcile: 已连接 MCP 服务: %s", sid)
                except Exception as e:
                    logger.warning("Reconcile: 连接 MCP 服务失败: %s — %s", sid, e)

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

# -*- coding: utf-8 -*-
"""MCP 客户端北向网关契约 — Protocol + DTO。

定义 mcp_client 子模块对外暴露的接口形状和数据模型。
外部调用方（mcp_meta_tools、CLI adapter、REST adapter）只依赖此契约。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# ══════════════════════════════════════════
# DTO — 北向网关数据传输对象
# ══════════════════════════════════════════


@dataclass(frozen=True)
class McpServiceSummary:
    """MCP 服务摘要 — 列表展示用。"""

    service_id: str
    name: str
    description: str | None
    transport_type: str
    """传输方式标识：'stdio' 或 'streamable-http'。"""
    enabled: bool
    connected: bool
    connection_status: str = "disconnected"
    last_error: str | None = None
    retry_attempt: int = 0
    next_retry_at: float | None = None
    group: str = "default"
    """服务分组标签。"""


@dataclass(frozen=True)
class McpServiceInfo:
    """MCP 服务配置详情 — get_service 返回。

    transport 是嵌套字典，结构如：
        stdio:            {"type": "stdio", "command": ..., "args": [...], "env": {...}}
        streamable-http:  {"type": "streamable-http", "url": ..., "headers": {...}}
    """

    service_id: str
    name: str
    description: str | None
    transport: dict
    """传输配置（嵌套字典，含 type 字段标识传输方式）。"""
    enabled: bool
    connected: bool
    connection_status: str = "disconnected"
    last_error: str | None = None
    retry_attempt: int = 0
    next_retry_at: float | None = None
    group: str = "default"
    """服务分组标签。"""
    usage_guide: str | None = None


@dataclass(frozen=True)
class McpServiceAddRequest:
    """添加 MCP 服务的请求参数。

    transport 是嵌套字典，结构同 McpServiceInfo.transport。
    """

    service_id: str
    name: str
    transport: dict
    """传输配置（嵌套字典，含 type 字段标识传输方式）。"""
    description: str | None = None
    enabled: bool = True
    group: str = "default"
    """服务分组标签。"""
    usage_guide: str | None = None


@dataclass(frozen=True)
class McpServiceUpdateRequest:
    """更新 MCP 服务配置的请求参数。

    所有字段可选，仅传入需要修改的字段。
    transport 若提供，为完整的传输配置嵌套字典（整体替换）。
    """

    name: str | None = None
    description: str | None = None
    transport: dict | None = None
    """传输配置（整体替换）。"""
    group: str | None = None
    """服务分组标签。"""
    usage_guide: str | None = None


@dataclass(frozen=True)
class McpServiceDetailResult:
    """服务能力详情 — get_service_detail 返回。"""

    supports_tools: bool
    supports_resources: bool
    server_name: str
    server_version: str
    instructions: str | None


@dataclass(frozen=True)
class McpTestResult:
    """连接测试结果。"""

    success: bool
    server_name: str = ""
    server_version: str = ""
    supports_tools: bool = False
    supports_resources: bool = False
    error: str | None = None


@dataclass(frozen=True)
class McpToolSummaryDTO:
    """工具摘要 DTO。"""

    name: str
    description: str | None


@dataclass(frozen=True)
class McpToolInfoDTO:
    """工具详情 DTO — 含 inputSchema。"""

    name: str
    description: str | None
    input_schema: dict = field(default_factory=dict)


@dataclass(frozen=True)
class McpToolResultDTO:
    """工具执行结果 DTO。"""

    content: str
    content_blocks: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    structured_content: dict | None = None
    is_error: bool = False


@dataclass(frozen=True)
class McpResourceInfoDTO:
    """资源信息 DTO。"""

    uri: str
    name: str
    description: str | None = None
    mime_type: str | None = None


@dataclass(frozen=True)
class McpResourceContentDTO:
    """资源内容 DTO。"""

    uri: str
    mime_type: str | None = None
    text: str | None = None
    blob: str | None = None


# ══════════════════════════════════════════
# Gateway Protocol — 北向网关契约
# ══════════════════════════════════════════


class McpClientGateway(Protocol):
    """MCP 客户端模块北向网关契约。

    所有外部调用方（mcp_meta_tools、CLI adapter、REST adapter）
    通过此 Protocol 使用 MCP 客户端能力。
    """

    # ── 配置管理 ──

    def list_services(self) -> list[McpServiceSummary]:
        """列出所有已配置的 MCP 服务。"""
        ...

    def get_service(self, service_id: str) -> McpServiceInfo:
        """获取服务配置详情。"""
        ...

    def add_service(self, config: McpServiceAddRequest) -> None:
        """添加 MCP 服务配置。"""
        ...

    def update_service(self, service_id: str, update: McpServiceUpdateRequest) -> None:
        """更新 MCP 服务配置（部分更新）。"""
        ...

    async def remove_service(self, service_id: str) -> None:
        """删除 MCP 服务配置并断开连接。"""
        ...

    def enable_service(self, service_id: str) -> None:
        """启用 MCP 服务。"""
        ...

    def disable_service(self, service_id: str) -> None:
        """禁用 MCP 服务。"""
        ...

    async def test_connection(self, service_id: str) -> McpTestResult:
        """测试连接：临时建立 → initialize → 获取能力 → 断开。"""
        ...

    async def connect_all_enabled(self) -> dict[str, str | None]:
        """调度所有已启用 MCP 服务后台连接。"""
        ...

    async def connect_service(self, service_id: str) -> None:
        """手动重连指定 MCP 服务。"""
        ...

    async def reconcile_connections(self) -> None:
        """根据当前配置状态对齐连接运行态。"""
        ...

    async def reconcile_if_config_changed(self) -> None:
        """若配置目录 mtime 变化，则 reconcile 连接运行态。"""
        ...

    # ── 协议操作 ──

    async def ensure_connected(self, service_id: str) -> None:
        """确保与指定 MCP 服务的连接已建立。"""
        ...

    async def get_service_detail(self, service_id: str) -> McpServiceDetailResult:
        """获取服务能力和 instructions。"""
        ...

    async def list_tools(self, service_id: str) -> list[McpToolSummaryDTO]:
        """列出指定服务的所有工具（名称 + 描述）。"""
        ...

    async def get_tool_detail(
        self, service_id: str, tool_names: list[str]
    ) -> list[McpToolInfoDTO]:
        """批量获取工具的 inputSchema。"""
        ...

    async def call_tool(
        self, service_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> McpToolResultDTO:
        """执行 MCP 工具。"""
        ...

    async def list_resources(self, service_id: str) -> list[McpResourceInfoDTO]:
        """列出指定服务的资源。"""
        ...

    async def read_resource(self, service_id: str, uri: str) -> McpResourceContentDTO:
        """读取指定资源内容。"""
        ...

    # ── 生命周期 ──

    async def shutdown(self) -> None:
        """断开所有 MCP 连接（应用关闭或 CLI 命令结束时调用）。"""
        ...

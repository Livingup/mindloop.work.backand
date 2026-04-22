# -*- coding: utf-8 -*-
"""MCP 会话连接池管理器。

管理与多个 MCP 服务的 ClientSession 连接，支持按需建立、复用和优雅关闭。
内部使用 MCP Python SDK 的 stdio_client 和 streamable_http_client 传输层。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from agent_runtime.mcp.mcp_client.mcp_service_definition import McpServiceDefinition
from agent_runtime.mcp.mcp_client.mcp_value_objects import (
    McpResourceContent,
    McpResourceInfo,
    McpServiceCapabilities,
    McpToolInfo,
    McpToolResult,
    McpToolSummary,
)

logger = logging.getLogger(__name__)


class _ManagedConnection:
    """一个受管理的 MCP 服务连接。

    封装 AsyncExitStack + ClientSession + 缓存的能力信息。
    """

    def __init__(
        self,
        session: ClientSession,
        exit_stack: AsyncExitStack,
        capabilities: McpServiceCapabilities,
    ) -> None:
        self.session = session
        self.exit_stack = exit_stack
        self.capabilities = capabilities


class McpSessionManager:
    """MCP 会话连接池管理器。

    维护 service_id → _ManagedConnection 的映射，
    提供连接建立、协议操作和优雅关闭的统一入口。
    """

    def __init__(self) -> None:
        self._connections: dict[str, _ManagedConnection] = {}
        self._lock = asyncio.Lock()

    # ── 连接管理 ──

    async def connect(self, definition: McpServiceDefinition) -> McpServiceCapabilities:
        """按配置建立连接并完成 initialize 握手。

        如果已有连接则直接返回缓存的能力信息。

        Args:
            definition: MCP 服务配置定义。

        Returns:
            协商后的服务能力信息。

        Raises:
            ConnectionError: 连接或初始化失败。
        """
        async with self._lock:
            if definition.service_id in self._connections:
                return self._connections[definition.service_id].capabilities

            try:
                exit_stack = AsyncExitStack()
                await exit_stack.__aenter__()

                session = await self._create_session(definition, exit_stack)
                init_result = await session.initialize()

                capabilities = self._extract_capabilities(init_result)

                self._connections[definition.service_id] = _ManagedConnection(
                    session=session,
                    exit_stack=exit_stack,
                    capabilities=capabilities,
                )

                logger.info(
                    "已连接 MCP 服务: %s (%s v%s)",
                    definition.service_id,
                    capabilities.server_name,
                    capabilities.server_version,
                )
                return capabilities

            except Exception as e:
                # 连接失败时清理 exit_stack
                try:
                    await exit_stack.__aexit__(None, None, None)
                except Exception:
                    pass
                raise ConnectionError(
                    f"连接 MCP 服务 '{definition.service_id}' 失败: {e}"
                ) from e

    async def disconnect(self, service_id: str) -> None:
        """断开指定服务的连接。"""
        async with self._lock:
            conn = self._connections.pop(service_id, None)
            if conn is not None:
                try:
                    await conn.exit_stack.__aexit__(None, None, None)
                except Exception as e:
                    logger.warning("断开 MCP 服务 '%s' 时出错: %s", service_id, e)
                logger.info("已断开 MCP 服务: %s", service_id)

    async def disconnect_all(self) -> None:
        """断开所有连接（应用关闭时调用）。"""
        service_ids = list(self._connections.keys())
        for service_id in service_ids:
            await self.disconnect(service_id)

    def is_connected(self, service_id: str) -> bool:
        """是否已连接。"""
        return service_id in self._connections

    def get_capabilities(self, service_id: str) -> McpServiceCapabilities | None:
        """获取已连接服务的缓存能力信息。"""
        conn = self._connections.get(service_id)
        return conn.capabilities if conn else None

    # ── 协议操作 ──

    async def list_tools(self, service_id: str) -> list[McpToolSummary]:
        """列出指定服务的所有工具（名称 + 描述）。

        自动处理分页（nextCursor）。
        """
        session = self._get_session(service_id)
        all_tools: list[McpToolSummary] = []
        cursor: str | None = None

        while True:
            result = await session.list_tools(cursor=cursor)
            for tool in result.tools:
                all_tools.append(
                    McpToolSummary(name=tool.name, description=tool.description)
                )
            if result.nextCursor:
                cursor = result.nextCursor
            else:
                break

        return all_tools

    async def get_tool_detail(
        self, service_id: str, tool_names: list[str]
    ) -> list[McpToolInfo]:
        """批量获取指定工具的完整信息（含 inputSchema）。

        从 list_tools 结果中筛选匹配的工具。
        """
        session = self._get_session(service_id)
        all_tools: list[types.Tool] = []
        cursor: str | None = None

        while True:
            result = await session.list_tools(cursor=cursor)
            all_tools.extend(result.tools)
            if result.nextCursor:
                cursor = result.nextCursor
            else:
                break

        name_set = set(tool_names)
        return [
            McpToolInfo(
                name=tool.name,
                description=tool.description,
                input_schema=tool.inputSchema,
            )
            for tool in all_tools
            if tool.name in name_set
        ]

    async def call_tool(
        self, service_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> McpToolResult:
        """执行 MCP 工具。

        解析 CallToolResult，提取文本内容和结构化内容。
        """
        session = self._get_session(service_id)
        result = await session.call_tool(tool_name, arguments)

        # 拼接所有 TextContent
        text_parts: list[str] = []
        for content in result.content:
            if isinstance(content, types.TextContent):
                text_parts.append(content.text)

        return McpToolResult(
            content="\n".join(text_parts),
            structured_content=result.structuredContent,
            is_error=result.isError,
        )

    async def list_resources(self, service_id: str) -> list[McpResourceInfo]:
        """列出指定服务的所有资源。

        自动处理分页（nextCursor）。
        """
        session = self._get_session(service_id)
        all_resources: list[McpResourceInfo] = []
        cursor: str | None = None

        while True:
            result = await session.list_resources(cursor=cursor)
            for res in result.resources:
                all_resources.append(
                    McpResourceInfo(
                        uri=str(res.uri),
                        name=res.name,
                        description=res.description,
                        mime_type=res.mimeType,
                    )
                )
            if result.nextCursor:
                cursor = result.nextCursor
            else:
                break

        return all_resources

    async def read_resource(self, service_id: str, uri: str) -> McpResourceContent:
        """读取指定资源内容。"""
        from pydantic import AnyUrl

        session = self._get_session(service_id)
        result = await session.read_resource(AnyUrl(uri))

        # 取第一个内容项
        if not result.contents:
            return McpResourceContent(uri=uri)

        first = result.contents[0]

        # TextResourceContents 有 text 属性，BlobResourceContents 有 blob 属性
        text = getattr(first, "text", None)
        blob = getattr(first, "blob", None)

        return McpResourceContent(
            uri=str(first.uri),
            mime_type=first.mimeType,
            text=text,
            blob=blob,
        )

    # ── 内部方法 ──

    def _get_session(self, service_id: str) -> ClientSession:
        """获取已连接的 ClientSession。

        Raises:
            ConnectionError: 服务未连接。
        """
        conn = self._connections.get(service_id)
        if conn is None:
            raise ConnectionError(f"MCP 服务 '{service_id}' 未连接")
        return conn.session

    async def _create_session(
        self, definition: McpServiceDefinition, exit_stack: AsyncExitStack
    ) -> ClientSession:
        """根据传输方式创建 ClientSession。

        使用 exit_stack 管理传输层和 session 的生命周期。
        """
        from agent_runtime.mcp.mcp_client.mcp_transport_config import (
            StdioTransportConfig,
            StreamableHttpTransportConfig,
        )

        tc = definition.transport

        if isinstance(tc, StdioTransportConfig):
            params = StdioServerParameters(
                command=tc.command,
                args=tc.args,
                env=tc.env if tc.env else None,
            )
            read_stream, write_stream = await exit_stack.enter_async_context(
                stdio_client(params)
            )
        elif isinstance(tc, StreamableHttpTransportConfig):
            # streamable_http_client 不直接接受 headers，需通过 httpx.AsyncClient 传入
            http_client = None
            if tc.headers:
                import httpx
                http_client = httpx.AsyncClient(headers=tc.headers)
            read_stream, write_stream, _ = await exit_stack.enter_async_context(
                streamable_http_client(
                    tc.url,
                    http_client=http_client,
                )
            )
        else:
            raise ValueError(f"不支持的传输配置类型: {type(tc).__name__}")

        session = await exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        return session

    @staticmethod
    def _extract_capabilities(init_result: types.InitializeResult) -> McpServiceCapabilities:
        """从 InitializeResult 提取能力信息。"""
        caps = init_result.capabilities
        info = init_result.serverInfo

        return McpServiceCapabilities(
            supports_tools=caps.tools is not None,
            supports_resources=caps.resources is not None,
            server_name=info.name if info else "",
            server_version=info.version if info else "",
            instructions=init_result.instructions,
        )

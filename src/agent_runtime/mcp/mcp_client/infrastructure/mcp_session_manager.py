# -*- coding: utf-8 -*-
"""MCP 会话连接池管理器。

管理与多个 MCP 服务的 ClientSession 连接，支持按需建立、复用和优雅关闭。
内部使用 MCP Python SDK 的 stdio_client 和 streamable_http_client 传输层。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from agent_runtime.mcp.mcp_client.mcp_service_definition import McpServiceDefinition
from agent_runtime.mcp.mcp_client.mcp_value_objects import (
    MCP_CONNECTION_STATUS_CONNECTED,
    MCP_CONNECTION_STATUS_CONNECTING,
    MCP_CONNECTION_STATUS_DISABLED,
    MCP_CONNECTION_STATUS_DISCONNECTED,
    MCP_CONNECTION_STATUS_FAILED,
    MCP_CONNECTION_STATUS_RECONNECTING,
    McpConnectionStatus,
    McpResourceContent,
    McpResourceInfo,
    McpServiceCapabilities,
    McpToolInfo,
    McpToolResult,
    McpToolSummary,
)

logger = logging.getLogger(__name__)

_MCP_CONNECT_TIMEOUT_SECONDS = 10.0
_MCP_RECONNECT_BASE_DELAY_SECONDS = 2.0
_MCP_RECONNECT_MAX_DELAY_SECONDS = 60.0


def _dump_mcp_content_block(content: Any) -> dict[str, Any]:
    if hasattr(content, "model_dump"):
        return content.model_dump(mode="json", by_alias=True)
    if isinstance(content, dict):
        return content
    return {"type": "text", "text": str(content)}


class _ManagedConnection:
    """一个受管理的 MCP 服务连接。

    封装 AsyncExitStack + ClientSession + 缓存的能力信息。
    """

    def __init__(
        self,
        session: ClientSession,
        capabilities: McpServiceCapabilities,
        close_event: asyncio.Event,
        owner_task: asyncio.Task,
    ) -> None:
        self.session = session
        self.capabilities = capabilities
        self.close_event = close_event
        self.owner_task = owner_task


@dataclass
class _ConnectionRuntimeState:
    status: str = MCP_CONNECTION_STATUS_DISCONNECTED
    last_error: str | None = None
    retry_attempt: int = 0
    next_retry_at: float | None = None
    last_connected_at: float | None = None
    last_disconnected_at: float | None = None
    reconnect_task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class McpSessionManager:
    """MCP 会话连接池管理器。

    维护 service_id → _ManagedConnection 的映射，
    提供连接建立、协议操作和优雅关闭的统一入口。
    """

    def __init__(self) -> None:
        self._connections: dict[str, _ManagedConnection] = {}
        self._runtime_states: dict[str, _ConnectionRuntimeState] = {}

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
        return await self._connect(definition, status=MCP_CONNECTION_STATUS_CONNECTING)

    async def disconnect(self, service_id: str) -> None:
        """断开指定服务的连接。"""
        await self.cancel_reconnect(service_id)
        state = self._state(service_id)
        async with state.lock:
            await self._close_connection(service_id)
            state.status = MCP_CONNECTION_STATUS_DISCONNECTED
            state.last_error = None
            state.next_retry_at = None
            state.last_disconnected_at = time.time()

    async def disconnect_all(self) -> None:
        """断开所有连接（应用关闭时调用）。"""
        service_ids = set(self._connections.keys()) | set(self._runtime_states.keys())
        for service_id in service_ids:
            await self.disconnect(service_id)

    async def mark_disabled(self, service_id: str) -> None:
        """标记服务禁用并停止重连。"""
        await self.cancel_reconnect(service_id)
        state = self._state(service_id)
        async with state.lock:
            await self._close_connection(service_id)
            state.status = MCP_CONNECTION_STATUS_DISABLED
            state.last_error = None
            state.next_retry_at = None
            state.last_disconnected_at = time.time()

    async def mark_connection_lost(self, service_id: str, error: str) -> None:
        """标记已连接服务异常断连。"""
        state = self._state(service_id)
        async with state.lock:
            await self._close_connection(service_id)
            self._record_failure(state, error)

    def schedule_reconnect(self, definition: McpServiceDefinition) -> None:
        """为服务调度后台指数退避重连。"""
        state = self._state(definition.service_id)
        if not definition.enabled:
            state.status = MCP_CONNECTION_STATUS_DISABLED
            return
        if self.is_connected(definition.service_id):
            return
        if state.reconnect_task is not None and not state.reconnect_task.done():
            return
        state.reconnect_task = asyncio.create_task(self._reconnect_loop(definition))

    async def reconnect_now(self, definition: McpServiceDefinition) -> McpServiceCapabilities:
        """立即执行一次手动重连。"""
        await self.disconnect(definition.service_id)
        state = self._state(definition.service_id)
        state.retry_attempt = 0
        state.next_retry_at = None
        return await self._connect(definition, status=MCP_CONNECTION_STATUS_CONNECTING)

    async def cancel_reconnect(self, service_id: str) -> None:
        """取消服务后台重连任务。"""
        state = self._state(service_id)
        task = state.reconnect_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(Exception, asyncio.CancelledError):
                await task
        state.reconnect_task = None
        state.next_retry_at = None

    def is_connected(self, service_id: str) -> bool:
        """是否已连接。"""
        return service_id in self._connections

    def connected_service_ids(self) -> list[str]:
        """返回当前所有已连接的 service_id 列表。"""
        return list(self._connections.keys())

    def known_service_ids(self) -> list[str]:
        return list(set(self._connections.keys()) | set(self._runtime_states.keys()))

    def get_capabilities(self, service_id: str) -> McpServiceCapabilities | None:
        """获取已连接服务的缓存能力信息。"""
        conn = self._connections.get(service_id)
        return conn.capabilities if conn else None

    def get_connection_status(self, service_id: str) -> McpConnectionStatus:
        """获取 MCP 服务连接运行态。"""
        state = self._state(service_id)
        return McpConnectionStatus(
            service_id=service_id,
            status=state.status,
            connected=self.is_connected(service_id),
            last_error=state.last_error,
            retry_attempt=state.retry_attempt,
            next_retry_at=state.next_retry_at,
            last_connected_at=state.last_connected_at,
            last_disconnected_at=state.last_disconnected_at,
        )

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
        content_blocks: list[dict[str, Any]] = []
        for content in result.content:
            content_blocks.append(_dump_mcp_content_block(content))
            if isinstance(content, types.TextContent):
                text_parts.append(content.text)

        return McpToolResult(
            content="\n".join(text_parts),
            content_blocks=tuple(content_blocks),
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

    def _state(self, service_id: str) -> _ConnectionRuntimeState:
        return self._runtime_states.setdefault(service_id, _ConnectionRuntimeState())

    async def _connect(
        self,
        definition: McpServiceDefinition,
        *,
        status: str,
    ) -> McpServiceCapabilities:
        service_id = definition.service_id
        state = self._state(service_id)
        async with state.lock:
            if service_id in self._connections:
                state.status = MCP_CONNECTION_STATUS_CONNECTED
                state.last_error = None
                state.next_retry_at = None
                return self._connections[service_id].capabilities

            state.status = status
            state.last_error = None
            state.next_retry_at = None
            close_event = asyncio.Event()
            ready_future: asyncio.Future[tuple[ClientSession, McpServiceCapabilities]] = (
                asyncio.get_running_loop().create_future()
            )
            owner_task = asyncio.create_task(
                self._connection_owner(definition, close_event, ready_future)
            )

            try:
                session, capabilities = await asyncio.wait_for(
                    asyncio.shield(ready_future),
                    timeout=_MCP_CONNECT_TIMEOUT_SECONDS,
                )
                self._connections[service_id] = _ManagedConnection(
                    session=session,
                    capabilities=capabilities,
                    close_event=close_event,
                    owner_task=owner_task,
                )
                state.status = MCP_CONNECTION_STATUS_CONNECTED
                state.last_error = None
                state.retry_attempt = 0
                state.next_retry_at = None
                state.last_connected_at = time.time()
                logger.info(
                    "已连接 MCP 服务: %s (%s v%s)",
                    service_id,
                    capabilities.server_name,
                    capabilities.server_version,
                )
                return capabilities
            except TimeoutError as e:
                ready_future.cancel()
                await self._stop_owner_task(owner_task, close_event)
                error = f"连接 MCP 服务 '{service_id}' 超时"
                self._record_failure(state, error)
                raise ConnectionError(error) from e
            except asyncio.CancelledError as e:
                ready_future.cancel()
                await self._stop_owner_task(owner_task, close_event)
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
                error = f"连接 MCP 服务 '{service_id}' 失败: {e}"
                self._record_failure(state, error)
                raise ConnectionError(error) from e
            except Exception as e:
                await self._stop_owner_task(owner_task, close_event)
                error = f"连接 MCP 服务 '{service_id}' 失败: {e}"
                self._record_failure(state, error)
                raise ConnectionError(error) from e

    async def _close_connection(self, service_id: str) -> None:
        conn = self._connections.pop(service_id, None)
        if conn is None:
            return
        await self._stop_owner_task(conn.owner_task, conn.close_event)
        logger.info("已断开 MCP 服务: %s", service_id)

    async def _connection_owner(
        self,
        definition: McpServiceDefinition,
        close_event: asyncio.Event,
        ready_future: asyncio.Future[tuple[ClientSession, McpServiceCapabilities]],
    ) -> None:
        service_id = definition.service_id
        exit_stack = AsyncExitStack()
        try:
            await exit_stack.__aenter__()
            session = await self._create_session(definition, exit_stack)
            init_result = await session.initialize()
            capabilities = self._extract_capabilities(init_result)
            if not ready_future.done():
                ready_future.set_result((session, capabilities))
            await close_event.wait()
        except asyncio.CancelledError as e:
            if not ready_future.done():
                ready_future.set_exception(ConnectionError(f"连接 MCP 服务 '{service_id}' 初始化被取消"))
            raise
        except Exception as e:
            if not ready_future.done():
                ready_future.set_exception(e)
            else:
                logger.warning("MCP 服务连接 owner 异常退出: %s — %s", service_id, e)
        finally:
            await self._close_exit_stack(exit_stack)
            current_task = asyncio.current_task()
            conn = self._connections.get(service_id)
            if conn is not None and conn.owner_task is current_task:
                self._connections.pop(service_id, None)

    async def _stop_owner_task(
        self,
        owner_task: asyncio.Task,
        close_event: asyncio.Event,
    ) -> None:
        close_event.set()
        if owner_task is asyncio.current_task():
            return
        try:
            await asyncio.wait_for(asyncio.shield(owner_task), timeout=5.0)
        except TimeoutError:
            owner_task.cancel()
            with suppress(Exception, asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(asyncio.shield(owner_task), timeout=5.0)
        except (Exception, asyncio.CancelledError):
            pass

    async def _close_exit_stack(self, exit_stack: AsyncExitStack) -> None:
        try:
            await exit_stack.__aexit__(None, None, None)
        except (Exception, asyncio.CancelledError) as e:
            logger.debug("关闭 MCP 连接资源时出错: %s", e)

    def _record_failure(self, state: _ConnectionRuntimeState, error: str) -> None:
        state.status = MCP_CONNECTION_STATUS_FAILED
        state.last_error = error
        state.retry_attempt += 1
        state.last_disconnected_at = time.time()
        state.next_retry_at = state.last_disconnected_at + self._retry_delay(state.retry_attempt)

    def _retry_delay(self, retry_attempt: int) -> float:
        exponent = max(retry_attempt - 1, 0)
        return min(
            _MCP_RECONNECT_BASE_DELAY_SECONDS * (2 ** exponent),
            _MCP_RECONNECT_MAX_DELAY_SECONDS,
        )

    async def _reconnect_loop(self, definition: McpServiceDefinition) -> None:
        service_id = definition.service_id
        state = self._state(service_id)
        try:
            while definition.enabled and not self.is_connected(service_id):
                if state.retry_attempt > 0:
                    delay = self._retry_delay(state.retry_attempt)
                    state.next_retry_at = time.time() + delay
                    await asyncio.sleep(delay)
                try:
                    await self._connect(
                        definition,
                        status=MCP_CONNECTION_STATUS_RECONNECTING,
                    )
                    return
                except Exception as e:
                    logger.warning("后台重连 MCP 服务失败: %s — %s", service_id, e)
        finally:
            if state.reconnect_task is asyncio.current_task():
                state.reconnect_task = None

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

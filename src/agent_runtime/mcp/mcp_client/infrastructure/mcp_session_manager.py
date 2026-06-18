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

from agent_runtime.infrastructure.runtime_task_registry import get_runtime_task_registry
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
_MCP_DIAGNOSTIC_SLOW_PHASE_SECONDS = 1.0
_MCP_STOP_OWNER_TASK_TIMEOUT_SECONDS = 5.0


def _duration_ms_since(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _current_task_name() -> str | None:
    task = asyncio.current_task()
    return task.get_name() if task is not None else None


def _slow_or_debug(duration_ms: int) -> int:
    return logging.WARNING if duration_ms >= int(_MCP_DIAGNOSTIC_SLOW_PHASE_SECONDS * 1000) else logging.DEBUG


def _transport_type_name(definition: McpServiceDefinition) -> str:
    return type(definition.transport).__name__


def _log_mcp_phase(
    level: int,
    message: str,
    *,
    service_id: str,
    operation: str,
    phase: str,
    task_name: str | None,
    duration_ms: int | None = None,
    timeout_seconds: float | None = None,
    status: str | None = None,
    transport_type: str | None = None,
    owner_task_name: str | None = None,
    error_type: str | None = None,
    error: str | None = None,
) -> None:
    if not logger.isEnabledFor(level):
        return
    logger.log(
        level,
        "%s: trace_id=%s stream_id=%s event_type=%s service_id=%s operation=%s phase=%s task_name=%s duration_ms=%s timeout_seconds=%s "
        "status=%s transport_type=%s owner_task_name=%s error_type=%s error=%s",
        message,
        None,
        None,
        None,
        service_id,
        operation,
        phase,
        task_name,
        duration_ms,
        timeout_seconds,
        status,
        transport_type,
        owner_task_name,
        error_type,
        error,
    )


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
        state.reconnect_task = get_runtime_task_registry().create_task(
            self._reconnect_loop(definition),
            task_name=f"mcp-reconnect-loop-{definition.service_id}",
            owner_type="mcp_session_manager",
            owner_id=definition.service_id,
            task_class="reconnect_loop",
            diagnostic_context={
                "service_id": definition.service_id,
                "transport_type": _transport_type_name(definition),
                "retry_attempt": state.retry_attempt,
            },
        )

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
            get_runtime_task_registry().cancel(
                task,
                message="mcp reconnect cancelled",
                diagnostic_context={"service_id": service_id, "phase": "cancel_reconnect"},
            )
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
        operation = "mcp.connect"
        task_name = _current_task_name()
        transport_type = _transport_type_name(definition)
        connect_started_at = time.perf_counter()
        state = self._state(service_id)
        lock_wait_started_at = time.perf_counter()
        try:
            await state.lock.acquire()
        except asyncio.CancelledError:
            _log_mcp_phase(
                logging.WARNING,
                "MCP connect state lock wait cancelled",
                service_id=service_id,
                operation=operation,
                phase="state.lock.cancelled",
                task_name=task_name,
                duration_ms=_duration_ms_since(lock_wait_started_at),
                status=status,
                transport_type=transport_type,
                error_type="CancelledError",
            )
            raise
        lock_wait_ms = _duration_ms_since(lock_wait_started_at)
        _log_mcp_phase(
            _slow_or_debug(lock_wait_ms),
            "MCP connect state lock acquired",
            service_id=service_id,
            operation=operation,
            phase="state.lock.acquired",
            task_name=task_name,
            duration_ms=lock_wait_ms,
            status=status,
            transport_type=transport_type,
        )
        try:
            if service_id in self._connections:
                state.status = MCP_CONNECTION_STATUS_CONNECTED
                state.last_error = None
                state.next_retry_at = None
                _log_mcp_phase(
                    logging.DEBUG,
                    "MCP connect reused existing connection",
                    service_id=service_id,
                    operation=operation,
                    phase="connection.reused",
                    task_name=task_name,
                    duration_ms=_duration_ms_since(connect_started_at),
                    status=state.status,
                    transport_type=transport_type,
                )
                return self._connections[service_id].capabilities

            state.status = status
            state.last_error = None
            state.next_retry_at = None
            close_event = asyncio.Event()
            ready_future: asyncio.Future[tuple[ClientSession, McpServiceCapabilities]] = (
                asyncio.get_running_loop().create_future()
            )
            owner_task_created_at = time.perf_counter()
            owner_task = get_runtime_task_registry().create_task(
                self._connection_owner(
                    definition,
                    close_event,
                    ready_future,
                    owner_task_created_at=owner_task_created_at,
                ),
                task_name=f"mcp-connection-owner-{service_id}",
                owner_type="mcp_session_manager",
                owner_id=service_id,
                task_class="connection_owner",
                diagnostic_context={
                    "service_id": service_id,
                    "transport_type": transport_type,
                    "status": state.status,
                },
            )
            _log_mcp_phase(
                logging.DEBUG,
                "MCP connect owner task created",
                service_id=service_id,
                operation=operation,
                phase="owner_task.created",
                task_name=task_name,
                duration_ms=0,
                timeout_seconds=_MCP_CONNECT_TIMEOUT_SECONDS,
                status=state.status,
                transport_type=transport_type,
                owner_task_name=owner_task.get_name(),
            )

            try:
                ready_wait_started_at = time.perf_counter()
                session, capabilities = await asyncio.wait_for(
                    asyncio.shield(ready_future),
                    timeout=_MCP_CONNECT_TIMEOUT_SECONDS,
                )
                ready_wait_ms = _duration_ms_since(ready_wait_started_at)
                _log_mcp_phase(
                    _slow_or_debug(ready_wait_ms),
                    "MCP connect ready future completed",
                    service_id=service_id,
                    operation=operation,
                    phase="ready_future.completed",
                    task_name=task_name,
                    duration_ms=ready_wait_ms,
                    timeout_seconds=_MCP_CONNECT_TIMEOUT_SECONDS,
                    status=state.status,
                    transport_type=transport_type,
                    owner_task_name=owner_task.get_name(),
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
                _log_mcp_phase(
                    logging.INFO,
                    "MCP service connected",
                    service_id=service_id,
                    operation=operation,
                    phase="connection.connected",
                    task_name=task_name,
                    duration_ms=_duration_ms_since(connect_started_at),
                    timeout_seconds=_MCP_CONNECT_TIMEOUT_SECONDS,
                    status=state.status,
                    transport_type=transport_type,
                    owner_task_name=owner_task.get_name(),
                )
                logger.info(
                    "已连接 MCP 服务: %s (%s v%s)",
                    service_id,
                    capabilities.server_name,
                    capabilities.server_version,
                )
                return capabilities
            except TimeoutError as e:
                ready_wait_ms = _duration_ms_since(ready_wait_started_at)
                _log_mcp_phase(
                    logging.ERROR,
                    "MCP connect ready future timed out",
                    service_id=service_id,
                    operation=operation,
                    phase="ready_future.timeout",
                    task_name=task_name,
                    duration_ms=ready_wait_ms,
                    timeout_seconds=_MCP_CONNECT_TIMEOUT_SECONDS,
                    status=state.status,
                    transport_type=transport_type,
                    owner_task_name=owner_task.get_name(),
                    error_type="TimeoutError",
                    error=f"ready_future did not complete within {_MCP_CONNECT_TIMEOUT_SECONDS}s",
                )
                ready_future.cancel()
                cleanup_started_at = time.perf_counter()
                await self._stop_owner_task(
                    owner_task,
                    close_event,
                    service_id=service_id,
                    operation="mcp.connect.timeout_cleanup",
                )
                cleanup_ms = _duration_ms_since(cleanup_started_at)
                _log_mcp_phase(
                    _slow_or_debug(cleanup_ms),
                    "MCP connect timeout cleanup completed",
                    service_id=service_id,
                    operation=operation,
                    phase="timeout_cleanup.completed",
                    task_name=task_name,
                    duration_ms=cleanup_ms,
                    timeout_seconds=_MCP_CONNECT_TIMEOUT_SECONDS,
                    status=state.status,
                    transport_type=transport_type,
                    owner_task_name=owner_task.get_name(),
                )
                error = f"连接 MCP 服务 '{service_id}' 超时"
                self._record_failure(state, error)
                raise ConnectionError(error) from e
            except asyncio.CancelledError as e:
                _log_mcp_phase(
                    logging.WARNING,
                    "MCP connect cancelled while waiting for readiness",
                    service_id=service_id,
                    operation=operation,
                    phase="ready_future.cancelled",
                    task_name=task_name,
                    duration_ms=_duration_ms_since(connect_started_at),
                    timeout_seconds=_MCP_CONNECT_TIMEOUT_SECONDS,
                    status=state.status,
                    transport_type=transport_type,
                    owner_task_name=owner_task.get_name(),
                    error_type="CancelledError",
                )
                ready_future.cancel()
                await self._stop_owner_task(
                    owner_task,
                    close_event,
                    service_id=service_id,
                    operation="mcp.connect.cancel_cleanup",
                )
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
                error = f"连接 MCP 服务 '{service_id}' 失败: {e}"
                self._record_failure(state, error)
                raise ConnectionError(error) from e
            except Exception as e:
                _log_mcp_phase(
                    logging.ERROR,
                    "MCP connect failed before readiness",
                    service_id=service_id,
                    operation=operation,
                    phase="ready_future.exception",
                    task_name=task_name,
                    duration_ms=_duration_ms_since(connect_started_at),
                    timeout_seconds=_MCP_CONNECT_TIMEOUT_SECONDS,
                    status=state.status,
                    transport_type=transport_type,
                    owner_task_name=owner_task.get_name(),
                    error_type=e.__class__.__name__,
                    error=str(e),
                )
                await self._stop_owner_task(
                    owner_task,
                    close_event,
                    service_id=service_id,
                    operation="mcp.connect.exception_cleanup",
                )
                error = f"连接 MCP 服务 '{service_id}' 失败: {e}"
                self._record_failure(state, error)
                raise ConnectionError(error) from e
        finally:
            state.lock.release()

    async def _close_connection(self, service_id: str) -> None:
        conn = self._connections.pop(service_id, None)
        if conn is None:
            return
        await self._stop_owner_task(
            conn.owner_task,
            conn.close_event,
            service_id=service_id,
            operation="mcp.disconnect",
        )
        logger.info("已断开 MCP 服务: %s", service_id)

    async def _connection_owner(
        self,
        definition: McpServiceDefinition,
        close_event: asyncio.Event,
        ready_future: asyncio.Future[tuple[ClientSession, McpServiceCapabilities]],
        *,
        owner_task_created_at: float,
    ) -> None:
        service_id = definition.service_id
        operation = "mcp.connection_owner"
        task_name = _current_task_name()
        transport_type = _transport_type_name(definition)
        owner_started_at = time.perf_counter()
        start_delay_ms = int((owner_started_at - owner_task_created_at) * 1000)
        get_runtime_task_registry().mark_progress(
            asyncio.current_task() or f"mcp-connection-owner-{service_id}",
            diagnostic_context={"service_id": service_id, "transport_type": transport_type, "phase": "owner_task.started"},
        )
        _log_mcp_phase(
            _slow_or_debug(start_delay_ms),
            "MCP connection owner task started",
            service_id=service_id,
            operation=operation,
            phase="owner_task.started",
            task_name=task_name,
            duration_ms=start_delay_ms,
            transport_type=transport_type,
            owner_task_name=task_name,
        )
        exit_stack = AsyncExitStack()
        try:
            exit_stack_entered_at = time.perf_counter()
            await exit_stack.__aenter__()
            _log_mcp_phase(
                _slow_or_debug(_duration_ms_since(exit_stack_entered_at)),
                "MCP connection owner exit stack entered",
                service_id=service_id,
                operation=operation,
                phase="exit_stack.entered",
                task_name=task_name,
                duration_ms=_duration_ms_since(exit_stack_entered_at),
                transport_type=transport_type,
                owner_task_name=task_name,
            )
            create_session_started_at = time.perf_counter()
            session = await self._create_session(definition, exit_stack)
            create_session_ms = _duration_ms_since(create_session_started_at)
            _log_mcp_phase(
                _slow_or_debug(create_session_ms),
                "MCP connection owner create session completed",
                service_id=service_id,
                operation=operation,
                phase="create_session.completed",
                task_name=task_name,
                duration_ms=create_session_ms,
                transport_type=transport_type,
                owner_task_name=task_name,
            )
            initialize_started_at = time.perf_counter()
            _log_mcp_phase(
                logging.DEBUG,
                "MCP session initialize started",
                service_id=service_id,
                operation=operation,
                phase="session.initialize.started",
                task_name=task_name,
                duration_ms=0,
                transport_type=transport_type,
                owner_task_name=task_name,
            )
            init_result = await session.initialize()
            initialize_ms = _duration_ms_since(initialize_started_at)
            _log_mcp_phase(
                _slow_or_debug(initialize_ms),
                "MCP session initialize completed",
                service_id=service_id,
                operation=operation,
                phase="session.initialize.completed",
                task_name=task_name,
                duration_ms=initialize_ms,
                transport_type=transport_type,
                owner_task_name=task_name,
            )
            capabilities = self._extract_capabilities(init_result)
            if not ready_future.done():
                ready_future.set_result((session, capabilities))
                get_runtime_task_registry().mark_progress(
                    asyncio.current_task() or f"mcp-connection-owner-{service_id}",
                    diagnostic_context={
                        "service_id": service_id,
                        "transport_type": transport_type,
                        "phase": "ready_future.set_result",
                    },
                )
                _log_mcp_phase(
                    logging.DEBUG,
                    "MCP ready future set result",
                    service_id=service_id,
                    operation=operation,
                    phase="ready_future.set_result",
                    task_name=task_name,
                    duration_ms=_duration_ms_since(owner_started_at),
                    transport_type=transport_type,
                    owner_task_name=task_name,
                )
            close_wait_started_at = time.perf_counter()
            get_runtime_task_registry().mark_progress(
                asyncio.current_task() or f"mcp-connection-owner-{service_id}",
                diagnostic_context={"service_id": service_id, "transport_type": transport_type, "phase": "close_event.waiting"},
            )
            await close_event.wait()
            get_runtime_task_registry().mark_progress(
                asyncio.current_task() or f"mcp-connection-owner-{service_id}",
                diagnostic_context={"service_id": service_id, "transport_type": transport_type, "phase": "close_event.received"},
            )
            _log_mcp_phase(
                _slow_or_debug(_duration_ms_since(close_wait_started_at)),
                "MCP connection owner close event received",
                service_id=service_id,
                operation=operation,
                phase="close_event.received",
                task_name=task_name,
                duration_ms=_duration_ms_since(close_wait_started_at),
                transport_type=transport_type,
                owner_task_name=task_name,
            )
        except asyncio.CancelledError as e:
            _log_mcp_phase(
                logging.WARNING,
                "MCP connection owner cancelled",
                service_id=service_id,
                operation=operation,
                phase="owner_task.cancelled",
                task_name=task_name,
                duration_ms=_duration_ms_since(owner_started_at),
                transport_type=transport_type,
                owner_task_name=task_name,
                error_type="CancelledError",
            )
            if not ready_future.done():
                ready_future.set_exception(ConnectionError(f"连接 MCP 服务 '{service_id}' 初始化被取消"))
            raise
        except Exception as e:
            _log_mcp_phase(
                logging.WARNING,
                "MCP connection owner failed",
                service_id=service_id,
                operation=operation,
                phase="owner_task.exception",
                task_name=task_name,
                duration_ms=_duration_ms_since(owner_started_at),
                transport_type=transport_type,
                owner_task_name=task_name,
                error_type=e.__class__.__name__,
                error=str(e),
            )
            if not ready_future.done():
                ready_future.set_exception(e)
            else:
                logger.warning("MCP 服务连接 owner 异常退出: %s — %s", service_id, e)
        finally:
            await self._close_exit_stack(exit_stack, service_id=service_id, operation=operation)
            current_task = asyncio.current_task()
            conn = self._connections.get(service_id)
            if conn is not None and conn.owner_task is current_task:
                self._connections.pop(service_id, None)

    async def _stop_owner_task(
        self,
        owner_task: asyncio.Task,
        close_event: asyncio.Event,
        *,
        service_id: str | None = None,
        operation: str = "mcp.stop_owner_task",
    ) -> None:
        actual_service_id = service_id or "unknown"
        task_name = _current_task_name()
        stop_started_at = time.perf_counter()
        close_event.set()
        _log_mcp_phase(
            logging.DEBUG,
            "MCP owner task stop requested",
            service_id=actual_service_id,
            operation=operation,
            phase="owner_task.close_event_set",
            task_name=task_name,
            duration_ms=0,
            timeout_seconds=_MCP_STOP_OWNER_TASK_TIMEOUT_SECONDS,
            owner_task_name=owner_task.get_name(),
        )
        if owner_task is asyncio.current_task():
            _log_mcp_phase(
                logging.DEBUG,
                "MCP owner task stop skipped for current task",
                service_id=actual_service_id,
                operation=operation,
                phase="owner_task.current_task_skip",
                task_name=task_name,
                duration_ms=_duration_ms_since(stop_started_at),
                timeout_seconds=_MCP_STOP_OWNER_TASK_TIMEOUT_SECONDS,
                owner_task_name=owner_task.get_name(),
            )
            return
        first_wait_started_at = time.perf_counter()
        try:
            await asyncio.wait_for(
                asyncio.shield(owner_task),
                timeout=_MCP_STOP_OWNER_TASK_TIMEOUT_SECONDS,
            )
            first_wait_ms = _duration_ms_since(first_wait_started_at)
            _log_mcp_phase(
                _slow_or_debug(first_wait_ms),
                "MCP owner task stopped after close event",
                service_id=actual_service_id,
                operation=operation,
                phase="owner_task.first_wait.completed",
                task_name=task_name,
                duration_ms=first_wait_ms,
                timeout_seconds=_MCP_STOP_OWNER_TASK_TIMEOUT_SECONDS,
                owner_task_name=owner_task.get_name(),
            )
        except TimeoutError:
            first_wait_ms = _duration_ms_since(first_wait_started_at)
            _log_mcp_phase(
                logging.WARNING,
                "MCP owner task did not stop after close event, cancelling",
                service_id=actual_service_id,
                operation=operation,
                phase="owner_task.first_wait.timeout",
                task_name=task_name,
                duration_ms=first_wait_ms,
                timeout_seconds=_MCP_STOP_OWNER_TASK_TIMEOUT_SECONDS,
                owner_task_name=owner_task.get_name(),
                error_type="TimeoutError",
            )
            get_runtime_task_registry().cancel(
                owner_task,
                message="mcp owner task stop timeout",
                diagnostic_context={"service_id": actual_service_id, "phase": "owner_task.first_wait.timeout"},
            )
            cancel_wait_started_at = time.perf_counter()
            try:
                await asyncio.wait_for(
                    asyncio.shield(owner_task),
                    timeout=_MCP_STOP_OWNER_TASK_TIMEOUT_SECONDS,
                )
                cancel_wait_ms = _duration_ms_since(cancel_wait_started_at)
                _log_mcp_phase(
                    _slow_or_debug(cancel_wait_ms),
                    "MCP owner task cancel wait completed",
                    service_id=actual_service_id,
                    operation=operation,
                    phase="owner_task.cancel_wait.completed",
                    task_name=task_name,
                    duration_ms=cancel_wait_ms,
                    timeout_seconds=_MCP_STOP_OWNER_TASK_TIMEOUT_SECONDS,
                    owner_task_name=owner_task.get_name(),
                )
            except TimeoutError:
                _log_mcp_phase(
                    logging.ERROR,
                    "MCP owner task cancel wait timed out",
                    service_id=actual_service_id,
                    operation=operation,
                    phase="owner_task.cancel_wait.timeout",
                    task_name=task_name,
                    duration_ms=_duration_ms_since(cancel_wait_started_at),
                    timeout_seconds=_MCP_STOP_OWNER_TASK_TIMEOUT_SECONDS,
                    owner_task_name=owner_task.get_name(),
                    error_type="TimeoutError",
                )
            except (Exception, asyncio.CancelledError) as exc:
                _log_mcp_phase(
                    logging.DEBUG,
                    "MCP owner task cancel wait ended with exception",
                    service_id=actual_service_id,
                    operation=operation,
                    phase="owner_task.cancel_wait.exception",
                    task_name=task_name,
                    duration_ms=_duration_ms_since(cancel_wait_started_at),
                    timeout_seconds=_MCP_STOP_OWNER_TASK_TIMEOUT_SECONDS,
                    owner_task_name=owner_task.get_name(),
                    error_type=exc.__class__.__name__,
                    error=str(exc),
                )
        except (Exception, asyncio.CancelledError) as exc:
            _log_mcp_phase(
                logging.DEBUG,
                "MCP owner task first wait ended with exception",
                service_id=actual_service_id,
                operation=operation,
                phase="owner_task.first_wait.exception",
                task_name=task_name,
                duration_ms=_duration_ms_since(first_wait_started_at),
                timeout_seconds=_MCP_STOP_OWNER_TASK_TIMEOUT_SECONDS,
                owner_task_name=owner_task.get_name(),
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
        finally:
            total_ms = _duration_ms_since(stop_started_at)
            _log_mcp_phase(
                _slow_or_debug(total_ms),
                "MCP owner task stop sequence completed",
                service_id=actual_service_id,
                operation=operation,
                phase="owner_task.stop_sequence.completed",
                task_name=task_name,
                duration_ms=total_ms,
                timeout_seconds=_MCP_STOP_OWNER_TASK_TIMEOUT_SECONDS,
                owner_task_name=owner_task.get_name(),
            )

    async def _close_exit_stack(
        self,
        exit_stack: AsyncExitStack,
        *,
        service_id: str | None = None,
        operation: str = "mcp.connection_owner",
    ) -> None:
        actual_service_id = service_id or "unknown"
        task_name = _current_task_name()
        close_started_at = time.perf_counter()
        try:
            await exit_stack.__aexit__(None, None, None)
            close_ms = _duration_ms_since(close_started_at)
            _log_mcp_phase(
                _slow_or_debug(close_ms),
                "MCP exit stack close completed",
                service_id=actual_service_id,
                operation=operation,
                phase="exit_stack.close.completed",
                task_name=task_name,
                duration_ms=close_ms,
            )
        except (Exception, asyncio.CancelledError) as e:
            _log_mcp_phase(
                logging.WARNING,
                "MCP exit stack close failed",
                service_id=actual_service_id,
                operation=operation,
                phase="exit_stack.close.exception",
                task_name=task_name,
                duration_ms=_duration_ms_since(close_started_at),
                error_type=e.__class__.__name__,
                error=str(e),
            )

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
            get_runtime_task_registry().mark_progress(
                asyncio.current_task() or f"mcp-reconnect-loop-{service_id}",
                diagnostic_context={"service_id": service_id, "phase": "reconnect_loop.started"},
            )
            while definition.enabled and not self.is_connected(service_id):
                if state.retry_attempt > 0:
                    delay = self._retry_delay(state.retry_attempt)
                    state.next_retry_at = time.time() + delay
                    get_runtime_task_registry().mark_progress(
                        asyncio.current_task() or f"mcp-reconnect-loop-{service_id}",
                        diagnostic_context={
                            "service_id": service_id,
                            "phase": "reconnect_loop.sleeping",
                            "retry_attempt": state.retry_attempt,
                            "delay_seconds": delay,
                        },
                    )
                    await asyncio.sleep(delay)
                try:
                    get_runtime_task_registry().mark_progress(
                        asyncio.current_task() or f"mcp-reconnect-loop-{service_id}",
                        diagnostic_context={
                            "service_id": service_id,
                            "phase": "reconnect_loop.connecting",
                            "retry_attempt": state.retry_attempt,
                        },
                    )
                    await self._connect(
                        definition,
                        status=MCP_CONNECTION_STATUS_RECONNECTING,
                    )
                    get_runtime_task_registry().mark_progress(
                        asyncio.current_task() or f"mcp-reconnect-loop-{service_id}",
                        diagnostic_context={"service_id": service_id, "phase": "reconnect_loop.connected"},
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

        service_id = definition.service_id
        operation = "mcp.create_session"
        task_name = _current_task_name()
        transport_type = _transport_type_name(definition)
        create_started_at = time.perf_counter()
        tc = definition.transport

        _log_mcp_phase(
            logging.DEBUG,
            "MCP create session started",
            service_id=service_id,
            operation=operation,
            phase="create_session.started",
            task_name=task_name,
            duration_ms=0,
            transport_type=transport_type,
        )
        try:
            transport_started_at = time.perf_counter()
            _log_mcp_phase(
                logging.DEBUG,
                "MCP transport context enter started",
                service_id=service_id,
                operation=operation,
                phase="transport.enter.started",
                task_name=task_name,
                duration_ms=0,
                transport_type=transport_type,
            )
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
            transport_ms = _duration_ms_since(transport_started_at)
            _log_mcp_phase(
                _slow_or_debug(transport_ms),
                "MCP transport context entered",
                service_id=service_id,
                operation=operation,
                phase="transport.enter.completed",
                task_name=task_name,
                duration_ms=transport_ms,
                transport_type=transport_type,
            )

            session_started_at = time.perf_counter()
            session = await exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            session_ms = _duration_ms_since(session_started_at)
            _log_mcp_phase(
                _slow_or_debug(session_ms),
                "MCP client session context entered",
                service_id=service_id,
                operation=operation,
                phase="client_session.enter.completed",
                task_name=task_name,
                duration_ms=session_ms,
                transport_type=transport_type,
            )
            _log_mcp_phase(
                _slow_or_debug(_duration_ms_since(create_started_at)),
                "MCP create session completed",
                service_id=service_id,
                operation=operation,
                phase="create_session.completed",
                task_name=task_name,
                duration_ms=_duration_ms_since(create_started_at),
                transport_type=transport_type,
            )
            return session
        except Exception as exc:
            _log_mcp_phase(
                logging.ERROR,
                "MCP create session failed",
                service_id=service_id,
                operation=operation,
                phase="create_session.exception",
                task_name=task_name,
                duration_ms=_duration_ms_since(create_started_at),
                transport_type=transport_type,
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
            raise

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

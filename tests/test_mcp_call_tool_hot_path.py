import asyncio
import time
from types import SimpleNamespace

import pytest

from agent_runtime.mcp.mcp_client.infrastructure import mcp_session_manager as session_module
from agent_runtime.mcp.mcp_client.infrastructure.mcp_session_manager import McpSessionManager
from agent_runtime.mcp.mcp_client.mcp_client_service import McpClientService
from agent_runtime.mcp.mcp_client.mcp_service_definition import McpServiceDefinition
from agent_runtime.mcp.mcp_client.mcp_transport_config import StdioTransportConfig
from agent_runtime.mcp.mcp_client.mcp_value_objects import (
    MCP_CONNECTION_STATUS_FAILED,
    MCP_CONNECTION_STATUS_RECONNECTING,
    McpServiceCapabilities,
)


class _Repository:
    def __init__(self, definition: McpServiceDefinition) -> None:
        self.definition = definition

    def find_all(self):
        return [self.definition]

    def find_by_id(self, service_id: str):
        if service_id == self.definition.service_id:
            return self.definition
        return None

    def save(self, definition: McpServiceDefinition) -> None:
        self.definition = definition

    def delete(self, service_id: str) -> bool:
        return service_id == self.definition.service_id

    def contains(self, service_id: str) -> bool:
        return service_id == self.definition.service_id

    def config_dir_mtime(self) -> float:
        return 0.0


def _definition(service_id: str = "hot-path") -> McpServiceDefinition:
    return McpServiceDefinition(
        service_id=service_id,
        name="Hot Path MCP",
        transport=StdioTransportConfig(command="never-called"),
        enabled=True,
    )


def _service(manager: McpSessionManager, definition: McpServiceDefinition | None = None) -> McpClientService:
    return McpClientService(_Repository(definition or _definition()), manager)


async def _assert_fast(awaitable, *, max_seconds: float = 0.05):
    started = time.perf_counter()
    with pytest.raises(ConnectionError) as exc_info:
        await awaitable
    assert time.perf_counter() - started < max_seconds
    return str(exc_info.value)


def test_call_tool_fail_fast_when_service_is_disconnected():
    async def scenario() -> None:
        manager = McpSessionManager()
        service = _service(manager)

        message = await _assert_fast(service.call_tool("hot-path", "do_work", {}))

        assert "当前不可用" in message
        assert "disconnected" in message
        assert "快速失败" in message
        assert manager.get_connection_status("hot-path").connected is False

        await manager.disconnect_all()

    asyncio.run(scenario())


def test_call_tool_fail_fast_when_service_failed_or_reconnecting():
    async def scenario() -> None:
        manager = McpSessionManager()
        service = _service(manager)

        failed_state = manager._state("hot-path")
        failed_state.status = MCP_CONNECTION_STATUS_FAILED
        failed_state.last_error = "connection refused"
        failed_state.retry_attempt = 2
        failed_state.next_retry_at = time.time() + 30

        failed_message = await _assert_fast(service.call_tool("hot-path", "do_work", {}))
        assert "failed" in failed_message
        assert "connection refused" in failed_message
        assert "重连尝试次数: 2" in failed_message

        reconnecting_state = manager._state("hot-path")
        reconnecting_state.status = MCP_CONNECTION_STATUS_RECONNECTING
        reconnecting_message = await _assert_fast(service.call_tool("hot-path", "do_work", {}))
        assert "reconnecting" in reconnecting_message

        await manager.disconnect_all()

    asyncio.run(scenario())


def test_call_tool_does_not_wait_for_reconnect_lock_when_not_connected():
    async def scenario() -> None:
        manager = McpSessionManager()
        service = _service(manager)
        state = manager._state("hot-path")
        state.status = MCP_CONNECTION_STATUS_RECONNECTING
        await state.lock.acquire()
        try:
            message = await _assert_fast(service.call_tool("hot-path", "do_work", {}))
            assert "reconnecting" in message
        finally:
            state.lock.release()
            await manager.disconnect_all()

    asyncio.run(scenario())


def test_call_tool_business_timeout_marks_connection_lost_and_returns_failure(monkeypatch):
    async def scenario() -> None:
        monkeypatch.setattr(session_module, "_MCP_TOOL_CALL_BUSINESS_TIMEOUT_SECONDS", 0.01)
        manager = McpSessionManager()
        service = _service(manager)
        close_event = asyncio.Event()
        owner_task = asyncio.create_task(close_event.wait())
        owner_task.set_name("fake-mcp-owner-hot-path")
        manager._connections["hot-path"] = session_module._ManagedConnection(
            session=SimpleNamespace(call_tool=lambda tool_name, arguments: asyncio.sleep(3600)),
            capabilities=McpServiceCapabilities(server_name="fake", server_version="1.0.0", supports_tools=True),
            close_event=close_event,
            owner_task=owner_task,
        )
        manager._state("hot-path").status = "connected"

        started = time.perf_counter()
        with pytest.raises(ConnectionError) as exc_info:
            await service.call_tool("hot-path", "slow_tool", {})
        elapsed = time.perf_counter() - started

        assert elapsed < 0.2
        message = str(exc_info.value)
        assert "执行超时" in message
        assert "slow_tool" in message
        status = manager.get_connection_status("hot-path")
        assert status.connected is False
        assert status.status == MCP_CONNECTION_STATUS_FAILED
        assert status.last_error is not None and "执行超时" in status.last_error

        await asyncio.wait_for(close_event.wait(), timeout=0.2)
        await owner_task

    asyncio.run(scenario())

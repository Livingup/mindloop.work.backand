import asyncio
import logging
import threading

import pytest

from agent_runtime.infrastructure.asyncio_diagnostics import (
    configure_blocking_pool,
    monitored_to_thread,
    shutdown_blocking_executors,
)
from agent_runtime.tool.local_tool_definition import LocalToolDefinition
from agent_runtime.tool.local_tool_service import LocalToolService
from agent_runtime.tool.tool_execution_models import BaseToolResult


class _Repository:
    def __init__(self, tool_def: LocalToolDefinition) -> None:
        self._tool_def = tool_def

    def list_all(self):
        return [self._tool_def]

    def get_by_name(self, tool_name: str) -> LocalToolDefinition:
        if tool_name != self._tool_def.tool_name:
            raise KeyError(tool_name)
        return self._tool_def


class _SchemaCache:
    def get_all_schemas(self, _tool_def):
        return {}


def _service(tool_def: LocalToolDefinition) -> LocalToolService:
    return LocalToolService(_Repository(tool_def), _SchemaCache())


def test_direct_sync_tool_remains_inline_and_redacts_raw_params_from_logs(caplog: pytest.LogCaptureFixture) -> None:
    caller_thread = threading.current_thread().name

    def direct_tool(secret: str) -> str:
        return f"{threading.current_thread().name}:{secret}"

    service = _service(
        LocalToolDefinition(
            tool_name="direct_tool",
            description="Direct sync test tool.",
            fn=direct_tool,
        )
    )

    async def run_case() -> BaseToolResult:
        with caplog.at_level(logging.INFO, logger="agent_runtime.tool.local_tool_service"):
            return await service.execute_tool("direct_tool", {"secret": "raw-secret-value"})

    result = asyncio.run(run_case())

    assert result.success is True
    assert result.output == f"{caller_thread}:raw-secret-value"
    assert "raw-secret-value" not in caplog.text
    assert "param_keys=['secret']" in caplog.text
    assert "param_key_count=1" in caplog.text


def test_blocking_sync_tool_uses_declared_blocking_pool() -> None:
    pool_name = "unit_local_tool_blocking"
    configure_blocking_pool(
        pool_name,
        max_workers=1,
        isolated=True,
        thread_name_prefix="test-local-tool",
        max_pending=2,
    )
    try:
        def blocking_tool() -> str:
            return threading.current_thread().name

        service = _service(
            LocalToolDefinition(
                tool_name="blocking_tool",
                description="Blocking sync test tool.",
                fn=blocking_tool,
                execution_mode="blocking",
                blocking_pool=pool_name,
                blocking_operation="unit.local_tool.blocking",
            )
        )

        result = asyncio.run(service.execute_tool("blocking_tool", {}))

        assert result.success is True
        assert result.output.startswith("test-local-tool")
    finally:
        shutdown_blocking_executors(wait=True, cancel_futures=True)


def test_async_tool_remains_await_native_even_when_direct() -> None:
    async def async_tool() -> str:
        await asyncio.sleep(0)
        return "async-result"

    service = _service(
        LocalToolDefinition(
            tool_name="async_tool",
            description="Async test tool.",
            fn=async_tool,
        )
    )

    result = asyncio.run(service.execute_tool("async_tool", {}))

    assert result.success is True
    assert result.output == "async-result"


def test_blocking_pool_overload_becomes_structured_tool_error() -> None:
    pool_name = "unit_local_tool_overload"
    configure_blocking_pool(
        pool_name,
        max_workers=1,
        isolated=True,
        thread_name_prefix="test-local-tool-overload",
        max_pending=1,
    )
    release_worker = threading.Event()
    first_started = threading.Event()

    def slow_work(label: str) -> str:
        first_started.set()
        release_worker.wait(timeout=5)
        return label

    def rejected_tool() -> str:
        return "should-not-run"

    service = _service(
        LocalToolDefinition(
            tool_name="rejected_tool",
            description="Rejected blocking sync test tool.",
            fn=rejected_tool,
            execution_mode="blocking",
            blocking_pool=pool_name,
            blocking_operation="unit.local_tool.rejected",
        )
    )

    async def run_case() -> BaseToolResult:
        first = asyncio.create_task(
            monitored_to_thread(
                slow_work,
                "first",
                operation="unit.local_tool.fill_pool",
                caller="test_blocking_pool_overload_becomes_structured_tool_error",
                pool=pool_name,
            )
        )
        while not first_started.is_set():
            await asyncio.sleep(0.01)
        queued = asyncio.create_task(
            monitored_to_thread(
                slow_work,
                "queued",
                operation="unit.local_tool.fill_pool",
                caller="test_blocking_pool_overload_becomes_structured_tool_error",
                pool=pool_name,
            )
        )
        await asyncio.sleep(0.05)
        result = await service.execute_tool("rejected_tool", {})
        release_worker.set()
        assert await first == "first"
        assert await queued == "queued"
        return result

    try:
        result = asyncio.run(run_case())
    finally:
        release_worker.set()
        shutdown_blocking_executors(wait=True, cancel_futures=True)

    assert result.success is False
    assert result.error_code == "BlockingPoolOverloadedError"
    assert "unit.local_tool.rejected" in (result.error_message or "")

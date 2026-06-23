import asyncio
import contextvars
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_runtime.infrastructure.asyncio_diagnostics import (
    BlockingPool,
    BlockingPoolConfig,
    BlockingPoolOverloadedError,
    BlockingScheduler,
    ExecutorRegistry,
    _executor_debug_snapshot,
    _new_blocking_call_id,
    _sanitize_context,
    _sanitize_error_text,
    blocking_scheduler_health_snapshot,
    configure_blocking_pool,
    default_thread_pool_max_workers,
    monitored_to_thread,
    shutdown_blocking_executors,
)

from agent_runtime.runtime_health import runtime_concurrency_health_snapshot


def _scheduler_with_test_pools() -> BlockingScheduler:
    return BlockingScheduler(
        ExecutorRegistry(
            {
                BlockingPool.DEFAULT.value: BlockingPoolConfig(
                    pool_name=BlockingPool.DEFAULT.value,
                    max_workers=2,
                    thread_name_prefix="test-default",
                    isolated=True,
                ),
                BlockingPool.COMMUNICATION.value: BlockingPoolConfig(
                    pool_name=BlockingPool.COMMUNICATION.value,
                    max_workers=1,
                    thread_name_prefix="test-communication",
                    isolated=True,
                ),
                BlockingPool.FILE_RESOURCE.value: BlockingPoolConfig(
                    pool_name=BlockingPool.FILE_RESOURCE.value,
                    max_workers=1,
                    thread_name_prefix="test-file-resource",
                    isolated=False,
                ),
            }
        )
    )


def test_blocking_call_id_is_stable_human_readable_shape() -> None:
    blocking_call_id = _new_blocking_call_id("communication", "publish message/append")

    assert re.fullmatch(r"communication:publish-message-append:[0-9a-f]{12}", blocking_call_id)


def test_sanitizers_redact_sensitive_context_and_error_text() -> None:
    sanitized = _sanitize_context(
        {
            "authorization": "Bearer secret-token",
            "command_line": "python secret.py --token value",
            "payload_length": 1024,
            "source_url": "https://user:password@example.com:8443/path?q=secret",
            "nested": {"refresh_token": "hidden", "safe": "visible"},
        }
    )

    assert sanitized["authorization"] == "[redacted]"
    assert sanitized["command_line"] == "[redacted]"
    assert sanitized["payload_length"] == "[redacted]"
    assert sanitized["source_url"] == "https://example.com:8443/path"
    assert sanitized["nested"] == {"refresh_token": "[redacted]", "safe": "visible"}
    assert _sanitize_error_text("Authorization bearer token leaked") == "[redacted]"


def test_executor_registry_defaults_and_shared_pool_mapping() -> None:
    scheduler = _scheduler_with_test_pools()
    try:
        snapshot = scheduler.health_snapshot()["executors"]

        assert snapshot[BlockingPool.COMMUNICATION.value]["isolated"] is True
        assert snapshot[BlockingPool.COMMUNICATION.value]["executor_pool"] == BlockingPool.COMMUNICATION.value
        assert snapshot[BlockingPool.FILE_RESOURCE.value]["isolated"] is False
        assert snapshot[BlockingPool.FILE_RESOURCE.value]["executor_pool"] == BlockingPool.DEFAULT.value
        assert snapshot[BlockingPool.DEFAULT.value]["max_workers"] == 2
    finally:
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_default_registry_isolates_selected_blocking_pools_with_bounded_pending_capacity() -> None:
    scheduler = BlockingScheduler()
    try:
        executors = scheduler.health_snapshot()["executors"]

        for pool in (
            BlockingPool.FILE_RESOURCE,
            BlockingPool.TERMINAL,
            BlockingPool.TERMINAL_CONTROL,
            BlockingPool.TERMINAL_RENDER,
            BlockingPool.DB_BLOCKING,
            BlockingPool.NETWORK,
        ):
            pool_snapshot = executors[pool.value]
            assert pool_snapshot["isolated"] is True
            assert pool_snapshot["executor_pool"] == pool.value
            assert pool_snapshot["max_pending"] == pool_snapshot["max_workers"] * 4

        terminal_read_snapshot = executors[BlockingPool.TERMINAL_READ.value]
        assert terminal_read_snapshot["isolated"] is True
        assert terminal_read_snapshot["executor_pool"] == BlockingPool.TERMINAL_READ.value
        assert terminal_read_snapshot["max_pending"] == terminal_read_snapshot["max_workers"] * 2
        assert terminal_read_snapshot["executor_pool"] != executors[BlockingPool.TERMINAL_CONTROL.value]["executor_pool"]

        cpu_render_snapshot = executors[BlockingPool.CPU_RENDER.value]
        assert cpu_render_snapshot["isolated"] is False
        assert cpu_render_snapshot["executor_pool"] == BlockingPool.DEFAULT.value
        assert cpu_render_snapshot["max_pending"] is None
    finally:
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_executor_registry_reconfigure_recreates_executor() -> None:
    scheduler = _scheduler_with_test_pools()
    try:
        executor, _logical, executor_pool_name, _executor_config = scheduler.executor_registry.get_executor(
            BlockingPool.COMMUNICATION
        )
        old_executor_id = id(executor)

        config = scheduler.configure_pool(BlockingPool.COMMUNICATION, max_workers=2, isolated=True)
        new_executor, _logical, new_executor_pool_name, new_executor_config = scheduler.executor_registry.get_executor(
            BlockingPool.COMMUNICATION
        )

        assert config.max_workers == 2
        assert executor_pool_name == new_executor_pool_name == BlockingPool.COMMUNICATION.value
        assert new_executor_config.max_workers == 2
        assert id(new_executor) != old_executor_id
    finally:
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_executor_debug_snapshot_reports_thread_pool_state() -> None:
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="snapshot-test") as executor:
        snapshot_before = _executor_debug_snapshot(executor)
        future = executor.submit(lambda: threading.current_thread().name)
        worker_thread_name = future.result(timeout=5)
        snapshot_after = _executor_debug_snapshot(executor)

    assert snapshot_before["executor_id"].startswith("0x")
    assert snapshot_before["max_workers"] == 1
    assert snapshot_after["thread_count"] >= 1
    assert snapshot_after["alive_thread_count"] >= 1
    assert worker_thread_name.startswith("snapshot-test")


def test_scheduler_preserves_contextvars_and_records_success_counters(caplog: pytest.LogCaptureFixture) -> None:
    scheduler = _scheduler_with_test_pools()
    request_id = contextvars.ContextVar("request_id")
    request_id.set("req-123")
    test_logger = logging.getLogger("tests.asyncio_diagnostics.success")

    async def run_case() -> None:
        with caplog.at_level(logging.DEBUG, logger=test_logger.name):
            result = await scheduler.run(
                lambda suffix: (request_id.get(), threading.current_thread().name, suffix),
                "done",
                operation="unit.success",
                caller="test_scheduler_preserves_contextvars_and_records_success_counters",
                pool=BlockingPool.COMMUNICATION,
                log=test_logger,
                slow_queue_seconds=60.0,
                slow_execution_seconds=60.0,
                diagnostic_context={"safe": "value", "authorization": "secret"},
            )

        assert result[0] == "req-123"
        assert result[1].startswith("test-communication")
        assert result[2] == "done"

    try:
        asyncio.run(run_case())
        snapshot = scheduler.health_snapshot()
        pool_stats = snapshot["pools"][BlockingPool.COMMUNICATION.value]
        op_stats = pool_stats["operations"]["unit.success"]

        assert pool_stats["submitted"] == 1
        assert pool_stats["completed"] == 1
        assert pool_stats["pending"] == 0
        assert pool_stats["active"] == 0
        assert op_stats["submitted"] == 1
        assert op_stats["completed"] == 1
        assert "blocking_call_id=communication:unit.success:" in caplog.text
        assert "phase=blocking.worker_started" in caplog.text
        assert "phase=blocking.executor_after_submit" in caplog.text
        assert "authorization=[redacted]" in caplog.text
    finally:
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_scheduler_records_worker_failure_and_sanitizes_error(caplog: pytest.LogCaptureFixture) -> None:
    scheduler = _scheduler_with_test_pools()
    test_logger = logging.getLogger("tests.asyncio_diagnostics.failure")

    def failing_work() -> None:
        raise RuntimeError("token should not appear in diagnostics")

    async def run_case() -> None:
        with caplog.at_level(logging.DEBUG, logger=test_logger.name):
            with pytest.raises(RuntimeError):
                await scheduler.run(
                    failing_work,
                    operation="unit.failure",
                    caller="test_scheduler_records_worker_failure_and_sanitizes_error",
                    pool=BlockingPool.COMMUNICATION,
                    log=test_logger,
                )

    try:
        asyncio.run(run_case())
        pool_stats = scheduler.health_snapshot()["pools"][BlockingPool.COMMUNICATION.value]
        op_stats = pool_stats["operations"]["unit.failure"]

        assert pool_stats["failed"] == 1
        assert pool_stats["pending"] == 0
        assert pool_stats["active"] == 0
        assert op_stats["failed"] == 1
        assert "phase=blocking.worker_failed" in caplog.text
        assert "error=[redacted]" in caplog.text
        assert "token should not appear" not in caplog.text
    finally:
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_scheduler_records_await_cancellation_and_worker_finish(caplog: pytest.LogCaptureFixture) -> None:
    scheduler = _scheduler_with_test_pools()
    test_logger = logging.getLogger("tests.asyncio_diagnostics.cancel")
    release_worker = threading.Event()

    def slow_work() -> str:
        release_worker.wait(timeout=5)
        return "finished"

    async def run_case() -> None:
        with caplog.at_level(logging.DEBUG, logger=test_logger.name):
            task = asyncio.create_task(
                scheduler.run(
                    slow_work,
                    operation="unit.cancel",
                    caller="test_scheduler_records_await_cancellation_and_worker_finish",
                    pool=BlockingPool.COMMUNICATION,
                    log=test_logger,
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release_worker.set()
            await asyncio.sleep(0.05)

    try:
        asyncio.run(run_case())
        pool_stats = scheduler.health_snapshot()["pools"][BlockingPool.COMMUNICATION.value]
        op_stats = pool_stats["operations"]["unit.cancel"]

        assert pool_stats["cancelled"] == 1
        assert pool_stats["completed"] == 1
        assert pool_stats["pending"] == 0
        assert pool_stats["active"] == 0
        assert op_stats["cancelled"] == 1
        assert "phase=blocking.await_cancelled" in caplog.text
        assert "phase=blocking.worker_finished_after_await_cancelled" in caplog.text
    finally:
        release_worker.set()
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_scheduler_rejects_over_max_pending_without_submitting(caplog: pytest.LogCaptureFixture) -> None:
    scheduler = BlockingScheduler(
        ExecutorRegistry(
            {
                BlockingPool.COMMUNICATION.value: BlockingPoolConfig(
                    pool_name=BlockingPool.COMMUNICATION.value,
                    max_workers=1,
                    thread_name_prefix="test-communication",
                    isolated=True,
                    max_pending=1,
                )
            }
        )
    )
    test_logger = logging.getLogger("tests.asyncio_diagnostics.rejected")
    release_worker = threading.Event()
    first_started = threading.Event()
    submitted_labels: list[str] = []

    def slow_work(label: str) -> str:
        submitted_labels.append(label)
        first_started.set()
        release_worker.wait(timeout=5)
        return label

    async def run_case() -> None:
        first = asyncio.create_task(
            scheduler.run(
                slow_work,
                "first",
                operation="unit.rejected",
                caller="test_scheduler_rejects_over_max_pending_without_submitting",
                pool=BlockingPool.COMMUNICATION,
                log=test_logger,
            )
        )
        while not first_started.is_set():
            await asyncio.sleep(0.01)
        queued = asyncio.create_task(
            scheduler.run(
                slow_work,
                "queued",
                operation="unit.rejected",
                caller="test_scheduler_rejects_over_max_pending_without_submitting",
                pool=BlockingPool.COMMUNICATION,
                log=test_logger,
            )
        )
        while scheduler.health_snapshot()["pools"][BlockingPool.COMMUNICATION.value]["pending"] < 1:
            await asyncio.sleep(0.01)
        with caplog.at_level(logging.WARNING, logger=test_logger.name):
            with pytest.raises(BlockingPoolOverloadedError) as exc_info:
                await scheduler.run(
                    slow_work,
                    "rejected",
                    operation="unit.rejected",
                    caller="test_scheduler_rejects_over_max_pending_without_submitting",
                    pool=BlockingPool.COMMUNICATION,
                    log=test_logger,
                )
        assert exc_info.value.pool_name == BlockingPool.COMMUNICATION.value
        assert exc_info.value.pending == 1
        assert exc_info.value.max_pending == 1
        release_worker.set()
        assert await first == "first"
        assert await queued == "queued"

    try:
        asyncio.run(run_case())
        pool_stats = scheduler.health_snapshot()["pools"][BlockingPool.COMMUNICATION.value]
        op_stats = pool_stats["operations"]["unit.rejected"]

        assert submitted_labels == ["first", "queued"]
        assert pool_stats["max_pending"] == 1
        assert pool_stats["submitted"] == 2
        assert pool_stats["completed"] == 2
        assert pool_stats["rejected"] == 1
        assert pool_stats["overloaded"] == 1
        assert pool_stats["pending"] == 0
        assert pool_stats["active"] == 0
        assert op_stats["rejected"] == 1
        assert op_stats["overloaded"] == 1

        health = runtime_concurrency_health_snapshot(
            event_loop_snapshot={"monitor_running": True, "status": "ok"},
            network_snapshot={},
            runtime_task_summary={
                "total_count": 0,
                "active_count": 0,
                "by_status": {},
                "active_by_owner": {},
                "active_by_class": {},
                "recent_terminal_tasks": [],
            },
            blocking_scheduler_snapshot=scheduler.health_snapshot(),
            include_blocking_operations=True,
        )
        blocking_summary = health["blocking_scheduler"]
        assert health["status"] == "degraded"
        assert blocking_summary["logical_totals"]["rejected"] == 1
        assert blocking_summary["logical_totals"]["overloaded"] == 1
        assert health["signals"]["blocking_scheduler"]["rejected_operations"] == 1
        assert health["signals"]["blocking_scheduler"]["overloaded_operations"] == 1
        assert health["signals"]["blocking_scheduler"]["overloaded"] is True
        assert blocking_summary["top_operations"][0]["rejected"] == 1
        assert blocking_summary["top_operations"][0]["overloaded"] == 1

        assert "phase=blocking.rejected" in caplog.text
        assert "pool_rejected=1" in caplog.text
        assert "operation_overloaded=1" in caplog.text
    finally:
        release_worker.set()
        scheduler.shutdown(wait=True, cancel_futures=True)


def test_scheduler_records_submit_failure_without_stale_pending(caplog: pytest.LogCaptureFixture) -> None:
    scheduler = _scheduler_with_test_pools()
    test_logger = logging.getLogger("tests.asyncio_diagnostics.submit_failure")

    async def run_case() -> None:
        executor, _logical, _executor_pool_name, _executor_config = scheduler.executor_registry.get_executor(
            BlockingPool.COMMUNICATION
        )
        executor.shutdown(wait=True, cancel_futures=True)
        with caplog.at_level(logging.DEBUG, logger=test_logger.name):
            with pytest.raises(RuntimeError):
                await scheduler.run(
                    lambda: None,
                    operation="unit.submit_failure",
                    caller="test_scheduler_records_submit_failure_without_stale_pending",
                    pool=BlockingPool.COMMUNICATION,
                    log=test_logger,
                )

    asyncio.run(run_case())
    pool_stats = scheduler.health_snapshot()["pools"][BlockingPool.COMMUNICATION.value]
    op_stats = pool_stats["operations"]["unit.submit_failure"]

    assert pool_stats["submitted"] == 1
    assert pool_stats["failed"] == 1
    assert pool_stats["pending"] == 0
    assert op_stats["failed"] == 1
    assert "phase=blocking.submit_failed" in caplog.text
    assert "shutdown=True" in caplog.text


def test_global_pool_configuration_and_health_snapshot_api() -> None:
    original_max_workers = default_thread_pool_max_workers()
    try:
        config = configure_blocking_pool(
            BlockingPool.FILE_RESOURCE,
            max_workers=1,
            isolated=True,
            thread_name_prefix="test-global-file-resource",
            max_pending=2,
        )
        snapshot = blocking_scheduler_health_snapshot()

        assert config.isolated is True
        assert config.max_pending == 2
        assert snapshot["executors"][BlockingPool.FILE_RESOURCE.value]["isolated"] is True
        assert snapshot["executors"][BlockingPool.FILE_RESOURCE.value]["max_workers"] == 1
        assert snapshot["executors"][BlockingPool.FILE_RESOURCE.value]["max_pending"] == 2

        result = asyncio.run(
            monitored_to_thread(
                lambda: threading.current_thread().name,
                operation="unit.global",
                caller="test_global_pool_configuration_and_health_snapshot_api",
                pool=BlockingPool.FILE_RESOURCE,
            )
        )
        assert result.startswith("test-global-file-resource")
    finally:
        reset_workers = max(4, min(16, original_max_workers))
        configure_blocking_pool(
            BlockingPool.FILE_RESOURCE,
            max_workers=reset_workers,
            isolated=True,
            thread_name_prefix="blocking-file-resource",
            max_pending=reset_workers * 4,
        )
        shutdown_blocking_executors(wait=True, cancel_futures=True)

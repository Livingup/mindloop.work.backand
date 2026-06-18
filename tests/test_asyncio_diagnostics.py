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
        )
        snapshot = blocking_scheduler_health_snapshot()

        assert config.isolated is True
        assert snapshot["executors"][BlockingPool.FILE_RESOURCE.value]["isolated"] is True
        assert snapshot["executors"][BlockingPool.FILE_RESOURCE.value]["max_workers"] == 1

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
        configure_blocking_pool(
            BlockingPool.FILE_RESOURCE,
            max_workers=max(4, min(16, original_max_workers)),
            isolated=False,
            thread_name_prefix="blocking-file-resource",
        )
        shutdown_blocking_executors(wait=True, cancel_futures=True)

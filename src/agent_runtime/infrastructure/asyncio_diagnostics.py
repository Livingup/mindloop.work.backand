from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

_R = TypeVar("_R")

logger = logging.getLogger(__name__)

DEFAULT_TO_THREAD_SLOW_QUEUE_SECONDS = 1.0
DEFAULT_TO_THREAD_SLOW_EXECUTION_SECONDS = 1.0
_MAX_CONTEXT_VALUE_LENGTH = 500


class BlockingPool(str, Enum):
    """Logical pools for blocking Runtime work."""

    COMMUNICATION = "communication"
    TERMINAL = "terminal"
    TERMINAL_READ = "terminal_read"
    TERMINAL_CONTROL = "terminal_control"
    TERMINAL_RENDER = "terminal_render"
    CPU_RENDER = "cpu_render"
    FILE_RESOURCE = "file_resource"
    DB_BLOCKING = "db_blocking"
    NETWORK = "network"
    DIAGNOSTIC = "diagnostic"
    MAINTENANCE = "maintenance"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class BlockingPoolConfig:
    pool_name: str
    max_workers: int
    thread_name_prefix: str
    isolated: bool = True
    max_pending: int | None = None


@dataclass(slots=True)
class _BlockingStats:
    submitted: int = 0
    pending: int = 0
    active: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    rejected: int = 0
    overloaded: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "submitted": self.submitted,
            "pending": self.pending,
            "active": self.active,
            "completed": self.completed,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "rejected": self.rejected,
            "overloaded": self.overloaded,
        }


@dataclass(slots=True)
class _StatsSnapshot:
    pool: dict[str, int]
    operation: dict[str, int]


@dataclass(slots=True)
class _AdmissionResult:
    accepted: bool
    snapshot: _StatsSnapshot
    pending: int
    max_pending: int | None


@dataclass(slots=True)
class _AdmissionWaiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[_AdmissionResult]
    operation: str
    max_pending: int | None
    admission: _AdmissionResult | None = None


def _complete_admission_waiter(
    future: asyncio.Future[_AdmissionResult],
    admission: _AdmissionResult,
) -> None:
    if not future.done():
        future.set_result(admission)


class BlockingPoolOverloadedError(RuntimeError):
    """Raised when a blocking pool rejects work before executor submission."""

    def __init__(self, *, pool_name: str, operation: str, pending: int, max_pending: int) -> None:
        self.pool_name = pool_name
        self.operation = operation
        self.pending = pending
        self.max_pending = max_pending
        super().__init__(
            "Blocking pool overloaded: "
            f"pool={pool_name} operation={operation} pending={pending} max_pending={max_pending}"
        )


@dataclass(slots=True)
class _SubmittedBlockingCall:
    blocking_call_id: str
    pool_name: str
    executor_pool_name: str
    operation: str
    caller: str
    task_name: str | None
    function_name: str | None
    function_module: str | None
    submitted_at: float
    queue_slow_threshold_ms: int
    execution_slow_threshold_ms: int
    pool_max_workers: int
    diagnostic_context: dict[str, Any]
    target_logger: logging.Logger
    pool_snapshot: dict[str, int]
    operation_snapshot: dict[str, int]


def default_thread_pool_max_workers() -> int:
    """Return CPython's default ThreadPoolExecutor max_workers formula."""
    return min(32, (os.cpu_count() or 1) + 4)


def _cpu_render_max_workers() -> int:
    return max(2, min(4, os.cpu_count() or 1))


def _default_pool_configs() -> dict[str, BlockingPoolConfig]:
    default_workers = default_thread_pool_max_workers()
    io_workers = max(4, min(16, default_workers))
    # Align terminal pools with Phase-1 live capacity (~200 concurrent PTY sessions).
    # TERMINAL_READ hosts long-lived Windows winpty readers; size must match live quota.
    terminal_read_workers = 200
    terminal_control_workers = 32
    terminal_workers = 32
    terminal_render_workers = max(4, min(8, _cpu_render_max_workers() * 2))
    return {
        BlockingPool.DEFAULT.value: BlockingPoolConfig(
            pool_name=BlockingPool.DEFAULT.value,
            max_workers=default_workers,
            thread_name_prefix="blocking-default",
            isolated=True,
        ),
        BlockingPool.COMMUNICATION.value: BlockingPoolConfig(
            pool_name=BlockingPool.COMMUNICATION.value,
            max_workers=max(4, min(8, default_workers)),
            thread_name_prefix="blocking-communication",
            isolated=True,
        ),
        BlockingPool.TERMINAL.value: BlockingPoolConfig(
            pool_name=BlockingPool.TERMINAL.value,
            max_workers=terminal_workers,
            thread_name_prefix="blocking-terminal",
            isolated=True,
            max_pending=terminal_workers * 4,
        ),
        BlockingPool.TERMINAL_READ.value: BlockingPoolConfig(
            pool_name=BlockingPool.TERMINAL_READ.value,
            max_workers=terminal_read_workers,
            thread_name_prefix="blocking-terminal-read",
            isolated=True,
            max_pending=terminal_read_workers * 2,
        ),
        BlockingPool.TERMINAL_CONTROL.value: BlockingPoolConfig(
            pool_name=BlockingPool.TERMINAL_CONTROL.value,
            max_workers=terminal_control_workers,
            thread_name_prefix="blocking-terminal-control",
            isolated=True,
            max_pending=terminal_control_workers * 4,
        ),
        BlockingPool.TERMINAL_RENDER.value: BlockingPoolConfig(
            pool_name=BlockingPool.TERMINAL_RENDER.value,
            max_workers=terminal_render_workers,
            thread_name_prefix="blocking-terminal-render",
            isolated=True,
            max_pending=terminal_render_workers * 4,
        ),
        BlockingPool.CPU_RENDER.value: BlockingPoolConfig(
            pool_name=BlockingPool.CPU_RENDER.value,
            max_workers=_cpu_render_max_workers(),
            thread_name_prefix="blocking-cpu-render",
            isolated=False,
        ),
        BlockingPool.FILE_RESOURCE.value: BlockingPoolConfig(
            pool_name=BlockingPool.FILE_RESOURCE.value,
            max_workers=io_workers,
            thread_name_prefix="blocking-file-resource",
            isolated=True,
            max_pending=io_workers * 4,
        ),
        BlockingPool.DB_BLOCKING.value: BlockingPoolConfig(
            pool_name=BlockingPool.DB_BLOCKING.value,
            max_workers=io_workers,
            thread_name_prefix="blocking-db",
            isolated=True,
            max_pending=io_workers * 4,
        ),
        BlockingPool.NETWORK.value: BlockingPoolConfig(
            pool_name=BlockingPool.NETWORK.value,
            max_workers=io_workers,
            thread_name_prefix="blocking-network",
            isolated=True,
            max_pending=io_workers * 4,
        ),
        BlockingPool.DIAGNOSTIC.value: BlockingPoolConfig(
            pool_name=BlockingPool.DIAGNOSTIC.value,
            max_workers=2,
            thread_name_prefix="blocking-diagnostic",
            isolated=True,
        ),
        BlockingPool.MAINTENANCE.value: BlockingPoolConfig(
            pool_name=BlockingPool.MAINTENANCE.value,
            max_workers=2,
            thread_name_prefix="blocking-maintenance",
            isolated=True,
        ),
    }


def _env_key_for(pool_name: str, suffix: str) -> str:
    normalized = pool_name.upper().replace("-", "_").replace(".", "_")
    return f"AGENT_RUNTIME_BLOCKING_POOL_{normalized}_{suffix}"


def _parse_env_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "isolated"}:
        return True
    if normalized in {"0", "false", "no", "off", "shared", "default"}:
        return False
    logger.warning("Invalid blocking pool boolean override ignored: value=%s", value)
    return default


def _apply_environment_overrides(configs: dict[str, BlockingPoolConfig]) -> dict[str, BlockingPoolConfig]:
    updated: dict[str, BlockingPoolConfig] = {}
    for pool_name, config in configs.items():
        max_workers = config.max_workers
        max_workers_text = os.environ.get(_env_key_for(pool_name, "MAX_WORKERS"))
        if max_workers_text is not None:
            try:
                max_workers = max(1, int(max_workers_text))
            except ValueError:
                logger.warning(
                    "Invalid blocking pool max worker override ignored: pool=%s value=%s",
                    pool_name,
                    max_workers_text,
                )
        max_pending = config.max_pending
        max_pending_text = os.environ.get(_env_key_for(pool_name, "MAX_PENDING"))
        if max_pending_text is not None:
            try:
                parsed_max_pending = int(max_pending_text)
                max_pending = None if parsed_max_pending < 0 else parsed_max_pending
            except ValueError:
                logger.warning(
                    "Invalid blocking pool max pending override ignored: pool=%s value=%s",
                    pool_name,
                    max_pending_text,
                )
        isolated = _parse_env_bool(
            os.environ.get(_env_key_for(pool_name, "ISOLATED")),
            default=config.isolated,
        )
        if pool_name == BlockingPool.DEFAULT.value:
            isolated = True
        updated[pool_name] = replace(config, max_workers=max_workers, isolated=isolated, max_pending=max_pending)
    return updated


def _duration_ms_since(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _current_task_name() -> str | None:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return None
    return task.get_name() if task is not None else None


def _normalize_pool_name(pool: BlockingPool | str | None) -> str:
    if pool is None:
        return BlockingPool.DEFAULT.value
    if isinstance(pool, BlockingPool):
        return pool.value
    value = str(pool).strip()
    return value or BlockingPool.DEFAULT.value


def _safe_blocking_id_part(value: str, *, max_length: int = 80) -> str:
    safe = "".join(char if char.isalnum() or char in {".", "_", "-"} else "-" for char in value.strip())
    safe = safe.strip("-._") or "unknown"
    return safe[:max_length]


def _new_blocking_call_id(pool_name: str, operation: str) -> str:
    return f"{_safe_blocking_id_part(pool_name, max_length=32)}:{_safe_blocking_id_part(operation)}:{uuid4().hex[:12]}"


def _sanitize_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value[:_MAX_CONTEXT_VALUE_LENGTH]
    if not parts.scheme or not parts.netloc:
        return value[:_MAX_CONTEXT_VALUE_LENGTH]
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    sanitized = urlunsplit((parts.scheme, host, parts.path, "", ""))
    return sanitized[:_MAX_CONTEXT_VALUE_LENGTH]


_SENSITIVE_CONTEXT_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "access_key",
    "secret",
    "password",
    "credential",
    "access_token",
    "refresh_token",
    "id_token",
    "bearer",
    "payload",
    "content",
    "base64",
    "command_line",
    "input_text",
)


def _sanitize_context_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(part in lowered for part in _SENSITIVE_CONTEXT_KEY_PARTS):
        return "[redacted]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if "url" in lowered:
            return _sanitize_url(value)
        if len(value) > _MAX_CONTEXT_VALUE_LENGTH:
            return f"{value[:_MAX_CONTEXT_VALUE_LENGTH]}...(truncated,len={len(value)})"
        return value
    if isinstance(value, Mapping):
        return {str(item_key): _sanitize_context_value(str(item_key), item_value) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        sanitized_items = [_sanitize_context_value(key, item) for item in items[:12]]
        if len(items) > 12:
            sanitized_items.append(f"...(truncated,len={len(items)})")
        return sanitized_items
    text = repr(value)
    if len(text) > _MAX_CONTEXT_VALUE_LENGTH:
        return f"{text[:_MAX_CONTEXT_VALUE_LENGTH]}...(truncated,len={len(text)})"
    return text


def _sanitize_context(context: dict[str, Any] | None) -> dict[str, Any]:
    return {str(key): _sanitize_context_value(str(key), value) for key, value in (context or {}).items()}


def _format_context(context: dict[str, Any]) -> str:
    if not context:
        return ""
    return " ".join(f"{key}={value}" for key, value in sorted(context.items()))


_SENSITIVE_ERROR_TEXT_MARKERS = (
    "authorization",
    "bearer ",
    "token",
    "api_key",
    "apikey",
    "secret",
    "password",
    "payload",
    "base64",
    "command_line",
    "access key",
    "refresh key",
)


def _sanitize_error_text(error: str | None) -> str | None:
    if error is None:
        return None
    text = str(error)
    lowered = text.lower()
    if any(marker in lowered for marker in _SENSITIVE_ERROR_TEXT_MARKERS):
        return "[redacted]"
    if len(text) > _MAX_CONTEXT_VALUE_LENGTH:
        return f"{text[:_MAX_CONTEXT_VALUE_LENGTH]}...(truncated,len={len(text)})"
    return text


def _executor_debug_snapshot(executor: ThreadPoolExecutor | None) -> dict[str, Any]:
    if executor is None:
        return {
            "executor_id": None,
            "max_workers": None,
            "work_queue_size": None,
            "thread_count": None,
            "alive_thread_count": None,
            "shutdown": None,
            "broken": None,
        }
    threads = getattr(executor, "_threads", ())
    try:
        thread_count = len(threads)
        alive_thread_count = sum(1 for thread in threads if thread.is_alive())
    except Exception:
        thread_count = None
        alive_thread_count = None
    work_queue = getattr(executor, "_work_queue", None)
    try:
        work_queue_size = work_queue.qsize() if work_queue is not None else None
    except Exception:
        work_queue_size = None
    broken = getattr(executor, "_broken", None)
    return {
        "executor_id": hex(id(executor)),
        "max_workers": getattr(executor, "_max_workers", None),
        "work_queue_size": work_queue_size,
        "thread_count": thread_count,
        "alive_thread_count": alive_thread_count,
        "shutdown": getattr(executor, "_shutdown", None),
        "broken": repr(broken) if broken else None,
    }


class ExecutorRegistry:
    """Registry of named executors with per-pool isolation switches."""

    def __init__(self, configs: dict[str, BlockingPoolConfig] | None = None) -> None:
        base_configs = configs or _apply_environment_overrides(_default_pool_configs())
        self._configs = dict(base_configs)
        self._executors: dict[str, ThreadPoolExecutor] = {}
        self._lock = threading.RLock()

    def configure_pool(
        self,
        pool: BlockingPool | str,
        *,
        max_workers: int | None = None,
        isolated: bool | None = None,
        thread_name_prefix: str | None = None,
        max_pending: int | None = None,
    ) -> BlockingPoolConfig:
        """Override a pool configuration and recreate the executor on next use."""
        pool_name = _normalize_pool_name(pool)
        with self._lock:
            current = self._config_for_locked(pool_name)
            next_config = replace(
                current,
                max_workers=max(1, max_workers if max_workers is not None else current.max_workers),
                isolated=current.isolated if isolated is None else bool(isolated),
                thread_name_prefix=thread_name_prefix or current.thread_name_prefix,
                max_pending=max_pending if max_pending is not None else current.max_pending,
            )
            if pool_name == BlockingPool.DEFAULT.value:
                next_config = replace(next_config, isolated=True)
            self._configs[pool_name] = next_config
            executor_pool_name = pool_name if next_config.isolated else BlockingPool.DEFAULT.value
            old_executor = self._executors.pop(executor_pool_name, None)
        if old_executor is not None:
            old_executor_snapshot = _executor_debug_snapshot(old_executor)
            logger.info(
                "Blocking pool reconfigured; retiring old executor: pool=%s executor_pool=%s old_executor_id=%s "
                "old_work_queue_size=%s old_thread_count=%s old_alive_thread_count=%s old_shutdown=%s old_broken=%s",
                pool_name,
                executor_pool_name,
                old_executor_snapshot["executor_id"],
                old_executor_snapshot["work_queue_size"],
                old_executor_snapshot["thread_count"],
                old_executor_snapshot["alive_thread_count"],
                old_executor_snapshot["shutdown"],
                old_executor_snapshot["broken"],
            )
            old_executor.shutdown(wait=False, cancel_futures=False)
        logger.info(
            "Blocking pool configured: pool=%s max_workers=%s max_pending=%s isolated=%s thread_name_prefix=%s "
            "executor_pool=%s old_executor_replaced=%s",
            pool_name,
            next_config.max_workers,
            next_config.max_pending,
            next_config.isolated,
            next_config.thread_name_prefix,
            executor_pool_name,
            old_executor is not None,
        )
        return next_config

    def get_executor(
        self,
        pool: BlockingPool | str | None,
    ) -> tuple[ThreadPoolExecutor, BlockingPoolConfig, str, BlockingPoolConfig]:
        pool_name = _normalize_pool_name(pool)
        with self._lock:
            logical_config = self._config_for_locked(pool_name)
            executor_pool_name = pool_name if logical_config.isolated else BlockingPool.DEFAULT.value
            executor_config = self._config_for_locked(executor_pool_name)
            executor = self._executors.get(executor_pool_name)
            if executor is None:
                executor = ThreadPoolExecutor(
                    max_workers=executor_config.max_workers,
                    thread_name_prefix=executor_config.thread_name_prefix,
                )
                self._executors[executor_pool_name] = executor
                executor_snapshot = _executor_debug_snapshot(executor)
                logger.info(
                    "Blocking executor created: pool=%s executor_pool=%s max_workers=%s isolated=%s thread_name_prefix=%s "
                    "executor_id=%s work_queue_size=%s thread_count=%s alive_thread_count=%s shutdown=%s broken=%s",
                    pool_name,
                    executor_pool_name,
                    executor_config.max_workers,
                    logical_config.isolated,
                    executor_config.thread_name_prefix,
                    executor_snapshot["executor_id"],
                    executor_snapshot["work_queue_size"],
                    executor_snapshot["thread_count"],
                    executor_snapshot["alive_thread_count"],
                    executor_snapshot["shutdown"],
                    executor_snapshot["broken"],
                )
            return executor, logical_config, executor_pool_name, executor_config

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot: dict[str, Any] = {}
            for pool_name, config in sorted(self._configs.items()):
                executor_pool = pool_name if config.isolated else BlockingPool.DEFAULT.value
                executor = self._executors.get(executor_pool)
                snapshot[pool_name] = {
                    "max_workers": config.max_workers,
                    "max_pending": config.max_pending,
                    "thread_name_prefix": config.thread_name_prefix,
                    "isolated": config.isolated,
                    "executor_pool": executor_pool,
                    "created": executor is not None,
                    "runtime": _executor_debug_snapshot(executor),
                }
            return snapshot

    def shutdown(self, *, wait: bool = False, cancel_futures: bool = False) -> None:
        with self._lock:
            executor_items = tuple(self._executors.items())
            self._executors.clear()
        logger.info(
            "Blocking executors shutdown requested: executor_count=%s wait=%s cancel_futures=%s executor_pools=%s",
            len(executor_items),
            wait,
            cancel_futures,
            [pool_name for pool_name, _executor in executor_items],
        )
        for executor_pool_name, executor in executor_items:
            before_snapshot = _executor_debug_snapshot(executor)
            logger.debug(
                "Blocking executor shutdown starting: executor_pool=%s executor_id=%s work_queue_size=%s "
                "thread_count=%s alive_thread_count=%s shutdown=%s broken=%s wait=%s cancel_futures=%s",
                executor_pool_name,
                before_snapshot["executor_id"],
                before_snapshot["work_queue_size"],
                before_snapshot["thread_count"],
                before_snapshot["alive_thread_count"],
                before_snapshot["shutdown"],
                before_snapshot["broken"],
                wait,
                cancel_futures,
            )
            executor.shutdown(wait=wait, cancel_futures=cancel_futures)
            after_snapshot = _executor_debug_snapshot(executor)
            logger.info(
                "Blocking executor shutdown completed: executor_pool=%s executor_id=%s work_queue_size=%s "
                "thread_count=%s alive_thread_count=%s shutdown=%s broken=%s",
                executor_pool_name,
                after_snapshot["executor_id"],
                after_snapshot["work_queue_size"],
                after_snapshot["thread_count"],
                after_snapshot["alive_thread_count"],
                after_snapshot["shutdown"],
                after_snapshot["broken"],
            )

    def _config_for_locked(self, pool_name: str) -> BlockingPoolConfig:
        config = self._configs.get(pool_name)
        if config is not None:
            return config
        dynamic = BlockingPoolConfig(
            pool_name=pool_name,
            max_workers=default_thread_pool_max_workers(),
            thread_name_prefix=f"blocking-{pool_name[:24]}",
            isolated=False,
            max_pending=None,
        )
        self._configs[pool_name] = dynamic
        logger.warning("Dynamic blocking pool created with shared default executor: pool=%s", pool_name)
        return dynamic


class BlockingScheduler:
    """Central blocking-call scheduler with pool and operation diagnostics."""

    def __init__(self, executor_registry: ExecutorRegistry | None = None) -> None:
        self._executor_registry = executor_registry or ExecutorRegistry()
        self._pool_stats: dict[str, _BlockingStats] = {}
        self._operation_stats: dict[tuple[str, str], _BlockingStats] = {}
        self._admission_waiters: dict[str, deque[_AdmissionWaiter]] = {}
        self._stats_lock = threading.RLock()

    @property
    def executor_registry(self) -> ExecutorRegistry:
        return self._executor_registry

    async def run(
        self,
        func: Callable[..., _R],
        /,
        *args: Any,
        operation: str,
        caller: str,
        pool: BlockingPool | str | None = BlockingPool.DEFAULT,
        blocking_call_id: str | None = None,
        log: logging.Logger | None = None,
        slow_queue_seconds: float = DEFAULT_TO_THREAD_SLOW_QUEUE_SECONDS,
        slow_execution_seconds: float = DEFAULT_TO_THREAD_SLOW_EXECUTION_SECONDS,
        diagnostic_context: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> _R:
        target_logger = log or logger
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()
        submitted_at = time.perf_counter()
        task_name = _current_task_name()
        pool_name = _normalize_pool_name(pool)
        actual_call_id = blocking_call_id or _new_blocking_call_id(pool_name, operation)
        executor, logical_config, executor_pool_name, executor_config = self._executor_registry.get_executor(pool_name)
        function_name = getattr(func, "__qualname__", getattr(func, "__name__", func.__class__.__name__))
        function_module = getattr(func, "__module__", None)
        queue_slow_threshold_ms = int(slow_queue_seconds * 1000)
        execution_slow_threshold_ms = int(slow_execution_seconds * 1000)
        context = _sanitize_context(diagnostic_context)
        admission = await self._admit_blocking_call(pool_name, operation, logical_config.max_pending)
        snapshot = admission.snapshot
        call = _SubmittedBlockingCall(
            blocking_call_id=actual_call_id,
            pool_name=pool_name,
            executor_pool_name=executor_pool_name,
            operation=operation,
            caller=caller,
            task_name=task_name,
            function_name=function_name,
            function_module=function_module,
            submitted_at=submitted_at,
            queue_slow_threshold_ms=queue_slow_threshold_ms,
            execution_slow_threshold_ms=execution_slow_threshold_ms,
            pool_max_workers=executor_config.max_workers,
            diagnostic_context=context,
            target_logger=target_logger,
            pool_snapshot=snapshot.pool,
            operation_snapshot=snapshot.operation,
        )
        self._log_blocking_diagnostic(logging.DEBUG, "blocking call submitted", call, phase="blocking.submitted")
        self._log_executor_state(logging.DEBUG, "blocking executor state before submit", call, executor, phase="blocking.executor_before_submit")

        state_lock = threading.Lock()
        state = {"worker_started": False, "await_cancelled": False, "cancelled_before_start_recorded": False}

        def _run() -> _R:
            worker_started_at = time.perf_counter()
            with state_lock:
                state["worker_started"] = True
            queue_delay_ms = int((worker_started_at - submitted_at) * 1000)
            worker_thread_name = threading.current_thread().name
            started_snapshot = self._record_worker_started(pool_name, operation)
            call.pool_snapshot = started_snapshot.pool
            call.operation_snapshot = started_snapshot.operation
            queue_level = logging.WARNING if queue_delay_ms >= queue_slow_threshold_ms else logging.DEBUG
            self._log_blocking_diagnostic(
                queue_level,
                "blocking worker started",
                call,
                phase="blocking.worker_started",
                executor_queue_delay_ms=queue_delay_ms,
                worker_thread_name=worker_thread_name,
            )
            self._log_executor_state(
                logging.DEBUG,
                "blocking executor state after worker start",
                call,
                executor,
                phase="blocking.executor_after_worker_start",
            )
            try:
                result = ctx.run(func, *args, **kwargs)
            except BaseException as exc:
                duration_ms = int((time.perf_counter() - worker_started_at) * 1000)
                failed_snapshot = self._record_worker_failed(pool_name, operation)
                call.pool_snapshot = failed_snapshot.pool
                call.operation_snapshot = failed_snapshot.operation
                self._log_blocking_diagnostic(
                    logging.ERROR,
                    "blocking worker failed",
                    call,
                    phase="blocking.worker_failed",
                    duration_ms=duration_ms,
                    executor_queue_delay_ms=queue_delay_ms,
                    worker_thread_name=worker_thread_name,
                    error_type=exc.__class__.__name__,
                    error=str(exc),
                )
                self._log_executor_state(
                    logging.ERROR,
                    "blocking executor state after worker failure",
                    call,
                    executor,
                    phase="blocking.executor_after_worker_failed",
                    error_type=exc.__class__.__name__,
                    error=str(exc),
                )
                raise
            duration_ms = int((time.perf_counter() - worker_started_at) * 1000)
            completed_snapshot = self._record_worker_completed(pool_name, operation)
            call.pool_snapshot = completed_snapshot.pool
            call.operation_snapshot = completed_snapshot.operation
            duration_level = logging.WARNING if duration_ms >= execution_slow_threshold_ms else logging.DEBUG
            self._log_blocking_diagnostic(
                duration_level,
                "blocking worker completed",
                call,
                phase="blocking.worker_completed",
                duration_ms=duration_ms,
                executor_queue_delay_ms=queue_delay_ms,
                worker_thread_name=worker_thread_name,
                final_state="success",
            )
            self._log_executor_state(
                logging.DEBUG,
                "blocking executor state after worker completion",
                call,
                executor,
                phase="blocking.executor_after_worker_completed",
            )
            return result

        try:
            concurrent_future = executor.submit(_run)
        except BaseException as exc:
            submit_failed_snapshot = self._record_submit_failed(pool_name, operation)
            call.pool_snapshot = submit_failed_snapshot.pool
            call.operation_snapshot = submit_failed_snapshot.operation
            self._log_executor_state(
                logging.ERROR,
                "blocking executor submit failed",
                call,
                executor,
                phase="blocking.submit_failed",
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
            raise
        self._log_executor_state(
            logging.DEBUG,
            "blocking executor state after submit",
            call,
            executor,
            phase="blocking.executor_after_submit",
        )
        asyncio_future = asyncio.wrap_future(concurrent_future, loop=loop)
        concurrent_future.add_done_callback(
            lambda done: self._on_concurrent_future_done(done, call, state, state_lock, executor)
        )

        try:
            result = await asyncio_future
        except asyncio.CancelledError:
            with state_lock:
                state["await_cancelled"] = True
            cancelled_snapshot = self._record_await_cancelled(pool_name, operation)
            call.pool_snapshot = cancelled_snapshot.pool
            call.operation_snapshot = cancelled_snapshot.operation
            self._log_blocking_diagnostic(
                logging.WARNING,
                "blocking await cancelled",
                call,
                phase="blocking.await_cancelled",
                duration_ms=_duration_ms_since(submitted_at),
                error_type="CancelledError",
            )
            self._log_executor_state(
                logging.WARNING,
                "blocking executor state after await cancellation",
                call,
                executor,
                phase="blocking.executor_after_await_cancelled",
                error_type="CancelledError",
            )
            raise
        except Exception as exc:
            self._log_blocking_diagnostic(
                logging.ERROR,
                "blocking await failed",
                call,
                phase="blocking.await_failed",
                duration_ms=_duration_ms_since(submitted_at),
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
            self._log_executor_state(
                logging.ERROR,
                "blocking executor state after await failure",
                call,
                executor,
                phase="blocking.executor_after_await_failed",
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
            raise
        self._log_blocking_diagnostic(
            logging.DEBUG,
            "blocking await completed",
            call,
            phase="blocking.await_completed",
            duration_ms=_duration_ms_since(submitted_at),
            final_state="success",
        )
        return result

    def health_snapshot(self) -> dict[str, Any]:
        executors = self._executor_registry.health_snapshot()
        with self._stats_lock:
            pools: dict[str, Any] = {}
            for pool_name, stats in sorted(self._pool_stats.items()):
                operations = {
                    operation: op_stats.to_dict()
                    for (item_pool, operation), op_stats in sorted(self._operation_stats.items())
                    if item_pool == pool_name
                }
                executor_config = executors.get(pool_name) if isinstance(executors.get(pool_name), dict) else {}
                pools[pool_name] = {
                    **stats.to_dict(),
                    "max_pending": executor_config.get("max_pending"),
                    "operations": operations,
                }
        return {
            "pools": pools,
            "executors": executors,
        }

    def configure_pool(
        self,
        pool: BlockingPool | str,
        *,
        max_workers: int | None = None,
        isolated: bool | None = None,
        thread_name_prefix: str | None = None,
        max_pending: int | None = None,
    ) -> BlockingPoolConfig:
        return self._executor_registry.configure_pool(
            pool,
            max_workers=max_workers,
            isolated=isolated,
            thread_name_prefix=thread_name_prefix,
            max_pending=max_pending,
        )

    def shutdown(self, *, wait: bool = False, cancel_futures: bool = False) -> None:
        self._executor_registry.shutdown(wait=wait, cancel_futures=cancel_futures)

    def _stats_for_locked(self, pool_name: str, operation: str) -> tuple[_BlockingStats, _BlockingStats]:
        pool_stats = self._pool_stats.setdefault(pool_name, _BlockingStats())
        op_stats = self._operation_stats.setdefault((pool_name, operation), _BlockingStats())
        return pool_stats, op_stats

    def _snapshot_locked(self, pool_name: str, operation: str) -> _StatsSnapshot:
        pool_stats, op_stats = self._stats_for_locked(pool_name, operation)
        return _StatsSnapshot(pool=pool_stats.to_dict(), operation=op_stats.to_dict())

    async def _admit_blocking_call(
        self,
        pool_name: str,
        operation: str,
        max_pending: int | None,
    ) -> _AdmissionResult:
        loop = asyncio.get_running_loop()
        with self._stats_lock:
            pool_stats, _op_stats = self._stats_for_locked(pool_name, operation)
            waiters = self._admission_waiters.get(pool_name)
            if not waiters and (max_pending is None or pool_stats.pending < max_pending):
                return self._record_admitted_locked(pool_name, operation, max_pending)
            waiter = _AdmissionWaiter(
                loop=loop,
                future=loop.create_future(),
                operation=operation,
                max_pending=max_pending,
            )
            self._admission_waiters.setdefault(pool_name, deque()).append(waiter)
            self._wake_admission_waiters_locked(pool_name)

        try:
            return await asyncio.shield(waiter.future)
        except asyncio.CancelledError:
            with self._stats_lock:
                waiters = self._admission_waiters.get(pool_name)
                if waiter.admission is None:
                    if waiters is not None:
                        try:
                            waiters.remove(waiter)
                        except ValueError:
                            pass
                        if not waiters:
                            self._admission_waiters.pop(pool_name, None)
                else:
                    self._release_admission_locked(pool_name, operation)
                    waiter.admission = None
                self._wake_admission_waiters_locked(pool_name)
            if not waiter.future.done():
                waiter.future.cancel()
            raise

    def _record_admitted_locked(
        self,
        pool_name: str,
        operation: str,
        max_pending: int | None,
    ) -> _AdmissionResult:
        pool_stats, op_stats = self._stats_for_locked(pool_name, operation)
        for stats in (pool_stats, op_stats):
            stats.submitted += 1
            stats.pending += 1
        return _AdmissionResult(
            accepted=True,
            snapshot=self._snapshot_locked(pool_name, operation),
            pending=pool_stats.pending,
            max_pending=max_pending,
        )

    def _release_admission_locked(self, pool_name: str, operation: str) -> None:
        pool_stats, op_stats = self._stats_for_locked(pool_name, operation)
        for stats in (pool_stats, op_stats):
            stats.pending = max(0, stats.pending - 1)

    def _wake_admission_waiters_locked(self, pool_name: str) -> None:
        waiters = self._admission_waiters.get(pool_name)
        if not waiters:
            return
        pool_stats = self._pool_stats.setdefault(pool_name, _BlockingStats())
        while waiters:
            waiter = waiters[0]
            if waiter.future.done():
                waiters.popleft()
                continue
            if waiter.max_pending is not None and pool_stats.pending >= waiter.max_pending:
                break
            waiters.popleft()
            admission = self._record_admitted_locked(pool_name, waiter.operation, waiter.max_pending)
            waiter.admission = admission
            try:
                waiter.loop.call_soon_threadsafe(
                    _complete_admission_waiter,
                    waiter.future,
                    admission,
                )
            except RuntimeError:
                waiter.admission = None
                self._release_admission_locked(pool_name, waiter.operation)
        if not waiters:
            self._admission_waiters.pop(pool_name, None)

    def _record_worker_started(self, pool_name: str, operation: str) -> _StatsSnapshot:
        with self._stats_lock:
            pool_stats, op_stats = self._stats_for_locked(pool_name, operation)
            for stats in (pool_stats, op_stats):
                stats.pending = max(0, stats.pending - 1)
                stats.active += 1
            self._wake_admission_waiters_locked(pool_name)
            return self._snapshot_locked(pool_name, operation)

    def _record_worker_completed(self, pool_name: str, operation: str) -> _StatsSnapshot:
        with self._stats_lock:
            pool_stats, op_stats = self._stats_for_locked(pool_name, operation)
            for stats in (pool_stats, op_stats):
                stats.active = max(0, stats.active - 1)
                stats.completed += 1
            return self._snapshot_locked(pool_name, operation)

    def _record_worker_failed(self, pool_name: str, operation: str) -> _StatsSnapshot:
        with self._stats_lock:
            pool_stats, op_stats = self._stats_for_locked(pool_name, operation)
            for stats in (pool_stats, op_stats):
                stats.active = max(0, stats.active - 1)
                stats.failed += 1
            return self._snapshot_locked(pool_name, operation)

    def _record_submit_failed(self, pool_name: str, operation: str) -> _StatsSnapshot:
        with self._stats_lock:
            pool_stats, op_stats = self._stats_for_locked(pool_name, operation)
            for stats in (pool_stats, op_stats):
                stats.pending = max(0, stats.pending - 1)
                stats.failed += 1
            self._wake_admission_waiters_locked(pool_name)
            return self._snapshot_locked(pool_name, operation)

    def _record_await_cancelled(self, pool_name: str, operation: str) -> _StatsSnapshot:
        with self._stats_lock:
            pool_stats, op_stats = self._stats_for_locked(pool_name, operation)
            for stats in (pool_stats, op_stats):
                stats.cancelled += 1
            return self._snapshot_locked(pool_name, operation)

    def _record_cancelled_before_start(self, pool_name: str, operation: str) -> _StatsSnapshot:
        with self._stats_lock:
            pool_stats, op_stats = self._stats_for_locked(pool_name, operation)
            for stats in (pool_stats, op_stats):
                stats.pending = max(0, stats.pending - 1)
            self._wake_admission_waiters_locked(pool_name)
            return self._snapshot_locked(pool_name, operation)

    def _on_concurrent_future_done(
        self,
        done: ConcurrentFuture[Any],
        call: _SubmittedBlockingCall,
        state: dict[str, bool],
        state_lock: threading.Lock,
        executor: ThreadPoolExecutor,
    ) -> None:
        cancelled_before_start = False
        with state_lock:
            worker_started = state["worker_started"]
            await_cancelled = state["await_cancelled"]
            if done.cancelled() and not worker_started and not state["cancelled_before_start_recorded"]:
                state["cancelled_before_start_recorded"] = True
                cancelled_before_start = True
        if cancelled_before_start:
            snapshot = self._record_cancelled_before_start(call.pool_name, call.operation)
            call.pool_snapshot = snapshot.pool
            call.operation_snapshot = snapshot.operation
            self._log_blocking_diagnostic(
                logging.WARNING,
                "blocking worker cancelled before start",
                call,
                phase="blocking.worker_cancelled_before_start",
                duration_ms=_duration_ms_since(call.submitted_at),
                final_state="cancelled_before_start",
            )
        if not await_cancelled:
            return
        final_state = "cancelled_before_start" if done.cancelled() else "success"
        error_type = None
        error = None
        if not done.cancelled():
            try:
                exc = done.exception()
            except BaseException as future_exc:
                exc = future_exc
            if exc is not None:
                final_state = "exception"
                error_type = exc.__class__.__name__
                error = str(exc)
        self._log_blocking_diagnostic(
            logging.WARNING,
            "blocking worker finished after await cancellation",
            call,
            phase="blocking.worker_finished_after_await_cancelled",
            duration_ms=_duration_ms_since(call.submitted_at),
            final_state=final_state,
            error_type=error_type,
            error=error,
        )
        self._log_executor_state(
            logging.DEBUG,
            "blocking executor state after await-cancelled worker finish",
            call,
            executor,
            phase="blocking.executor_after_await_cancelled_finish",
        )

    def _log_executor_state(
        self,
        level: int,
        message: str,
        call: _SubmittedBlockingCall,
        executor: ThreadPoolExecutor,
        *,
        phase: str,
        error_type: str | None = None,
        error: str | None = None,
    ) -> None:
        target_logger = call.target_logger
        if not target_logger.isEnabledFor(level):
            return
        executor_snapshot = _executor_debug_snapshot(executor)
        target_logger.log(
            level,
            "%s: blocking_call_id=%s pool=%s executor_pool=%s operation=%s caller=%s phase=%s "
            "executor_id=%s max_workers=%s work_queue_size=%s thread_count=%s alive_thread_count=%s "
            "shutdown=%s broken=%s pool_active=%s pool_pending=%s operation_active=%s operation_pending=%s "
            "error_type=%s error=%s context=%s",
            message,
            call.blocking_call_id,
            call.pool_name,
            call.executor_pool_name,
            call.operation,
            call.caller,
            phase,
            executor_snapshot["executor_id"],
            executor_snapshot["max_workers"],
            executor_snapshot["work_queue_size"],
            executor_snapshot["thread_count"],
            executor_snapshot["alive_thread_count"],
            executor_snapshot["shutdown"],
            executor_snapshot["broken"],
            call.pool_snapshot.get("active"),
            call.pool_snapshot.get("pending"),
            call.operation_snapshot.get("active"),
            call.operation_snapshot.get("pending"),
            error_type,
            _sanitize_error_text(error),
            _format_context(call.diagnostic_context),
        )

    def _log_blocking_diagnostic(
        self,
        level: int,
        message: str,
        call: _SubmittedBlockingCall,
        *,
        phase: str,
        duration_ms: int | None = None,
        executor_queue_delay_ms: int | None = None,
        worker_thread_name: str | None = None,
        final_state: str | None = None,
        error_type: str | None = None,
        error: str | None = None,
    ) -> None:
        target_logger = call.target_logger
        if not target_logger.isEnabledFor(level):
            return
        pool_snapshot = call.pool_snapshot
        operation_snapshot = call.operation_snapshot
        target_logger.log(
            level,
            "%s: blocking_call_id=%s pool=%s executor_pool=%s operation=%s caller=%s "
            "phase=%s task_name=%s duration_ms=%s executor_queue_delay_ms=%s "
            "worker_thread_name=%s pool_max_workers=%s function=%s.%s "
            "pool_active=%s pool_pending=%s pool_submitted=%s pool_completed=%s pool_failed=%s pool_cancelled=%s "
            "pool_rejected=%s pool_overloaded=%s operation_active=%s operation_pending=%s operation_submitted=%s "
            "operation_completed=%s operation_failed=%s operation_cancelled=%s operation_rejected=%s "
            "operation_overloaded=%s queue_slow_threshold_ms=%s execution_slow_threshold_ms=%s "
            "final_state=%s error_type=%s error=%s context=%s",
            message,
            call.blocking_call_id,
            call.pool_name,
            call.executor_pool_name,
            call.operation,
            call.caller,
            phase,
            call.task_name,
            duration_ms,
            executor_queue_delay_ms,
            worker_thread_name,
            call.pool_max_workers,
            call.function_module,
            call.function_name,
            pool_snapshot.get("active"),
            pool_snapshot.get("pending"),
            pool_snapshot.get("submitted"),
            pool_snapshot.get("completed"),
            pool_snapshot.get("failed"),
            pool_snapshot.get("cancelled"),
            pool_snapshot.get("rejected"),
            pool_snapshot.get("overloaded"),
            operation_snapshot.get("active"),
            operation_snapshot.get("pending"),
            operation_snapshot.get("submitted"),
            operation_snapshot.get("completed"),
            operation_snapshot.get("failed"),
            operation_snapshot.get("cancelled"),
            operation_snapshot.get("rejected"),
            operation_snapshot.get("overloaded"),
            call.queue_slow_threshold_ms,
            call.execution_slow_threshold_ms,
            final_state,
            error_type,
            _sanitize_error_text(error),
            _format_context(call.diagnostic_context),
        )


_blocking_scheduler = BlockingScheduler()


def get_blocking_scheduler() -> BlockingScheduler:
    return _blocking_scheduler


def blocking_scheduler_health_snapshot() -> dict[str, Any]:
    return _blocking_scheduler.health_snapshot()


def configure_blocking_pool(
    pool: BlockingPool | str,
    *,
    max_workers: int | None = None,
    isolated: bool | None = None,
    thread_name_prefix: str | None = None,
    max_pending: int | None = None,
) -> BlockingPoolConfig:
    return _blocking_scheduler.configure_pool(
        pool,
        max_workers=max_workers,
        isolated=isolated,
        thread_name_prefix=thread_name_prefix,
        max_pending=max_pending,
    )


def shutdown_blocking_executors(*, wait: bool = False, cancel_futures: bool = False) -> None:
    _blocking_scheduler.shutdown(wait=wait, cancel_futures=cancel_futures)


async def monitored_to_thread(
    func: Callable[..., _R],
    /,
    *args: Any,
    operation: str,
    caller: str,
    pool: BlockingPool | str | None = BlockingPool.DEFAULT,
    blocking_call_id: str | None = None,
    log: logging.Logger | None = None,
    slow_queue_seconds: float = DEFAULT_TO_THREAD_SLOW_QUEUE_SECONDS,
    slow_execution_seconds: float = DEFAULT_TO_THREAD_SLOW_EXECUTION_SECONDS,
    diagnostic_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> _R:
    """Run blocking work through the central BlockingScheduler.

    This keeps the previous low-intrusion call style while adding named pool routing,
    stable blocking_call_id values, pool/operation counters, and cancellation correlation.
    """
    return await _blocking_scheduler.run(
        func,
        *args,
        operation=operation,
        caller=caller,
        pool=pool,
        blocking_call_id=blocking_call_id,
        log=log,
        slow_queue_seconds=slow_queue_seconds,
        slow_execution_seconds=slow_execution_seconds,
        diagnostic_context=diagnostic_context,
        **kwargs,
    )

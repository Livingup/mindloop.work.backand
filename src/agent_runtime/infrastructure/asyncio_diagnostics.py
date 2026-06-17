from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

_R = TypeVar("_R")

logger = logging.getLogger(__name__)

DEFAULT_TO_THREAD_SLOW_QUEUE_SECONDS = 1.0
DEFAULT_TO_THREAD_SLOW_EXECUTION_SECONDS = 1.0


def default_thread_pool_max_workers() -> int:
    """Return CPython's default ThreadPoolExecutor max_workers used by asyncio.to_thread."""
    return min(32, (os.cpu_count() or 1) + 4)


def _duration_ms_since(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _current_task_name() -> str | None:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return None
    return task.get_name() if task is not None else None


def _format_context(context: dict[str, Any]) -> str:
    if not context:
        return ""
    return " ".join(f"{key}={value}" for key, value in sorted(context.items()))


def _log_to_thread_diagnostic(
    target_logger: logging.Logger,
    level: int,
    message: str,
    *,
    operation: str,
    caller: str,
    phase: str,
    task_name: str | None,
    duration_ms: int | None = None,
    executor_queue_delay_ms: int | None = None,
    worker_thread_name: str | None = None,
    error_type: str | None = None,
    error: str | None = None,
    context: dict[str, Any] | None = None,
    default_executor_max_workers: int | None = None,
    function_name: str | None = None,
    function_module: str | None = None,
    invocation_id: int | None = None,
    queue_slow_threshold_ms: int | None = None,
    execution_slow_threshold_ms: int | None = None,
) -> None:
    if not target_logger.isEnabledFor(level):
        return
    context_text = _format_context(context or {})
    target_logger.log(
        level,
        "%s: operation=%s caller=%s phase=%s task_name=%s duration_ms=%s "
        "executor_queue_delay_ms=%s worker_thread_name=%s default_executor_max_workers=%s "
        "function=%s.%s invocation_id=%s queue_slow_threshold_ms=%s execution_slow_threshold_ms=%s "
        "error_type=%s error=%s context=%s",
        message,
        operation,
        caller,
        phase,
        task_name,
        duration_ms,
        executor_queue_delay_ms,
        worker_thread_name,
        default_executor_max_workers,
        function_module,
        function_name,
        invocation_id,
        queue_slow_threshold_ms,
        execution_slow_threshold_ms,
        error_type,
        error,
        context_text,
    )


async def monitored_to_thread(
    func: Callable[..., _R],
    /,
    *args: Any,
    operation: str,
    caller: str,
    log: logging.Logger | None = None,
    slow_queue_seconds: float = DEFAULT_TO_THREAD_SLOW_QUEUE_SECONDS,
    slow_execution_seconds: float = DEFAULT_TO_THREAD_SLOW_EXECUTION_SECONDS,
    diagnostic_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> _R:
    """Run blocking work in the default executor with queue/runtime diagnostics.

    This intentionally mirrors asyncio.to_thread by using the event loop default executor
    and by copying contextvars into the worker. It only adds structured logs around the
    submission, worker start, worker completion/failure, await completion, and await
    cancellation boundaries so incidents can identify which caller occupied workers.
    """
    target_logger = log or logger
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    submitted_at = time.perf_counter()
    task_name = _current_task_name()
    invocation_id = id(ctx)
    context = dict(diagnostic_context or {})
    function_name = getattr(func, "__qualname__", getattr(func, "__name__", func.__class__.__name__))
    function_module = getattr(func, "__module__", None)
    max_workers = default_thread_pool_max_workers()
    queue_slow_threshold_ms = int(slow_queue_seconds * 1000)
    execution_slow_threshold_ms = int(slow_execution_seconds * 1000)

    _log_to_thread_diagnostic(
        target_logger,
        logging.DEBUG,
        "asyncio.to_thread task submitted",
        operation=operation,
        caller=caller,
        phase="to_thread.submitted",
        task_name=task_name,
        duration_ms=0,
        context=context,
        default_executor_max_workers=max_workers,
        function_name=function_name,
        function_module=function_module,
        invocation_id=invocation_id,
        queue_slow_threshold_ms=queue_slow_threshold_ms,
        execution_slow_threshold_ms=execution_slow_threshold_ms,
    )

    def _run() -> _R:
        worker_started_at = time.perf_counter()
        queue_delay_ms = int((worker_started_at - submitted_at) * 1000)
        worker_thread_name = threading.current_thread().name
        queue_level = logging.WARNING if queue_delay_ms >= queue_slow_threshold_ms else logging.DEBUG
        _log_to_thread_diagnostic(
            target_logger,
            queue_level,
            "asyncio.to_thread worker started",
            operation=operation,
            caller=caller,
            phase="to_thread.worker_started",
            task_name=task_name,
            duration_ms=0,
            executor_queue_delay_ms=queue_delay_ms,
            worker_thread_name=worker_thread_name,
            context=context,
            default_executor_max_workers=max_workers,
            function_name=function_name,
            function_module=function_module,
            invocation_id=invocation_id,
            queue_slow_threshold_ms=queue_slow_threshold_ms,
            execution_slow_threshold_ms=execution_slow_threshold_ms,
        )
        try:
            result = ctx.run(func, *args, **kwargs)
        except BaseException as exc:
            duration_ms = int((time.perf_counter() - worker_started_at) * 1000)
            level = logging.WARNING if isinstance(exc, asyncio.CancelledError) else logging.ERROR
            _log_to_thread_diagnostic(
                target_logger,
                level,
                "asyncio.to_thread worker failed",
                operation=operation,
                caller=caller,
                phase="to_thread.worker_failed",
                task_name=task_name,
                duration_ms=duration_ms,
                executor_queue_delay_ms=queue_delay_ms,
                worker_thread_name=worker_thread_name,
                error_type=exc.__class__.__name__,
                error=str(exc),
                context=context,
                default_executor_max_workers=max_workers,
                function_name=function_name,
                function_module=function_module,
                invocation_id=invocation_id,
                queue_slow_threshold_ms=queue_slow_threshold_ms,
                execution_slow_threshold_ms=execution_slow_threshold_ms,
            )
            raise
        duration_ms = int((time.perf_counter() - worker_started_at) * 1000)
        duration_level = logging.WARNING if duration_ms >= execution_slow_threshold_ms else logging.DEBUG
        _log_to_thread_diagnostic(
            target_logger,
            duration_level,
            "asyncio.to_thread worker completed",
            operation=operation,
            caller=caller,
            phase="to_thread.worker_completed",
            task_name=task_name,
            duration_ms=duration_ms,
            executor_queue_delay_ms=queue_delay_ms,
            worker_thread_name=worker_thread_name,
            context=context,
            default_executor_max_workers=max_workers,
            function_name=function_name,
            function_module=function_module,
            invocation_id=invocation_id,
            queue_slow_threshold_ms=queue_slow_threshold_ms,
            execution_slow_threshold_ms=execution_slow_threshold_ms,
        )
        return result

    future = loop.run_in_executor(None, _run)
    try:
        result = await future
    except asyncio.CancelledError:
        _log_to_thread_diagnostic(
            target_logger,
            logging.WARNING,
            "asyncio.to_thread await cancelled",
            operation=operation,
            caller=caller,
            phase="to_thread.await_cancelled",
            task_name=task_name,
            duration_ms=_duration_ms_since(submitted_at),
            context=context,
            default_executor_max_workers=max_workers,
            function_name=function_name,
            function_module=function_module,
            invocation_id=invocation_id,
            queue_slow_threshold_ms=queue_slow_threshold_ms,
            execution_slow_threshold_ms=execution_slow_threshold_ms,
            error_type="CancelledError",
        )
        raise
    except Exception as exc:
        _log_to_thread_diagnostic(
            target_logger,
            logging.ERROR,
            "asyncio.to_thread await failed",
            operation=operation,
            caller=caller,
            phase="to_thread.await_failed",
            task_name=task_name,
            duration_ms=_duration_ms_since(submitted_at),
            context=context,
            default_executor_max_workers=max_workers,
            function_name=function_name,
            function_module=function_module,
            invocation_id=invocation_id,
            queue_slow_threshold_ms=queue_slow_threshold_ms,
            execution_slow_threshold_ms=execution_slow_threshold_ms,
            error_type=exc.__class__.__name__,
            error=str(exc),
        )
        raise
    _log_to_thread_diagnostic(
        target_logger,
        logging.DEBUG,
        "asyncio.to_thread await completed",
        operation=operation,
        caller=caller,
        phase="to_thread.await_completed",
        task_name=task_name,
        duration_ms=_duration_ms_since(submitted_at),
        context=context,
        default_executor_max_workers=max_workers,
        function_name=function_name,
        function_module=function_module,
        invocation_id=invocation_id,
        queue_slow_threshold_ms=queue_slow_threshold_ms,
        execution_slow_threshold_ms=execution_slow_threshold_ms,
    )
    return result

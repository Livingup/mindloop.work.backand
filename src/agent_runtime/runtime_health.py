from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from agent_runtime.infrastructure.asyncio_diagnostics import blocking_scheduler_health_snapshot
from agent_runtime.infrastructure.runtime_task_registry import get_runtime_task_registry

logger = logging.getLogger(__name__)

RUNTIME_TASK_STALE_PROGRESS_SECONDS = 300.0
RUNTIME_TASK_STALE_SUMMARY_LIMIT = 20
RUNTIME_TASK_CANCELLING_SUMMARY_LIMIT = 20
RUNTIME_TASK_RECENT_TERMINAL_SUMMARY_LIMIT = 10
RUNTIME_CONCURRENCY_DEFAULT_TOP_LIMIT = 20
RUNTIME_CONCURRENCY_DETAIL_MAX_LIMIT = 100
RUNTIME_CONCURRENCY_RECENT_SECONDS = 300.0

_BLOCKING_COUNTER_KEYS = ("submitted", "pending", "active", "completed", "failed", "cancelled")
_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
    "access_key",
    "refresh_key",
    "cookie",
    "header",
    "payload",
    "body",
    "content",
    "base64",
    "command",
)


def _current_task_name() -> str | None:
    task = asyncio.current_task()
    return task.get_name() if task is not None else None


def runtime_task_health_summary(
    *,
    stale_progress_seconds: float = RUNTIME_TASK_STALE_PROGRESS_SECONDS,
    stale_limit: int = RUNTIME_TASK_STALE_SUMMARY_LIMIT,
    cancelling_limit: int = RUNTIME_TASK_CANCELLING_SUMMARY_LIMIT,
    recent_terminal_limit: int = RUNTIME_TASK_RECENT_TERMINAL_SUMMARY_LIMIT,
) -> dict[str, Any]:
    """Return a compact, sanitized RuntimeTaskRegistry diagnostic summary.

    The health endpoint must stay backwards-compatible and bounded, so this helper
    exposes aggregate counts and selected task identifiers/status fields only. It
    intentionally does not include raw diagnostic_context or raw error text.
    Stale-progress entries are diagnostic signals, not automatic health failures.
    """
    registry_snapshot = get_runtime_task_registry().snapshot(include_completed=True)
    tasks = list(registry_snapshot.get("tasks") or [])
    active_tasks = [task for task in tasks if not bool((task.get("health_snapshot") or {}).get("terminal"))]
    stale_tasks = [
        task
        for task in active_tasks
        if _seconds_since_progress(task) is not None
        and _seconds_since_progress(task) >= stale_progress_seconds
    ]
    cancelling_tasks = [task for task in active_tasks if task.get("status") == "cancelling"]
    terminal_tasks = [task for task in tasks if bool((task.get("health_snapshot") or {}).get("terminal"))]

    return {
        "total_count": int(registry_snapshot.get("total_count") or 0),
        "active_count": int(registry_snapshot.get("active_count") or 0),
        "by_status": dict(registry_snapshot.get("by_status") or {}),
        "active_by_owner": dict(registry_snapshot.get("active_by_owner") or {}),
        "active_by_class": dict(registry_snapshot.get("active_by_class") or {}),
        "stale_progress_seconds": stale_progress_seconds,
        "stale_progress_count": len(stale_tasks),
        "stale_progress_tasks": [
            _runtime_task_summary_item(task)
            for task in sorted(stale_tasks, key=lambda item: _seconds_since_progress(item) or 0, reverse=True)[:stale_limit]
        ],
        "cancelling_count": len(cancelling_tasks),
        "cancelling_tasks": [
            _runtime_task_summary_item(task)
            for task in sorted(cancelling_tasks, key=lambda item: item.get("cancel_requested_at") or "")[:cancelling_limit]
        ],
        "recent_terminal_tasks": [
            _runtime_task_summary_item(task)
            for task in sorted(terminal_tasks, key=lambda item: item.get("completed_at") or "", reverse=True)[:recent_terminal_limit]
        ],
    }


def _seconds_since_progress(task: dict[str, Any]) -> float | None:
    health_snapshot = task.get("health_snapshot")
    if not isinstance(health_snapshot, dict):
        return None
    value = health_snapshot.get("seconds_since_progress")
    return float(value) if isinstance(value, int | float) else None


def _runtime_task_summary_item(task: dict[str, Any]) -> dict[str, Any]:
    health_snapshot = task.get("health_snapshot") if isinstance(task.get("health_snapshot"), dict) else {}
    return {
        "task_id": task.get("task_id"),
        "task_name": task.get("task_name"),
        "owner_type": task.get("owner_type"),
        "owner_id": task.get("owner_id"),
        "task_class": task.get("task_class"),
        "status": task.get("status"),
        "started_at": task.get("started_at"),
        "last_progress_at": task.get("last_progress_at"),
        "completed_at": task.get("completed_at"),
        "cancel_requested_at": task.get("cancel_requested_at"),
        "stop_requested_at": task.get("stop_requested_at"),
        "runtime_seconds": health_snapshot.get("runtime_seconds"),
        "seconds_since_progress": health_snapshot.get("seconds_since_progress"),
        "last_error_type": task.get("last_error_type"),
        "done_callback_error_type": task.get("done_callback_error_type"),
    }


def runtime_concurrency_limit(
    value: int | None,
    *,
    default: int = RUNTIME_CONCURRENCY_DEFAULT_TOP_LIMIT,
    maximum: int = RUNTIME_CONCURRENCY_DETAIL_MAX_LIMIT,
    minimum: int = 1,
) -> int:
    """Return a bounded diagnostics limit for public runtime concurrency payloads."""
    if value is None:
        return default
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    if limit < minimum:
        return default
    return min(limit, maximum)


def runtime_concurrency_health_snapshot(
    *,
    event_loop_snapshot: dict[str, Any] | None = None,
    network_snapshot: dict[str, Any] | None = None,
    runtime_task_summary: dict[str, Any] | None = None,
    blocking_scheduler_snapshot: dict[str, Any] | None = None,
    top_limit: int = RUNTIME_CONCURRENCY_DEFAULT_TOP_LIMIT,
    operation_limit: int = RUNTIME_CONCURRENCY_DEFAULT_TOP_LIMIT,
    include_blocking_operations: bool = False,
) -> dict[str, Any]:
    """Return a bounded unified runtime concurrency diagnostic snapshot.

    This helper aggregates event-loop lag, Stage-2 blocking scheduler state,
    Stage-3/4 runtime task registry health, and a compact network summary. It
    is a diagnostic triage view only: the status is not a business-health
    verdict and does not prove a root cause for any incident.
    """
    top_limit = runtime_concurrency_limit(top_limit)
    operation_limit = runtime_concurrency_limit(operation_limit)
    if event_loop_snapshot is None:
        event_loop_snapshot = {}
    if network_snapshot is None:
        network_snapshot = {}
    if runtime_task_summary is None:
        runtime_task_summary = runtime_task_health_summary(
            stale_limit=top_limit,
            cancelling_limit=top_limit,
            recent_terminal_limit=min(top_limit, RUNTIME_TASK_RECENT_TERMINAL_SUMMARY_LIMIT),
        )
    if blocking_scheduler_snapshot is None:
        blocking_scheduler_snapshot = blocking_scheduler_health_snapshot()

    event_loop = _event_loop_concurrency_summary(event_loop_snapshot)
    blocking_scheduler = _blocking_scheduler_concurrency_summary(
        blocking_scheduler_snapshot,
        top_limit=top_limit,
        operation_limit=operation_limit,
        include_operations=include_blocking_operations,
    )
    runtime_tasks = _runtime_tasks_concurrency_summary(runtime_task_summary, top_limit=top_limit)
    network = _network_concurrency_summary(network_snapshot)
    signals = _runtime_concurrency_signals(
        event_loop=event_loop,
        blocking_scheduler=blocking_scheduler,
        runtime_tasks=runtime_tasks,
        network=network,
    )

    return {
        "status": _runtime_concurrency_status(signals),
        "diagnostic_scope": "runtime_concurrency_observability",
        "diagnostic_only": True,
        "signals": signals,
        "event_loop": event_loop,
        "blocking_scheduler": blocking_scheduler,
        "runtime_tasks": runtime_tasks,
        "network": network,
        "limits": {
            "top_limit": top_limit,
            "operation_limit": operation_limit if include_blocking_operations else 0,
            "include_blocking_operations": include_blocking_operations,
        },
    }


def _event_loop_concurrency_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    status = _safe_label(snapshot.get("status"), default="unknown")
    return {
        "status": status,
        "monitor_running": bool(snapshot.get("monitor_running")),
        "last_lag_ms": _int_value(snapshot.get("last_lag_ms")),
        "max_lag_ms": _int_value(snapshot.get("max_lag_ms")),
        "slow_tick_count": _int_value(snapshot.get("slow_tick_count")),
        "blocked_tick_count": _int_value(snapshot.get("blocked_tick_count")),
        "last_tick_at": _safe_timestamp(snapshot.get("last_tick_at")),
        "last_slow_tick_at": _safe_timestamp(snapshot.get("last_slow_tick_at")),
        "last_blocked_tick_at": _safe_timestamp(snapshot.get("last_blocked_tick_at")),
    }


def _blocking_scheduler_concurrency_summary(
    snapshot: dict[str, Any],
    *,
    top_limit: int,
    operation_limit: int,
    include_operations: bool,
) -> dict[str, Any]:
    pools = snapshot.get("pools") if isinstance(snapshot.get("pools"), dict) else {}
    executors = snapshot.get("executors") if isinstance(snapshot.get("executors"), dict) else {}
    pool_items = [_blocking_pool_summary(name, value) for name, value in pools.items() if isinstance(value, dict)]
    executor_items = [
        _blocking_executor_summary(name, value) for name, value in executors.items() if isinstance(value, dict)
    ]
    totals = {key: sum(_int_value(item.get(key)) for item in pool_items) for key in _BLOCKING_COUNTER_KEYS}
    totals.update(
        {
            "executor_work_queue_size": sum(_int_value(item.get("work_queue_size")) for item in executor_items),
            "executor_thread_count": sum(_int_value(item.get("thread_count")) for item in executor_items),
            "executor_alive_thread_count": sum(_int_value(item.get("alive_thread_count")) for item in executor_items),
            "executor_max_workers": sum(_int_value(item.get("max_workers")) for item in executor_items),
            "executor_shutdown_count": sum(1 for item in executor_items if bool(item.get("shutdown"))),
            "executor_broken_count": sum(1 for item in executor_items if bool(item.get("broken"))),
        }
    )
    operation_count = sum(
        len(value.get("operations") or {}) for value in pools.values() if isinstance(value, dict)
    )
    summary: dict[str, Any] = {
        "pool_count": len(pool_items),
        "executor_count": len(executor_items),
        "operation_count": operation_count,
        "totals": totals,
        "top_active_pools": _top_nonzero(pool_items, key_names=("active", "pending", "submitted"), limit=top_limit),
        "top_pending_pools": _top_nonzero(pool_items, key_names=("pending", "active", "submitted"), limit=top_limit),
        "top_failed_pools": _top_nonzero(pool_items, key_names=("failed", "cancelled", "submitted"), limit=top_limit),
        "top_queued_executors": _top_nonzero(
            executor_items, key_names=("work_queue_size", "alive_thread_count", "thread_count"), limit=top_limit
        ),
        "unavailable_executors": [
            item for item in executor_items if bool(item.get("shutdown")) or bool(item.get("broken"))
        ][:top_limit],
    }
    if include_operations:
        summary["top_operations"] = _blocking_operation_summaries(
            pools, limit=operation_limit
        )
    return summary


def _blocking_pool_summary(pool_name: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "pool": _safe_label(pool_name),
        **{key: _int_value(value.get(key)) for key in _BLOCKING_COUNTER_KEYS},
    }


def _blocking_executor_summary(executor_name: str, value: dict[str, Any]) -> dict[str, Any]:
    runtime = value.get("runtime") if isinstance(value.get("runtime"), dict) else {}
    return {
        "executor": _safe_label(executor_name),
        "executor_pool": _safe_label(value.get("executor_pool")),
        "isolated": bool(value.get("isolated")),
        "max_workers": _int_value(value.get("max_workers")),
        "work_queue_size": _int_value(runtime.get("work_queue_size")),
        "thread_count": _int_value(runtime.get("thread_count")),
        "alive_thread_count": _int_value(runtime.get("alive_thread_count")),
        "shutdown": bool(runtime.get("shutdown")),
        "broken": bool(runtime.get("broken")),
    }


def _blocking_operation_summaries(pools: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    operation_items: list[dict[str, Any]] = []
    for pool_name, pool_value in pools.items():
        if not isinstance(pool_value, dict):
            continue
        operations = pool_value.get("operations") if isinstance(pool_value.get("operations"), dict) else {}
        for operation_name, operation_value in operations.items():
            if not isinstance(operation_value, dict):
                continue
            operation_items.append(
                {
                    "pool": _safe_label(pool_name),
                    "operation": _safe_label(operation_name),
                    **{key: _int_value(operation_value.get(key)) for key in _BLOCKING_COUNTER_KEYS},
                }
            )
    return _top_nonzero(
        operation_items,
        key_names=("active", "pending", "failed", "cancelled", "submitted"),
        limit=limit,
    )


def _runtime_tasks_concurrency_summary(summary: dict[str, Any], *, top_limit: int) -> dict[str, Any]:
    recent_terminal_tasks = list(summary.get("recent_terminal_tasks") or [])[:top_limit]
    return {
        "total_count": _int_value(summary.get("total_count")),
        "active_count": _int_value(summary.get("active_count")),
        "by_status": _int_counter_map(summary.get("by_status")),
        "top_active_owners": _top_counter_items(summary.get("active_by_owner"), limit=top_limit),
        "top_active_classes": _top_counter_items(summary.get("active_by_class"), limit=top_limit),
        "stale_progress_seconds": summary.get("stale_progress_seconds"),
        "stale_progress_count": _int_value(summary.get("stale_progress_count")),
        "stale_progress_tasks": list(summary.get("stale_progress_tasks") or [])[:top_limit],
        "cancelling_count": _int_value(summary.get("cancelling_count")),
        "cancelling_tasks": list(summary.get("cancelling_tasks") or [])[:top_limit],
        "recent_terminal_tasks": recent_terminal_tasks,
        "recent_failure_count": _recent_runtime_task_failure_count(recent_terminal_tasks),
    }


def _network_concurrency_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in snapshot.items():
        raw_key = str(key)
        if _is_sensitive_key(raw_key):
            continue
        safe_key = _safe_label(raw_key)
        if isinstance(value, bool) or value is None:
            summary[safe_key] = value
        elif isinstance(value, int | float):
            summary[safe_key] = value
        elif isinstance(value, str):
            summary[safe_key] = _safe_scalar_string(value)
    return summary


def _runtime_concurrency_signals(
    *,
    event_loop: dict[str, Any],
    blocking_scheduler: dict[str, Any],
    runtime_tasks: dict[str, Any],
    network: dict[str, Any],
) -> dict[str, Any]:
    blocking_totals = blocking_scheduler.get("totals") if isinstance(blocking_scheduler.get("totals"), dict) else {}
    event_loop_status = str(event_loop.get("status") or "unknown")
    blocked_recently = event_loop_status == "blocked_recently" or _timestamp_recent(
        event_loop.get("last_blocked_tick_at")
    )
    slow_recently = event_loop_status in {"slow_recently", "blocked_recently"} or _timestamp_recent(
        event_loop.get("last_slow_tick_at")
    )
    blocking_active = _int_value(blocking_totals.get("active"))
    blocking_pending = _int_value(blocking_totals.get("pending"))
    executor_queue_size = _int_value(blocking_totals.get("executor_work_queue_size"))
    executor_max_workers = _int_value(blocking_totals.get("executor_max_workers"))
    return {
        "event_loop": {
            "monitor_running": bool(event_loop.get("monitor_running")),
            "lagging": slow_recently,
            "blocked_recently": blocked_recently,
        },
        "blocking_scheduler": {
            "active_operations": blocking_active,
            "pending_operations": blocking_pending,
            "executor_queue_size": executor_queue_size,
            "executor_unavailable": _int_value(blocking_totals.get("executor_shutdown_count")) > 0
            or _int_value(blocking_totals.get("executor_broken_count")) > 0,
            "saturated": executor_max_workers > 0
            and blocking_active >= executor_max_workers
            and (blocking_pending > 0 or executor_queue_size > 0),
            "recent_failures_or_cancellations": _int_value(blocking_totals.get("failed")) > 0
            or _int_value(blocking_totals.get("cancelled")) > 0,
        },
        "runtime_tasks": {
            "active_tasks": _int_value(runtime_tasks.get("active_count")),
            "stale_progress": _int_value(runtime_tasks.get("stale_progress_count")) > 0,
            "cancelling_tasks": _int_value(runtime_tasks.get("cancelling_count")) > 0,
            "recent_failures": _int_value(runtime_tasks.get("recent_failure_count")) > 0,
        },
        "network": {
            "active_http_requests": _int_value(network.get("active_http_requests")),
            "active_sse_connections": _int_value(network.get("active_sse_connections")),
            "event_hub_queue_pressure": _int_value(network.get("event_hub_queue_full_count")) > 0,
            "dropped_or_queued_logs": _int_value(network.get("dropped_log_records")) > 0
            or _int_value(network.get("queued_log_records")) > 0,
            "reported_failures": any(
                _int_value(value) > 0
                for key, value in network.items()
                if isinstance(key, str) and ("error" in key.lower() or "fail" in key.lower())
            ),
        },
    }


def _runtime_concurrency_status(signals: dict[str, Any]) -> str:
    event_loop = signals.get("event_loop") if isinstance(signals.get("event_loop"), dict) else {}
    blocking_scheduler = (
        signals.get("blocking_scheduler") if isinstance(signals.get("blocking_scheduler"), dict) else {}
    )
    runtime_tasks = signals.get("runtime_tasks") if isinstance(signals.get("runtime_tasks"), dict) else {}
    network = signals.get("network") if isinstance(signals.get("network"), dict) else {}
    if (
        bool(event_loop.get("blocked_recently"))
        or bool(blocking_scheduler.get("executor_unavailable"))
        or bool(blocking_scheduler.get("saturated"))
    ):
        return "degraded"
    if (
        not bool(event_loop.get("monitor_running"))
        or bool(event_loop.get("lagging"))
        or _int_value(blocking_scheduler.get("pending_operations")) > 0
        or _int_value(blocking_scheduler.get("executor_queue_size")) > 0
        or bool(blocking_scheduler.get("recent_failures_or_cancellations"))
        or bool(runtime_tasks.get("stale_progress"))
        or bool(runtime_tasks.get("cancelling_tasks"))
        or bool(runtime_tasks.get("recent_failures"))
        or bool(network.get("event_hub_queue_pressure"))
        or bool(network.get("dropped_or_queued_logs"))
        or bool(network.get("reported_failures"))
    ):
        return "attention"
    return "ok"


def _top_counter_items(value: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    items = [{"key": _safe_label(key), "count": _int_value(count)} for key, count in value.items()]
    return sorted(items, key=lambda item: (item["count"], item["key"]), reverse=True)[:limit]


def _int_counter_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {_safe_label(key): _int_value(count) for key, count in value.items()}


def _top_nonzero(
    items: list[dict[str, Any]],
    *,
    key_names: tuple[str, ...],
    limit: int,
) -> list[dict[str, Any]]:
    nonzero_items = [item for item in items if any(_int_value(item.get(key)) > 0 for key in key_names)]
    return sorted(
        nonzero_items,
        key=lambda item: tuple(_int_value(item.get(key)) for key in key_names),
        reverse=True,
    )[:limit]


def _recent_runtime_task_failure_count(tasks: list[Any]) -> int:
    count = 0
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "")
        last_error_type = task.get("last_error_type") or task.get("done_callback_error_type")
        if status == "failed" or (last_error_type is not None and last_error_type != "CancelledError"):
            count += 1
    return count


def _timestamp_recent(value: Any) -> bool:
    timestamp = _safe_timestamp(value)
    return timestamp is not None and time.time() - timestamp <= RUNTIME_CONCURRENCY_RECENT_SECONDS


def _safe_timestamp(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _int_value(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return int(value)
    return 0


def _safe_label(value: Any, *, default: str = "unknown") -> str:
    if value is None:
        return default
    text = str(value)[:160]
    return "[redacted]" if _is_sensitive_text(text) else text


def _safe_scalar_string(value: str) -> str:
    text = value[:160]
    if _is_sensitive_text(text):
        return "[redacted]"
    if "://" in text:
        try:
            parsed = urlsplit(text)
            hostname = parsed.hostname or ""
            netloc = hostname
            if parsed.port is not None:
                netloc = f"{netloc}:{parsed.port}"
            text = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        except ValueError:
            return "[redacted]"
    return text


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _is_sensitive_text(text: str) -> bool:
    lowered = text.lower()
    return any(fragment in lowered for fragment in ("authorization", "password", "secret", "token", "api_key"))


@dataclass(slots=True)
class EventLoopHealthSnapshot:
    status: str
    monitor_running: bool
    last_lag_ms: int
    max_lag_ms: int
    slow_tick_count: int
    blocked_tick_count: int
    last_tick_at: float | None
    last_slow_tick_at: float | None
    last_blocked_tick_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "monitor_running": self.monitor_running,
            "last_lag_ms": self.last_lag_ms,
            "max_lag_ms": self.max_lag_ms,
            "slow_tick_count": self.slow_tick_count,
            "blocked_tick_count": self.blocked_tick_count,
            "last_tick_at": self.last_tick_at,
            "last_slow_tick_at": self.last_slow_tick_at,
            "last_blocked_tick_at": self.last_blocked_tick_at,
        }


class EventLoopLagMonitor:
    def __init__(
        self,
        *,
        interval_seconds: float = 1.0,
        slow_threshold_seconds: float = 2.0,
        blocked_threshold_seconds: float = 10.0,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._slow_threshold_seconds = slow_threshold_seconds
        self._blocked_threshold_seconds = blocked_threshold_seconds
        self._task: asyncio.Task[None] | None = None
        self._last_lag_seconds = 0.0
        self._max_lag_seconds = 0.0
        self._slow_tick_count = 0
        self._blocked_tick_count = 0
        self._last_tick_at: float | None = None
        self._last_slow_tick_at: float | None = None
        self._last_blocked_tick_at: float | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = get_runtime_task_registry().create_task(
            self._run(),
            task_name="event-loop-lag-monitor",
            owner_type="runtime_health",
            owner_id="event_loop",
            task_class="lag_monitor",
            diagnostic_context={
                "interval_seconds": self._interval_seconds,
                "slow_threshold_ms": int(self._slow_threshold_seconds * 1000),
                "blocked_threshold_ms": int(self._blocked_threshold_seconds * 1000),
            },
        )
        logger.debug(
            "Event loop lag monitor diagnostic: trace_id=%s stream_id=%s event_type=%s service_id=%s "
            "operation=%s phase=%s task_name=%s duration_ms=%s timeout_seconds=%s "
            "interval_seconds=%s slow_threshold_ms=%s blocked_threshold_ms=%s",
            None,
            None,
            "event_loop_lag",
            None,
            "event_loop_lag_monitor",
            "monitor.start",
            _current_task_name(),
            0,
            None,
            self._interval_seconds,
            int(self._slow_threshold_seconds * 1000),
            int(self._blocked_threshold_seconds * 1000),
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        task = self._task
        self._task = None
        stop_started_at = time.perf_counter()
        get_runtime_task_registry().cancel(
            task,
            message="event loop lag monitor stop",
            diagnostic_context={"component": "event_loop_lag_monitor"},
        )
        with suppress(asyncio.CancelledError):
            await task
        logger.debug(
            "Event loop lag monitor diagnostic: trace_id=%s stream_id=%s event_type=%s service_id=%s "
            "operation=%s phase=%s task_name=%s duration_ms=%s timeout_seconds=%s",
            None,
            None,
            "event_loop_lag",
            None,
            "event_loop_lag_monitor",
            "monitor.stop",
            _current_task_name(),
            int((time.perf_counter() - stop_started_at) * 1000),
            None,
        )

    def snapshot(self) -> dict[str, Any]:
        monitor_running = self._task is not None and not self._task.done()
        status = "ok"
        if self._last_lag_seconds >= self._blocked_threshold_seconds:
            status = "blocked_recently"
        elif self._last_lag_seconds >= self._slow_threshold_seconds:
            status = "slow_recently"
        snapshot = EventLoopHealthSnapshot(
            status=status,
            monitor_running=monitor_running,
            last_lag_ms=int(self._last_lag_seconds * 1000),
            max_lag_ms=int(self._max_lag_seconds * 1000),
            slow_tick_count=self._slow_tick_count,
            blocked_tick_count=self._blocked_tick_count,
            last_tick_at=self._last_tick_at,
            last_slow_tick_at=self._last_slow_tick_at,
            last_blocked_tick_at=self._last_blocked_tick_at,
        )
        return snapshot.to_dict()

    async def _run(self) -> None:
        expected = time.perf_counter() + self._interval_seconds
        while True:
            await asyncio.sleep(self._interval_seconds)
            now = time.perf_counter()
            lag_seconds = max(0.0, now - expected)
            self._last_lag_seconds = lag_seconds
            self._max_lag_seconds = max(self._max_lag_seconds, lag_seconds)
            self._last_tick_at = time.time()
            get_runtime_task_registry().mark_progress(
                asyncio.current_task() or "event-loop-lag-monitor",
                diagnostic_context={
                    "last_lag_ms": int(lag_seconds * 1000),
                    "max_lag_ms": int(self._max_lag_seconds * 1000),
                    "slow_tick_count": self._slow_tick_count,
                    "blocked_tick_count": self._blocked_tick_count,
                },
            )
            if lag_seconds >= self._blocked_threshold_seconds:
                self._blocked_tick_count += 1
                self._last_blocked_tick_at = time.time()
                logger.error(
                    "Event loop lag diagnostic: trace_id=%s stream_id=%s event_type=%s service_id=%s "
                    "operation=%s phase=%s task_name=%s duration_ms=%s timeout_seconds=%s "
                    "slow_threshold_ms=%s blocked_threshold_ms=%s slow_tick_count=%s blocked_tick_count=%s",
                    None,
                    None,
                    "event_loop_lag",
                    None,
                    "event_loop_lag_monitor",
                    "event_loop_lag.blocked",
                    _current_task_name(),
                    int(lag_seconds * 1000),
                    None,
                    int(self._slow_threshold_seconds * 1000),
                    int(self._blocked_threshold_seconds * 1000),
                    self._slow_tick_count,
                    self._blocked_tick_count,
                )
            elif lag_seconds >= self._slow_threshold_seconds:
                self._slow_tick_count += 1
                self._last_slow_tick_at = time.time()
                logger.warning(
                    "Event loop lag diagnostic: trace_id=%s stream_id=%s event_type=%s service_id=%s "
                    "operation=%s phase=%s task_name=%s duration_ms=%s timeout_seconds=%s "
                    "slow_threshold_ms=%s blocked_threshold_ms=%s slow_tick_count=%s blocked_tick_count=%s",
                    None,
                    None,
                    "event_loop_lag",
                    None,
                    "event_loop_lag_monitor",
                    "event_loop_lag.slow",
                    _current_task_name(),
                    int(lag_seconds * 1000),
                    None,
                    int(self._slow_threshold_seconds * 1000),
                    int(self._blocked_threshold_seconds * 1000),
                    self._slow_tick_count,
                    self._blocked_tick_count,
                )
            expected = now + self._interval_seconds

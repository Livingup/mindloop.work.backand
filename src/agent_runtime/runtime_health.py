from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from agent_runtime.infrastructure.runtime_task_registry import get_runtime_task_registry

logger = logging.getLogger(__name__)

RUNTIME_TASK_STALE_PROGRESS_SECONDS = 300.0
RUNTIME_TASK_STALE_SUMMARY_LIMIT = 20
RUNTIME_TASK_CANCELLING_SUMMARY_LIMIT = 20
RUNTIME_TASK_RECENT_TERMINAL_SUMMARY_LIMIT = 10


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

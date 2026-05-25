from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


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
        self._task = asyncio.create_task(self._run(), name="event-loop-lag-monitor")

    async def stop(self) -> None:
        if self._task is None:
            return
        task = self._task
        self._task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

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
            if lag_seconds >= self._blocked_threshold_seconds:
                self._blocked_tick_count += 1
                self._last_blocked_tick_at = time.time()
                logger.error("Event loop lag blocked threshold exceeded: lag_ms=%s", int(lag_seconds * 1000))
            elif lag_seconds >= self._slow_threshold_seconds:
                self._slow_tick_count += 1
                self._last_slow_tick_at = time.time()
                logger.warning("Event loop lag slow threshold exceeded: lag_ms=%s", int(lag_seconds * 1000))
            expected = now + self._interval_seconds

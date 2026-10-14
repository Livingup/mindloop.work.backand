# -*- coding: utf-8 -*-
"""运行时认证入口的轻量级进程内限流器。"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
import time


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """一次限流检查的业务结果。"""

    allowed: bool
    retry_after_seconds: int = 0
    remaining_attempts: int = 0


class InMemoryFixedWindowRateLimiter:
    """按固定时间窗口限制同一业务 key 的访问次数。

    该限流器只服务 Runtime 本地认证入口的基础防护，状态保存在当前进程内；
    它不承担分布式配额、审计封禁或持久化账号锁定职责。
    """

    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts 必须大于 0。")
        if window_seconds <= 0:
            raise ValueError("window_seconds 必须大于 0。")
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._clock = clock or time.monotonic
        self._lock = RLock()
        self._windows: dict[str, tuple[int, float]] = {}

    def consume(self, key: str) -> RateLimitDecision:
        """记录一次访问尝试，并返回当前是否允许继续。"""
        normalized_key = key.strip() or "unknown"
        now = self._clock()
        with self._lock:
            self._discard_expired(now)
            count, reset_at = self._windows.get(normalized_key, (0, now + self._window_seconds))
            if now >= reset_at:
                count = 0
                reset_at = now + self._window_seconds
            if count >= self._max_attempts:
                return RateLimitDecision(
                    allowed=False,
                    retry_after_seconds=max(1, int(reset_at - now) + 1),
                    remaining_attempts=0,
                )
            next_count = count + 1
            self._windows[normalized_key] = (next_count, reset_at)
            return RateLimitDecision(
                allowed=True,
                retry_after_seconds=0,
                remaining_attempts=max(0, self._max_attempts - next_count),
            )

    def reset(self, key: str) -> None:
        """清除某个业务 key 的当前窗口。"""
        normalized_key = key.strip() or "unknown"
        with self._lock:
            self._windows.pop(normalized_key, None)

    def _discard_expired(self, now: float) -> None:
        expired_keys = [key for key, (_, reset_at) in self._windows.items() if now >= reset_at]
        for key in expired_keys:
            self._windows.pop(key, None)

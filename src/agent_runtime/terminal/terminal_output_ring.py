# -*- coding: utf-8 -*-
"""进程内 terminal 输出 tail ring（非 communication 权威日志）。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import RLock
from typing import Deque


@dataclass(frozen=True, slots=True)
class TerminalOutputRingEntry:
    terminal_id: str
    command_id: str
    chunk_index: int
    stream: str
    chunk: str
    char_len: int


class TerminalOutputRing:
    """按 terminal_id 保留最近输出 chunk，供 attach/reopen/tail 读取。"""

    def __init__(self, *, max_chunks: int = 1000, max_chars: int = 100_000) -> None:
        if max_chunks < 1:
            raise ValueError("max_chunks 必须大于 0。")
        if max_chars < 1:
            raise ValueError("max_chars 必须大于 0。")
        self._max_chunks = max_chunks
        self._max_chars = max_chars
        self._lock = RLock()
        self._entries: dict[str, Deque[TerminalOutputRingEntry]] = {}
        self._chars: dict[str, int] = {}

    def append(
        self,
        *,
        terminal_id: str,
        command_id: str,
        chunk_index: int,
        stream: str,
        chunk: str,
    ) -> None:
        if not terminal_id or not chunk:
            return
        entry = TerminalOutputRingEntry(
            terminal_id=terminal_id,
            command_id=command_id,
            chunk_index=chunk_index,
            stream=stream,
            chunk=chunk,
            char_len=len(chunk),
        )
        with self._lock:
            queue = self._entries.get(terminal_id)
            if queue is None:
                queue = deque()
                self._entries[terminal_id] = queue
                self._chars[terminal_id] = 0
            queue.append(entry)
            self._chars[terminal_id] = self._chars.get(terminal_id, 0) + entry.char_len
            self._evict_locked(terminal_id)

    def list_tail(
        self,
        terminal_id: str,
        *,
        max_chunks: int | None = None,
        max_chars: int | None = None,
    ) -> list[TerminalOutputRingEntry]:
        chunk_limit = max_chunks if max_chunks is not None else self._max_chunks
        char_limit = max_chars if max_chars is not None else self._max_chars
        with self._lock:
            queue = self._entries.get(terminal_id)
            if not queue:
                return []
            selected: list[TerminalOutputRingEntry] = []
            used_chars = 0
            for entry in reversed(queue):
                if len(selected) >= chunk_limit:
                    break
                if used_chars + entry.char_len > char_limit and selected:
                    break
                selected.append(entry)
                used_chars += entry.char_len
            selected.reverse()
            return selected

    def clear_terminal(self, terminal_id: str) -> None:
        with self._lock:
            self._entries.pop(terminal_id, None)
            self._chars.pop(terminal_id, None)

    def _evict_locked(self, terminal_id: str) -> None:
        queue = self._entries.get(terminal_id)
        if queue is None:
            return
        while len(queue) > self._max_chunks or self._chars.get(terminal_id, 0) > self._max_chars:
            if not queue:
                break
            old = queue.popleft()
            self._chars[terminal_id] = max(0, self._chars.get(terminal_id, 0) - old.char_len)
        if not queue:
            self._entries.pop(terminal_id, None)
            self._chars.pop(terminal_id, None)


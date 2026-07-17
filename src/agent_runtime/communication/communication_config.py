# -*- coding: utf-8 -*-
"""通用通信模块配置。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CommunicationStorageMode = Literal["memory", "sqlite"]


@dataclass(frozen=True, slots=True)
class CommunicationConfig:
    """通用通信模块基础设施配置。

    storage_mode=memory：热路径只写进程内 ring，适合本机单进程 Runtime。
    storage_mode=sqlite：保留 durable-first 回滚路径（Phase3 前）。
    """

    # 通信流和消息日志 SQLite 数据目录（sqlite 模式；memory 模式可忽略写入）。
    data_dir: str = "~/.spread-ai/communication"

    # 兼容旧字段：映射为 memory_ring_max_events 默认来源。
    replay_buffer_size: int = 4000

    subscriber_queue_size: int = 1000

    storage_mode: CommunicationStorageMode = "memory"

    memory_ring_max_events: int = 4000

    memory_ring_max_bytes: int = 24 * 1024 * 1024

    memory_ring_main_min_events: int = 1000

    memory_ring_terminal_max_events: int = 3000

    global_memory_ring_max_bytes: int = 2560 * 1024 * 1024

    stream_idle_reclaim_minutes: int = 30

    stream_idle_shrink_events: int = 64

    idle_scan_interval_seconds: int = 60

    def __post_init__(self) -> None:
        """校验通信模块配置合法性。"""
        if not isinstance(self.data_dir, str) or not self.data_dir.strip():
            raise ValueError("communication.data_dir 不能为空。")
        if self.storage_mode not in ("memory", "sqlite"):
            raise ValueError("communication.storage_mode 只能是 memory 或 sqlite。")
        if self.replay_buffer_size <= 0:
            raise ValueError("communication.replay_buffer_size 必须大于 0。")
        if self.subscriber_queue_size <= 0:
            raise ValueError("communication.subscriber_queue_size 必须大于 0。")
        if self.memory_ring_max_events <= 0:
            raise ValueError("communication.memory_ring_max_events 必须大于 0。")
        if self.memory_ring_max_bytes <= 0:
            raise ValueError("communication.memory_ring_max_bytes 必须大于 0。")
        if self.memory_ring_main_min_events < 0:
            raise ValueError("communication.memory_ring_main_min_events 不能为负数。")
        if self.memory_ring_terminal_max_events < 0:
            raise ValueError("communication.memory_ring_terminal_max_events 不能为负数。")
        if self.global_memory_ring_max_bytes < 64 * 1024 * 1024:
            raise ValueError("communication.global_memory_ring_max_bytes 不能小于 64MB。")
        if self.global_memory_ring_max_bytes > 4096 * 1024 * 1024:
            raise ValueError("communication.global_memory_ring_max_bytes 不能大于 4096MB。")
        if self.stream_idle_reclaim_minutes < 0:
            raise ValueError("communication.stream_idle_reclaim_minutes 不能为负数。")
        if self.stream_idle_shrink_events < 0:
            raise ValueError("communication.stream_idle_shrink_events 不能为负数。")
        if self.idle_scan_interval_seconds <= 0:
            raise ValueError("communication.idle_scan_interval_seconds 必须大于 0。")


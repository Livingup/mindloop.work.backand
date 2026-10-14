# -*- coding: utf-8 -*-
"""通用通信模块领域错误。"""
from __future__ import annotations


class CommunicationGapUnrecoverableError(Exception):
    """内存 ring 无法按 after_seq 连续补齐时抛出。"""

    def __init__(
        self,
        *,
        stream_id: str,
        after_seq: int,
        ring_first_seq: int,
        ring_last_seq: int,
    ) -> None:
        self.stream_id = stream_id
        self.after_seq = after_seq
        self.ring_first_seq = ring_first_seq
        self.ring_last_seq = ring_last_seq
        super().__init__(
            "communication_gap_unrecoverable: "
            f"stream_id={stream_id}, after_seq={after_seq}, "
            f"ring_first_seq={ring_first_seq}, ring_last_seq={ring_last_seq}"
        )

    def to_dict(self) -> dict[str, int | str]:
        return {
            "error_code": "communication_gap_unrecoverable",
            "stream_id": self.stream_id,
            "after_seq": self.after_seq,
            "ring_first_seq": self.ring_first_seq,
            "ring_last_seq": self.ring_last_seq,
        }


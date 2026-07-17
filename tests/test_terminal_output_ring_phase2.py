# -*- coding: utf-8 -*-
from __future__ import annotations

from agent_runtime.terminal.terminal_output_ring import TerminalOutputRing


def test_terminal_output_ring_evicts_oldest_by_chunks_and_chars() -> None:
    ring = TerminalOutputRing(max_chunks=3, max_chars=20)
    ring.append(terminal_id="t1", command_id="c1", chunk_index=0, stream="stdout", chunk="aaaa")  # 4
    ring.append(terminal_id="t1", command_id="c1", chunk_index=1, stream="stdout", chunk="bbbb")  # 4
    ring.append(terminal_id="t1", command_id="c1", chunk_index=2, stream="stdout", chunk="cccc")  # 4
    ring.append(terminal_id="t1", command_id="c1", chunk_index=3, stream="stdout", chunk="dddd")  # 4
    entries = ring.list_tail("t1")
    assert [item.chunk_index for item in entries] == [1, 2, 3]
    ring.append(terminal_id="t1", command_id="c1", chunk_index=4, stream="stdout", chunk="x" * 18)
    entries = ring.list_tail("t1")
    assert len(entries) >= 1
    assert entries[-1].chunk_index == 4
    assert sum(item.char_len for item in entries) <= 20


def test_terminal_output_ring_list_tail_respects_limits() -> None:
    ring = TerminalOutputRing(max_chunks=100, max_chars=1000)
    for index in range(10):
        ring.append(
            terminal_id="t2",
            command_id="c2",
            chunk_index=index,
            stream="stdout",
            chunk=f"line-{index}-" + ("z" * 10),
        )
    entries = ring.list_tail("t2", max_chunks=2, max_chars=40)
    assert len(entries) <= 2
    assert entries[-1].chunk_index == 9


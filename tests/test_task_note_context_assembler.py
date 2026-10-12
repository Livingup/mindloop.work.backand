# -*- coding: utf-8 -*-
"""TaskNoteContextAssembler 单元测试。"""
from __future__ import annotations

import pytest

from agent_runtime.task_note.task_note_context_assembler import (
    TaskNoteContextAssembler,
)
from agent_runtime.task_note.task_note_models import TaskNoteWatermark
from agent_runtime.task_note.task_note_unit_slicer import TaskNoteUnitSlicer


def test_assemble_context_no_swap_when_watermark_zero():
    assembler = TaskNoteContextAssembler()
    messages = [
        {"id": "m1", "role": "user", "content": "你好"},
        {"id": "m2", "role": "assistant", "content": "你好，有什么可以帮您？"},
    ]
    wm = TaskNoteWatermark(
        agent_instance_id="agent-01",
        task_id="task-01",
        covered_until_seq=0,
    )

    res = assembler.assemble_context(messages=messages, watermark=wm)
    assert res.swapped is False
    assert len(res.messages) == 2


def test_assemble_context_swaps_prefix_and_preserves_tail():
    assembler = TaskNoteContextAssembler()
    messages = [
        {"id": "m1", "role": "user", "content": "问题一：重构登录"},
        {"id": "m2", "role": "assistant", "content": "正在处理登录模块重构"},
        {"id": "m3", "role": "user", "content": "问题二：增加手机号验证码"},
        {"id": "m4", "role": "assistant", "content": "已添加手机号验证码逻辑"},
    ]
    # 水位覆盖到 seq=2 (即 m1 与 m2)
    wm = TaskNoteWatermark(
        agent_instance_id="agent-01",
        task_id="task-01",
        covered_until_seq=2,
        note_version=1,
    )
    fake_snapshot = {
        "missions": [{"title": "重构登录模块", "status": "completed", "outcome_summary": "登录已完成重构", "pruned_at": "2026-10-12"}],
        "progress": {"completed_summary": "完成登录重构", "next_step": "处理手机验证码"},
    }

    res = assembler.assemble_context(
        messages=messages,
        watermark=wm,
        note_snapshot=fake_snapshot,
    )

    assert res.swapped is True
    assert res.covered_until_seq == 2
    assert res.pruned_message_count == 2  # m1, m2
    assert res.tail_message_count == 2    # m3, m4

    # 结果包含：1条快照消息 + 2条残差消息 (m3, m4)
    assert len(res.messages) == 3
    assert res.messages[0]["is_snapshot"] is True
    assert "登录已完成重构" in res.messages[0]["content"]
    assert res.messages[1]["id"] == "m3"
    assert res.messages[2]["id"] == "m4"

    # 关键验证：将替换后的消息重新送入切片器，验证增量 Token 连续性（快照被完全排除）
    slicer = TaskNoteUnitSlicer()
    slice_res = slicer.slice_messages(res.messages, watermark_covered_until_seq=2)
    # Unit 1 是快照，Unit 2 是 m3，Unit 3 是 m4
    assert slice_res.all_units[0].is_snapshot is True
    # 增量只统计未被 watermark 覆盖且非 snapshot 的 Unit
    assert slice_res.incremental_tokens > 0
    # 增量 token 仅由 m3 和 m4 贡献，绝对不包含快照字符！
    assert slice_res.incremental_tokens < 100


def test_assemble_context_does_not_repeat_swap():
    assembler = TaskNoteContextAssembler()
    messages = [
        {"id": "m1", "role": "user", "content": "问题一"},
        {"id": "m2", "role": "assistant", "content": "回答一"},
    ]
    wm = TaskNoteWatermark(
        agent_instance_id="agent-01",
        task_id="task-01",
        covered_until_seq=2,
    )

    # 上次已替换到了 seq=2
    res = assembler.assemble_context(
        messages=messages,
        watermark=wm,
        last_swapped_seq=2,
    )
    assert res.swapped is False
    assert len(res.messages) == 2

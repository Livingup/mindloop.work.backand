# -*- coding: utf-8 -*-
"""TaskNoteUnitSlicer 单元测试。"""
from __future__ import annotations

import pytest

from agent_runtime.task_note.task_note_unit_slicer import (
    TaskNoteUnitSlicer,
    estimate_message_tokens,
)


def test_estimate_message_tokens():
    msg = {"role": "user", "content": "你好，请帮我分析代码"}
    tokens = estimate_message_tokens(msg)
    assert tokens > 0


def test_basic_dialogue_slicing_and_atomic_breakpoints():
    messages = [
        {"id": "m1", "role": "system", "content": "You are a helpful assistant."},
        {"id": "m2", "role": "user", "content": "修改 auth.py"},
        {
            "id": "m3",
            "role": "assistant",
            "content": "我正在读取文件",
            "tool_calls": [{"id": "call_1", "function": {"name": "file_read"}}],
        },
        {"id": "m4", "role": "tool", "tool_call_id": "call_1", "content": "file content..."},
        {"id": "m5", "role": "assistant", "content": "文件已经读取完毕，已为您完成修改。"},
    ]

    slicer = TaskNoteUnitSlicer(trigger_threshold_tokens=100)
    res = slicer.slice_messages(messages, watermark_covered_until_seq=0)

    # 预期划分：
    # Unit 1: system (m1)
    # Unit 2: user_turn (m2)
    # Unit 3: tool_interaction (m3 + m4)
    # Unit 4: direct_response (m5)
    assert len(res.all_units) == 4
    assert res.all_units[0].unit_type == "system"
    assert res.all_units[1].unit_type == "user_turn"
    assert res.all_units[2].unit_type == "tool_interaction"
    assert len(res.all_units[2].messages) == 2
    assert res.all_units[2].end_message_id == "m4"
    assert res.all_units[3].unit_type == "direct_response"
    assert res.all_units[3].end_message_id == "m5"
    assert res.max_seq == 4


def test_snapshot_exclusion_from_incremental_tokens():
    """验证带有 is_snapshot=True 的手记快照在增量 Token 计算中被完全排除。"""
    messages = [
        {"id": "m1", "role": "user", "content": "# 任务手记\n这里是 50000 字符的历史手记快照" * 100, "is_snapshot": True},
        {"id": "m2", "role": "user", "content": "这是后续新的用户指令，字数很少"},
        {"id": "m3", "role": "assistant", "content": "收到，我将继续处理。"},
    ]

    slicer = TaskNoteUnitSlicer(trigger_threshold_tokens=80000)
    res = slicer.slice_messages(messages, watermark_covered_until_seq=0)

    assert len(res.all_units) == 3
    assert res.all_units[0].is_snapshot is True
    assert res.all_units[1].is_snapshot is False
    assert res.all_units[2].is_snapshot is False

    # 快照 Unit 的 token 不计入 incremental_tokens
    assert res.incremental_tokens < 1000
    assert res.should_trigger is False


def test_threshold_trigger_at_80k_tokens():
    """验证当增量消息超过 80k Token 门槛时正确触发观察。"""
    large_content = "这是一大段用来模拟业务执行超过80k Token的大文本内容。" * 15000
    messages = [
        {"id": "m1", "role": "user", "content": "请分析大日志"},
        {"id": "m2", "role": "assistant", "content": large_content},
    ]

    slicer = TaskNoteUnitSlicer(trigger_threshold_tokens=80000)
    res = slicer.slice_messages(messages, watermark_covered_until_seq=0)

    assert res.incremental_tokens >= 80000
    assert res.should_trigger is True


def test_watermark_advancement_filters_covered_units():
    """验证当 watermark 推进到 seq=2 时，前两个 Unit 不再作为增量单元。"""
    messages = [
        {"id": "m1", "role": "user", "content": "第一问"},
        {"id": "m2", "role": "assistant", "content": "第一答"},
        {"id": "m3", "role": "user", "content": "第二问"},
        {"id": "m4", "role": "assistant", "content": "第二答"},
    ]

    slicer = TaskNoteUnitSlicer(trigger_threshold_tokens=10)
    res = slicer.slice_messages(messages, watermark_covered_until_seq=2)

    assert len(res.all_units) == 4
    assert len(res.incremental_units) == 2
    assert res.incremental_units[0].seq == 3
    assert res.incremental_units[1].seq == 4


def test_subagent_inherited_units_extraction():
    """验证子 Agent 首次观察通过 fork_message_id 正确识别继承段。"""
    messages = [
        {"id": "m1", "role": "user", "content": "父会话初始化"},
        {"id": "m2", "role": "assistant", "content": "父会话步骤一"},
        {"id": "m_fork", "role": "user", "content": "委派子任务边界"},
        {"id": "m4", "role": "assistant", "content": "子任务开始工作"},
    ]

    slicer = TaskNoteUnitSlicer(trigger_threshold_tokens=100)
    res = slicer.slice_messages(
        messages,
        watermark_covered_until_seq=0,
        is_subagent=True,
        inherited_processed=False,
        fork_message_id="m_fork",
    )

    assert len(res.inherited_units) == 3
    assert res.inherited_units[-1].end_message_id == "m_fork"

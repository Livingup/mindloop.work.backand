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
    res = slicer.slice_messages(messages)

    # 预期划分：
    # Unit 1: system (m1)
    # Unit 2: user_turn (m2)
    # Unit 3: tool_interaction (m3 + m4)
    # Unit 4: direct_response (m5)
    assert len(res.incremental_units) == 4
    assert res.incremental_units[0].unit_type == "system"
    assert res.incremental_units[1].unit_type == "user_turn"
    assert res.incremental_units[2].unit_type == "tool_interaction"
    assert len(res.incremental_units[2].messages) == 2
    assert res.incremental_units[2].end_message_id == "m4"
    assert res.incremental_units[3].unit_type == "direct_response"
    assert res.incremental_units[3].end_message_id == "m5"
    assert res.max_seq == 4

    # 验证 end_message_id 各不相同
    end_ids = [u.end_message_id for u in res.incremental_units]
    assert len(end_ids) == len(set(end_ids))


def test_snapshot_exclusion_from_incremental_tokens():
    """验证带有 is_snapshot=True 的手记快照在增量 Token 计算中被完全排除。"""
    messages = [
        {"id": "m1", "role": "user", "content": "# 任务手记\n这里是 50000 字符的历史手记快照" * 100, "is_snapshot": True},
        {"id": "m2", "role": "user", "content": "这是后续新的用户指令，字数很少"},
        {"id": "m3", "role": "assistant", "content": "收到，我将继续处理。"},
    ]

    slicer = TaskNoteUnitSlicer(trigger_threshold_tokens=80000)
    res = slicer.slice_messages(messages)

    assert len(res.incremental_units) == 3
    assert res.incremental_units[0].is_snapshot is True
    assert res.incremental_units[1].is_snapshot is False
    assert res.incremental_units[2].is_snapshot is False

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
    res = slicer.slice_messages(messages)

    assert res.incremental_tokens >= 80000
    assert res.should_trigger is True


def test_first_swap_incremental_slicing_with_snapshot_merged_and_unmerged():
    """验证替换后再切片：覆盖未合并与合并两种情况。"""
    slicer = TaskNoteUnitSlicer(trigger_threshold_tokens=50)

    # 1. 未合并情况：快照独立，content 只有 1 项
    unmerged_messages = [
        {"id": "snap_1", "role": "user", "content": [{"text": "# 任务手记快照 (v1)\n- 进展..."}]},
        {"id": "m2", "role": "assistant", "content": "请提供下一步指令"},
    ]
    res_unmerged = slicer.slice_messages(
        unmerged_messages,
        start_after_message_id="snap_1",
        snapshot_message_id="snap_1",
    )
    # snap_1 只有 1 项 content，跳过，增量仅含 m2
    assert len(res_unmerged.incremental_units) == 1
    assert res_unmerged.incremental_units[0].end_message_id == "m2"
    assert res_unmerged.incremental_units[0].unit_type == "direct_response"

    # 2. 合并情况：快照与用户首条消息合并，content 有多项
    merged_messages = [
        {
            "id": "msg_user_1",
            "role": "user",
            "content": [
                {"text": "# 任务手记快照 (v1)\n- 进展..."},
                {"text": "这是合并的用户新指令，请帮我处理"},
            ],
        },
        {"id": "m3", "role": "assistant", "content": "收到，正在处理"},
    ]
    res_merged = slicer.slice_messages(
        merged_messages,
        start_after_message_id="msg_user_1",
        snapshot_message_id="msg_user_1",
    )
    # 期望包含 2 个 unit：
    # 第 1 个由合并的 msg_user_1 生成 user_turn，仅保留 content[1:]
    # 第 2 个是 m3 的 direct_response
    assert len(res_merged.incremental_units) == 2
    u1 = res_merged.incremental_units[0]
    assert u1.unit_type == "user_turn"
    assert u1.end_message_id == "msg_user_1"
    assert u1.is_snapshot is False
    assert len(u1.messages) == 1
    # 检查 content 已经剥离了快照，只保留 content[1:]
    msg_view = u1.messages[0]
    content = msg_view.get("content") if isinstance(msg_view, dict) else msg_view.content
    assert len(content) == 1
    assert "这是合并的用户新指令" in str(content[0])

    u2 = res_merged.incremental_units[1]
    assert u2.unit_type == "direct_response"
    assert u2.end_message_id == "m3"


def test_subagent_inherited_units_extraction():
    """验证子 Agent 首次切片时，inherited_units 截止到边界消息，seq 从 1 编号且不计入增量 token。"""
    messages = [
        {"id": "m1", "role": "user", "content": "父会话初始化"},
        {"id": "m2", "role": "assistant", "content": "父会话步骤一"},
        {"id": "m_fork", "role": "user", "content": "委派子任务边界"},
        {"id": "m4", "role": "assistant", "content": "子任务开始工作"},
    ]

    slicer = TaskNoteUnitSlicer(trigger_threshold_tokens=100)
    res = slicer.slice_messages(
        messages,
        start_after_message_id="m_fork",
        inherited_until_message_id="m_fork",
    )

    # inherited_units 截止到 m_fork
    assert len(res.inherited_units) == 3
    assert res.inherited_units[0].seq == 1
    assert res.inherited_units[-1].end_message_id == "m_fork"

    # incremental_units 严格从 m_fork 之后开始
    assert len(res.incremental_units) == 1
    assert res.incremental_units[0].seq == 1
    assert res.incremental_units[0].end_message_id == "m4"

    # 增量 token 不包含 inherited_units
    assert res.incremental_tokens == res.incremental_units[0].token_count

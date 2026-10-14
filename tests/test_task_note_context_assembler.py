# -*- coding: utf-8 -*-
"""TaskNoteContextAssembler 单元测试。"""
from __future__ import annotations

import pytest

from agent_runtime.conversation_message.message.models import (
    ROLE_ASSISTANT,
    ROLE_USER,
    TextPart,
    UniMessage,
)
from agent_runtime.task_note.task_note_context_assembler import (
    TaskNoteContextAssembler,
)
from agent_runtime.task_note.task_note_models import TaskNoteTracking
from agent_runtime.task_note.task_note_unit_slicer import TaskNoteUnitSlicer


@pytest.mark.asyncio
async def test_assemble_context_no_swap_when_note_version_zero():
    """当 tracking.note_version < 1 时，不执行替换。"""
    assembler = TaskNoteContextAssembler()
    messages = [
        UniMessage(role=ROLE_USER, content=[TextPart("你好")], message_id="m1"),
        UniMessage(role=ROLE_ASSISTANT, content=[TextPart("你好，有什么可以帮您？")], message_id="m2"),
    ]
    tracking = TaskNoteTracking(
        version=1,
        note_version=0,
        covered_until_message_id="m2",
    )

    res = await assembler.assemble_context(messages=messages, tracking=tracking)
    assert res.swapped is False
    assert len(res.messages) == 2


@pytest.mark.asyncio
async def test_assemble_context_swaps_and_merges_user_tail():
    """尾部首条是 user 时，合并到该 user 消息，保留原内容且沿用原 message_id。"""
    assembler = TaskNoteContextAssembler()
    messages = [
        UniMessage(role=ROLE_USER, content=[TextPart("问题一：重构登录")], message_id="m1"),
        UniMessage(role=ROLE_ASSISTANT, content=[TextPart("正在处理登录模块重构")], message_id="m2"),
        UniMessage(role=ROLE_USER, content=[TextPart("确认该方案，继续增加手机号验证码")], message_id="m3"),
    ]
    tracking = TaskNoteTracking(
        version=1,
        note_version=1,
        covered_until_message_id="m2",
    )
    fake_snapshot = {
        "missions": [
            {
                "title": "重构登录模块",
                "status": "completed",
                "outcome_summary": "登录已完成重构",
                "pruned_at": "2026-10-12",
            }
        ],
        "progress": {"completed_summary": "完成登录重构", "next_step": "处理手机验证码"},
    }

    res = await assembler.assemble_context(
        messages=messages,
        tracking=tracking,
        note_snapshot=fake_snapshot,
    )

    assert res.swapped is True
    assert res.note_version == 1
    assert res.tail_message_count == 1  # 尾部是 m3
    assert res.snapshot_message_id == "m3"  # 沿用原 user 消息 ID

    # 合并后只有 1 条消息（合并了手记的 m3）
    assert len(res.messages) == 1
    merged_msg = res.messages[0]
    assert merged_msg.message_id == "m3"
    assert len(merged_msg.content) == 2  # [TextPart(手记), TextPart(原内容)]
    assert "# 任务手记快照 (v1)" in merged_msg.content[0].text
    assert "登录已完成重构" in merged_msg.content[0].text
    assert merged_msg.content[1].text == "确认该方案，继续增加手机号验证码"


@pytest.mark.asyncio
async def test_assemble_context_swaps_and_creates_snapshot_message_when_tail_is_assistant():
    """尾部首条不是 user 时，新建独立快照消息放在头部。"""
    assembler = TaskNoteContextAssembler()
    messages = [
        UniMessage(role=ROLE_USER, content=[TextPart("问题一：重构登录")], message_id="m1"),
        UniMessage(role=ROLE_ASSISTANT, content=[TextPart("正在处理登录模块重构")], message_id="m2"),
        UniMessage(role=ROLE_ASSISTANT, content=[TextPart("继续输出模块设计方案")], message_id="m3"),
    ]
    tracking = TaskNoteTracking(
        version=1,
        note_version=1,
        covered_until_message_id="m2",
    )
    fake_snapshot = {
        "missions": [{"title": "重构登录模块", "status": "completed", "outcome_summary": "登录已完成重构"}],
        "progress": {"completed_summary": "完成登录重构", "next_step": "继续输出模块设计方案"},
    }

    res = await assembler.assemble_context(
        messages=messages,
        tracking=tracking,
        note_snapshot=fake_snapshot,
    )

    assert res.swapped is True
    assert res.note_version == 1
    assert res.tail_message_count == 1  # m3
    assert res.snapshot_message_id is not None
    assert res.snapshot_message_id.startswith("msg_snapshot_v1_")

    assert len(res.messages) == 2  # 快照消息 + m3
    assert res.messages[0].message_id == res.snapshot_message_id
    assert "# 任务手记快照 (v1)" in res.messages[0].content[0].text
    assert res.messages[1].message_id == "m3"


@pytest.mark.asyncio
async def test_assemble_context_consecutive_swaps():
    """连续两次替换，第二次的尾部恰好是第一次替换之后新增、且未被覆盖的消息。"""
    assembler = TaskNoteContextAssembler()
    # 第一次对话：m1 (user), m2 (assistant), m3 (assistant)
    m1 = UniMessage(role=ROLE_USER, content=[TextPart("任务一")], message_id="m1")
    m2 = UniMessage(role=ROLE_ASSISTANT, content=[TextPart("结果一")], message_id="m2")
    m3 = UniMessage(role=ROLE_ASSISTANT, content=[TextPart("结果一继续")], message_id="m3")
    messages1 = [m1, m2, m3]

    # 第一次替换：覆盖到 m2
    tracking1 = TaskNoteTracking(
        version=1,
        note_version=1,
        covered_until_message_id="m2",
    )
    fake_snapshot1 = {"missions": [{"title": "任务一", "status": "completed"}]}
    res1 = await assembler.assemble_context(
        messages=messages1,
        tracking=tracking1,
        note_snapshot=fake_snapshot1,
    )
    assert res1.swapped is True
    snap1_id = res1.snapshot_message_id
    assert len(res1.messages) == 2  # [snap1, m3]

    # 业务继续推进，产生了 m4 (user), m5 (assistant), m6 (assistant)
    snap1_msg = res1.messages[0]
    m4 = UniMessage(role=ROLE_USER, content=[TextPart("任务二")], message_id="m4")
    m5 = UniMessage(role=ROLE_ASSISTANT, content=[TextPart("结果二")], message_id="m5")
    m6 = UniMessage(role=ROLE_ASSISTANT, content=[TextPart("结果二残差")], message_id="m6")
    messages2 = [snap1_msg, m3, m4, m5, m6]

    # 第二次观察推进覆盖到了 m5
    tracking2 = TaskNoteTracking(
        version=2,
        note_version=2,
        covered_until_message_id="m5",
        snapshot_message_id=snap1_id,
        prefix_end_message_id=snap1_id,
    )
    fake_snapshot2 = {"missions": [{"title": "任务一与二", "status": "completed"}]}
    res2 = await assembler.assemble_context(
        messages=messages2,
        tracking=tracking2,
        note_snapshot=fake_snapshot2,
    )
    assert res2.swapped is True
    assert res2.note_version == 2
    # 第二次的尾部恰好是 m5 之后的 m6
    assert res2.tail_message_count == 1
    assert len(res2.messages) == 2  # [snap2, m6]
    assert res2.messages[1].message_id == "m6"

# -*- coding: utf-8 -*-
"""task_note_range 模块单元测试。"""
from __future__ import annotations

import pytest

from agent_runtime.conversation_message.message.models import (
    ROLE_USER,
    ROLE_ASSISTANT,
    TextPart,
    UniMessage,
)
from agent_runtime.task_note.task_note_range import (
    RangeStart,
    build_merged_head,
    estimate_range_tokens,
    get_msg_id,
    is_snapshot_header,
    range_messages,
    resolve_range_start,
)


def test_is_snapshot_header_and_get_msg_id():
    m1 = UniMessage(role=ROLE_USER, content=[TextPart("# 任务手记快照\n任务进展...")], message_id="m1")
    m2 = UniMessage(role=ROLE_USER, content=[TextPart("普通用户消息")], message_id="m2")
    m_dict = {"message_id": "m3", "role": "user", "content": [{"text": "# 任务手记快照\n..."}]}

    assert is_snapshot_header(m1) is True
    assert is_snapshot_header(m2) is False
    assert is_snapshot_header(m_dict) is True
    assert get_msg_id(m1) == "m1"
    assert get_msg_id(m_dict) == "m3"
    assert get_msg_id(None) is None


def test_build_merged_head():
    # 纯快照：只有 1 个 part
    m_single = UniMessage(role=ROLE_USER, content=[TextPart("# 任务手记快照\n...")], message_id="snap1")
    assert build_merged_head(m_single) is None

    # 合并快照：快照 + 用户原始输入
    m_merged = UniMessage(
        role=ROLE_USER,
        content=[TextPart("# 任务手记快照\n..."), TextPart("帮我写代码")],
        message_id="merged1",
    )
    head = build_merged_head(m_merged)
    assert head is not None
    assert head.message_id == "merged1"
    assert head.role == ROLE_USER
    assert len(head.content) == 1
    assert head.content[0].text == "帮我写代码"


def test_resolve_range_start_and_estimate_range_tokens():
    m1 = UniMessage(role=ROLE_USER, content=[TextPart("第一轮需求")], message_id="m1")
    m2 = UniMessage(role=ROLE_ASSISTANT, content=[TextPart("第一轮回复")], message_id="m2")
    m3_merged = UniMessage(
        role=ROLE_USER,
        content=[TextPart("# 任务手记快照\n..."), TextPart("第二轮补充")],
        message_id="m3",
    )
    m4 = UniMessage(role=ROLE_ASSISTANT, content=[TextPart("第二轮回复")], message_id="m4")
    messages = [m1, m2, m3_merged, m4]

    # 1. 起点为 m3 时，起算范围包含 m3 中合并进来的原文 + m4
    start = resolve_range_start(messages, covered_until_message_id="m3", prefix_end_message_id=None)
    assert start.start_message_id == "m3"
    assert start.start_idx == 3
    assert start.merged_head is not None
    assert start.merged_head.content[0].text == "第二轮补充"

    sub_msgs = range_messages(messages, start)
    assert len(sub_msgs) == 2
    assert sub_msgs[0].content[0].text == "第二轮补充"
    assert sub_msgs[1].message_id == "m4"

    toks = estimate_range_tokens(messages, start)
    assert toks > 0

    # 2. 起点不存在时，回退到快照消息 m3
    start_missing = resolve_range_start(messages, covered_until_message_id="missing_mid", prefix_end_message_id=None)
    assert start_missing.start_message_id == "m3"
    assert start_missing.start_idx == 3

    # 3. 无任何起点时，从第 0 条开始
    start_none = resolve_range_start(messages, covered_until_message_id=None, prefix_end_message_id=None)
    assert start_none.start_message_id is None
    assert start_none.start_idx == 0
    assert len(range_messages(messages, start_none)) == 4

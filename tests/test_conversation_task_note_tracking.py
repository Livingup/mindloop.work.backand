# -*- coding: utf-8 -*-
"""Conversation 对象 task_note_tracking 持久化、重启恢复与重置同步测试。"""
from __future__ import annotations

import pytest
from pathlib import Path

from agent_runtime.conversation_message.infrastructure.sqlite_conversation_repository import (
    SqliteConversationRepository,
)
from agent_runtime.conversation_message.message.models import (
    Conversation,
    ConversationState,
    ROLE_USER,
    ROLE_ASSISTANT,
    TextPart,
    UniMessage,
)
from agent_runtime.conversation_message.message.operations import create_conversation
from agent_runtime.task_note.task_note_store import TaskNoteStore
from agent_runtime.task_note.task_note_unit_slicer import TaskNoteUnitSlicer


@pytest.mark.asyncio
async def test_conversation_state_task_note_tracking_persistence_and_restart(tmp_path: Path):
    """测试 task_note_tracking 在 SQLite 中的持久化与重启加载。"""
    repo = SqliteConversationRepository(data_dir=tmp_path)
    conv = create_conversation()

    # 1. 模拟记录 Task Note 跟踪指标
    tracking_data = {
        "covered_until_seq": 88,
        "covered_until_message_id": "msg-breakpoint-88",
        "note_version": 3,
        "inherited_processed": True,
        "accumulated_incremental_tokens": 42000,
    }
    conv.state.task_note_tracking = tracking_data
    conv.append_message(UniMessage(role=ROLE_USER, content=[TextPart("你好")], message_id="m1"))
    conv.append_message(UniMessage(role=ROLE_ASSISTANT, content=[TextPart("世界")], message_id="m2"))

    await repo.create(
        conv,
        task_id="task-test",
        thread_id="thread-test",
        agent_id="agent-test",
        agent_instance_id="inst-test",
    )
    await repo.save(conv)
    repo.close()

    # 2. 模拟用户重启程序：创建全新仓库连接并读取
    restarted_repo = SqliteConversationRepository(data_dir=tmp_path)
    loaded_conv = await restarted_repo.get(conv.id)

    assert loaded_conv.state.task_note_tracking is not None
    assert loaded_conv.state.task_note_tracking["covered_until_seq"] == 88
    assert loaded_conv.state.task_note_tracking["covered_until_message_id"] == "msg-breakpoint-88"
    assert loaded_conv.state.task_note_tracking["note_version"] == 3
    assert loaded_conv.state.task_note_tracking["inherited_processed"] is True
    assert loaded_conv.state.task_note_tracking["accumulated_incremental_tokens"] == 42000
    restarted_repo.close()


@pytest.mark.asyncio
async def test_conversation_reset_sync(tmp_path: Path):
    """测试会话重置时跟踪指标置空与 Watermark 重置。"""
    repo = SqliteConversationRepository(data_dir=tmp_path)
    conv = create_conversation()
    conv.state.task_note_tracking = {
        "covered_until_seq": 50,
        "covered_until_message_id": "msg-50",
    }
    await repo.create(
        conv,
        task_id="task-reset",
        thread_id="thread-reset",
        agent_id="agent-reset",
        agent_instance_id="inst-reset",
    )

    # 重置会话
    conv.state.task_note_tracking = None
    await repo.save(conv)

    # 重新加载验证
    reloaded = await repo.get(conv.id)
    assert reloaded.state.task_note_tracking is None
    repo.close()

    # 验证 TaskNoteStore.reset_watermark 逻辑可用（内存无数据库连接时不抛出异常）
    store = TaskNoteStore(connection_factory=None)
    assert store.reset_watermark(agent_instance_id="inst-reset", task_id="task-reset") is True


def test_slicer_with_covered_message_id_resilience():
    """测试即使消息序列发生裁剪或 seq 重新编号，物理 message_id 仍能精确锚定断点。"""
    slicer = TaskNoteUnitSlicer(trigger_threshold_tokens=50)

    # 模拟历史消息中包含 msg-cut
    messages = [
        {"id": "msg-1", "role": "user", "content": "历史1"},
        {"id": "msg-cut", "role": "assistant", "content": "这是上一轮覆盖的最后一条消息"},
        {"id": "msg-new-1", "role": "user", "content": "新提问1"},
        {"id": "msg-new-2", "role": "assistant", "content": "新回复2" * 20},
    ]

    slice_res = slicer.slice_messages(
        messages=messages,
        watermark_covered_until_seq=999,  # 故意传入已错乱的 seq
        covered_until_message_id="msg-cut",  # 依靠物理消息 ID 自适应校准
    )

    # msg-cut 所在的 Unit seq 应该成为真实基准线
    assert slice_res.watermark_covered_until_seq == 2
    # 增量只应该包含 msg-new-1 和 msg-new-2
    assert len(slice_res.incremental_units) == 2
    assert slice_res.incremental_units[0].messages[0]["id"] == "msg-new-1"

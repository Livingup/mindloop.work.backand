# -*- coding: utf-8 -*-
"""TaskNoteTrackingCoordinator 单元测试。"""
from __future__ import annotations

import asyncio
import pytest

from agent_runtime.conversation_message.infrastructure.in_memory_conversation_repository import (
    InMemoryConversationRepository,
)
from agent_runtime.conversation_message.message.models import (
    Conversation,
    ROLE_USER,
    ROLE_ASSISTANT,
    TextPart,
    UniMessage,
)
from agent_runtime.task_note.task_note_models import TaskNoteTracking, ObservingRun
from agent_runtime.task_note.task_note_tracking_coordinator import (
    TaskNoteTrackingCoordinator,
)


@pytest.fixture
def repo():
    return InMemoryConversationRepository()


@pytest.fixture
def coordinator(repo):
    return TaskNoteTrackingCoordinator(
        conversation_repository=repo,
        trigger_threshold_tokens=20,  # 较低门槛便于触发
    )


@pytest.mark.asyncio
async def test_concurrent_instances_both_get_tickets(repo, coordinator):
    """测试两个不同实例同时达到阈值，都能拿到 ticket。"""
    c1 = Conversation(id="conv-1")
    c1.append_message(UniMessage(role=ROLE_USER, content=[TextPart("需求描述" * 30)], message_id="m1-1"))
    await repo.create(c1, task_id="t1", thread_id="th1", agent_id="a1", agent_instance_id="i1")

    c2 = Conversation(id="conv-2")
    c2.append_message(UniMessage(role=ROLE_USER, content=[TextPart("需求描述" * 30)], message_id="m2-1"))
    await repo.create(c2, task_id="t2", thread_id="th2", agent_id="a2", agent_instance_id="i2")

    t1, t2 = await asyncio.gather(
        coordinator.on_round(task_id="t1", instance_id="i1", conversation=c1, is_subagent=False),
        coordinator.on_round(task_id="t2", instance_id="i2", conversation=c2, is_subagent=False),
    )
    assert t1 is not None
    assert t2 is not None
    assert t1.agent_instance_id == "i1"
    assert t2.agent_instance_id == "i2"
    assert t1.run_id != t2.run_id


@pytest.mark.asyncio
async def test_same_instance_consecutive_on_round_single_ticket(repo, coordinator):
    """测试同一实例连续两次调用 on_round，仅首次获取 ticket (去重与排他)。"""
    conv = Conversation(id="conv-single")
    conv.append_message(UniMessage(role=ROLE_USER, content=[TextPart("指令内容" * 30)], message_id="m-1"))
    await repo.create(conv, task_id="t-1", thread_id="th-1", agent_id="a-1", agent_instance_id="i-1")

    ticket1 = await coordinator.on_round(task_id="t-1", instance_id="i-1", conversation=conv, is_subagent=False)
    assert ticket1 is not None

    ticket2 = await coordinator.on_round(task_id="t-1", instance_id="i-1", conversation=conv, is_subagent=False)
    assert ticket2 is None


@pytest.mark.asyncio
async def test_complete_mismatched_run_id_rejected(repo, coordinator):
    """测试 run_id 不一致时 complete 不推进状态。"""
    conv = Conversation(id="conv-mismatch")
    conv.append_message(UniMessage(role=ROLE_USER, content=[TextPart("指令内容" * 30)], message_id="m-1"))
    await repo.create(conv, task_id="t-1", thread_id="th-1", agent_id="a-1", agent_instance_id="i-1")

    ticket = await coordinator.on_round(task_id="t-1", instance_id="i-1", conversation=conv, is_subagent=False)
    assert ticket is not None

    # 用错误的 run_id 尝试 complete
    res = await coordinator.complete(
        instance_id="i-1",
        conversation_id="conv-mismatch",
        run_id="fake_run_id",
        success=True,
        covered_until_message_id="m-1",
    )
    assert res is None

    # 原 tracking 保持 observing 状态未推进
    tracking = await coordinator.get("conv-mismatch")
    assert tracking.note_version == 0
    assert tracking.observing is not None
    assert tracking.observing.run_id == ticket.run_id


@pytest.mark.asyncio
async def test_await_running_blocks_until_complete(repo, coordinator):
    """测试 await_running 一直阻塞，直到 complete 被调用。"""
    conv = Conversation(id="conv-wait")
    conv.append_message(UniMessage(role=ROLE_USER, content=[TextPart("长文本" * 30)], message_id="m-1"))
    await repo.create(conv, task_id="t-1", thread_id="th-1", agent_id="a-1", agent_instance_id="i-1")

    ticket = await coordinator.on_round(task_id="t-1", instance_id="i-1", conversation=conv, is_subagent=False)
    assert ticket is not None

    wait_finished = False

    async def waiter():
        nonlocal wait_finished
        await coordinator.await_running(instance_id="i-1", conversation_id="conv-wait")
        wait_finished = True

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert wait_finished is False  # 正在等待中

    # 外部完成观察
    new_ver = await coordinator.complete(
        instance_id="i-1",
        conversation_id="conv-wait",
        run_id=ticket.run_id,
        success=True,
        covered_until_message_id="m-1",
    )
    assert new_ver == 1

    await task
    assert wait_finished is True


@pytest.mark.asyncio
async def test_stale_observing_cleaned_on_restart(repo, coordinator):
    """测试 observing 是重启前遗留的（不在 _done 中），会自动清理。"""
    conv = Conversation(id="conv-stale")
    conv.append_message(UniMessage(role=ROLE_USER, content=[TextPart("长文本" * 30)], message_id="m-1"))
    await repo.create(conv, task_id="t-1", thread_id="th-1", agent_id="a-1", agent_instance_id="i-1")

    # 手动模拟持久化了一个旧 observing（未在 coordinator._done 中）
    stale_tracking = TaskNoteTracking(
        version=1,
        observing=ObservingRun(
            run_id="obs_old_dead",
            start_after_message_id=None,
            end_message_id="m-1",
            started_at="2026-10-13T00:00:00Z",
        ),
    )
    await repo.cas_task_note_tracking("conv-stale", 0, stale_tracking.to_dict())

    # 调用 on_round 会自动清理该遗留 run 并正常签发新 ticket
    ticket = await coordinator.on_round(task_id="t-1", instance_id="i-1", conversation=conv, is_subagent=False)
    assert ticket is not None
    assert ticket.run_id != "obs_old_dead"


@pytest.mark.asyncio
async def test_force_true_acquires_ticket_below_threshold(repo, coordinator):
    """测试当 token 低于阈值时，若 force=True 能直接拿到 ticket。"""
    conv = Conversation(id="conv-force")
    conv.append_message(UniMessage(role=ROLE_USER, content=[TextPart("短")], message_id="m-1"))
    await repo.create(conv, task_id="t-1", thread_id="th-1", agent_id="a-1", agent_instance_id="i-1")

    ticket_norm = await coordinator.on_round(task_id="t-1", instance_id="i-1", conversation=conv, is_subagent=False, force=False)
    assert ticket_norm is None

    ticket_force = await coordinator.on_round(task_id="t-1", instance_id="i-1", conversation=conv, is_subagent=False, force=True)
    assert ticket_force is not None
    assert ticket_force.end_message_id == "m-1"


"""测试 Task Note 观察者触发逻辑与父子关系解析 (修复回归测试)。"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine
from agent_runtime.agent.agent_instance import AgentInstance
from agent_runtime.agent.agent_task import AgentParentChildRelation, AgentTask
from agent_runtime.conversation_message.message.models import Conversation
from agent_runtime.task_note.task_note_tracking_coordinator import ObserveTicket
from agent_runtime.task_note.task_note_observer_runner import _resolve_stream_id


def _make_engine(
    *,
    gateway: MagicMock | None = None,
    coordinator: MagicMock | None = None,
    scheduler: MagicMock | None = None,
) -> AgentExecutionEngine:
    engine = AgentExecutionEngine.__new__(AgentExecutionEngine)
    engine._runtime_gateway = gateway or MagicMock()
    engine._task_note_tracking_coordinator = coordinator
    engine._task_note_observer_scheduler = scheduler
    return engine


def _make_instance(instance_id: str, task_id: str) -> AgentInstance:
    instance = AgentInstance.__new__(AgentInstance)
    object.__setattr__(instance, "instance_id", instance_id)
    object.__setattr__(instance, "task_id", task_id)
    return instance


def _build_task_hierarchy(
    task_id: str = "t1",
    root_id: str = "inst-root",
    child_id: str = "inst-child",
    grandchild_id: str = "inst-grandchild",
) -> AgentTask:
    rel1 = AgentParentChildRelation(
        relation_id="rel-1",
        task_id=task_id,
        parent_instance_id=root_id,
        child_instance_id=child_id,
    )
    rel2 = AgentParentChildRelation(
        relation_id="rel-2",
        task_id=task_id,
        parent_instance_id=child_id,
        child_instance_id=grandchild_id,
    )
    return AgentTask(
        task_id=task_id,
        root_agent_instance_id=root_id,
        agent_instance_ids=(root_id, child_id, grandchild_id),
        parent_child_relations=(rel1, rel2),
    )


def test_resolve_stream_id() -> None:
    """验收 _resolve_stream_id: 有 scene_id 走场景流，无 scene_id 走任务流。"""
    assert _resolve_stream_id("task-123", "scene-abc") == "stream-scene-scene-abc"
    assert _resolve_stream_id("task-123", None) == "stream-agent-task-task-123"
    assert _resolve_stream_id("task-123", "") == "stream-agent-task-task-123"


def test_resolve_parent_instance_id_and_ancestor_chain() -> None:
    """验收根实例与多级子实例的父实例解析和祖先链生成。"""
    task = _build_task_hierarchy()
    gateway = MagicMock()
    gateway.get_task.return_value = task
    engine = _make_engine(gateway=gateway)

    root_inst = _make_instance("inst-root", "t1")
    child_inst = _make_instance("inst-child", "t1")
    grandchild_inst = _make_instance("inst-grandchild", "t1")

    # 1. 根实例验证
    assert engine._resolve_parent_instance_id(root_inst) is None
    assert engine._ancestor_chain(root_inst) == []

    # 2. 一级子实例验证
    assert engine._resolve_parent_instance_id(child_inst) == "inst-root"
    assert engine._ancestor_chain(child_inst) == ["inst-root"]

    # 3. 二级孙实例验证 (近亲在前)
    assert engine._resolve_parent_instance_id(grandchild_inst) == "inst-child"
    assert engine._ancestor_chain(grandchild_inst) == ["inst-child", "inst-root"]


def test_resolve_parent_instance_id_task_not_found(caplog: pytest.LogCaptureFixture) -> None:
    """验收 task 不存在时优雅返回 None 并记录警告日志。"""
    gateway = MagicMock()
    gateway.get_task.side_effect = KeyError("task-not-found")
    engine = _make_engine(gateway=gateway)

    inst = _make_instance("inst-1", "task-not-found")
    with caplog.at_level(logging.WARNING):
        parent_id = engine._resolve_parent_instance_id(inst)
        chain = engine._ancestor_chain(inst)

    assert parent_id is None
    assert chain == []
    assert any("解析父实例失败，按根实例处理" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_trigger_background_observer_root_and_subagent() -> None:
    """验收增量触发时向 coordinator 传递正确的 is_subagent，向 scheduler 传递 parent 与 ancestor_chain。"""
    task = _build_task_hierarchy()
    gateway = MagicMock()
    gateway.get_task.return_value = task

    coordinator = MagicMock()
    ticket_mock = ObserveTicket(
        run_id="run-1",
        task_id="t1",
        agent_instance_id="inst-grandchild",
        conversation_id="conv-1",
        messages=[],
        start_after_message_id=None,
        end_message_id="msg-end",
        inherited_until_message_id=None,
        snapshot_message_id=None,
        is_subagent=True,
    )
    coordinator.on_round = AsyncMock(return_value=ticket_mock)

    scheduler = MagicMock()
    scheduler.start = MagicMock()

    engine = _make_engine(gateway=gateway, coordinator=coordinator, scheduler=scheduler)
    conv = Conversation(id="conv-1", system_prompt="system prompt text")

    # 触发孙实例
    grandchild_inst = _make_instance("inst-grandchild", "t1")
    await engine._trigger_background_observer_if_needed(
        task_id="t1",
        instance=grandchild_inst,
        conversation=conv,
        scene_id="scene-1",
    )

    coordinator.on_round.assert_awaited_once_with(
        task_id="t1",
        instance_id="inst-grandchild",
        conversation=conv,
        is_subagent=True,
    )
    scheduler.start.assert_called_once_with(
        ticket_mock,
        target_agent_definition_prompt="system prompt text",
        parent_agent_instance_id="inst-child",
        ancestor_chain=["inst-child", "inst-root"],
        scene_id="scene-1",
    )


@pytest.mark.asyncio
async def test_trigger_background_observer_coordinator_error_warning(caplog: pytest.LogCaptureFixture) -> None:
    """验收 coordinator 异常时记录 WARNING，带 exc_info 且不向外抛出。"""
    task = _build_task_hierarchy()
    gateway = MagicMock()
    gateway.get_task.return_value = task

    coordinator = MagicMock()
    coordinator.on_round = AsyncMock(side_effect=RuntimeError("coordinator storage failed"))
    scheduler = MagicMock()

    engine = _make_engine(gateway=gateway, coordinator=coordinator, scheduler=scheduler)
    conv = Conversation(id="conv-1", system_prompt="test")
    inst = _make_instance("inst-root", "t1")

    with caplog.at_level(logging.WARNING):
        # 必须不抛出异常
        await engine._trigger_background_observer_if_needed(
            task_id="t1",
            instance=inst,
            conversation=conv,
            scene_id=None,
        )

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("触发后台观察者失败: task=t1 instance=inst-root" in r.message for r in warning_records)
    assert any(r.exc_info is not None for r in warning_records)

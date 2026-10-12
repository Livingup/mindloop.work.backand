# -*- coding: utf-8 -*-
"""TaskNoteObserverScheduler 单元测试。"""
from __future__ import annotations

import asyncio
import pytest

from agent_runtime.task_note.task_note_models import TaskNoteWatermark
from agent_runtime.task_note.task_note_observer_scheduler import (
    ObserverSessionInput,
    ObserverSessionResult,
    TaskNoteObserverScheduler,
    format_units_for_observer,
)
from agent_runtime.task_note.task_note_store import TaskNoteStore
from agent_runtime.task_note.task_note_unit_slicer import TaskNoteUnitSlicer


class MockTaskNoteStore:
    def __init__(self):
        self.watermarks: dict[str, TaskNoteWatermark] = {}
        self.snapshots: dict[str, dict] = {}

    def get_watermark(self, agent_instance_id: str, task_id: str) -> TaskNoteWatermark:
        key = f"{agent_instance_id}:{task_id}"
        if key not in self.watermarks:
            self.watermarks[key] = TaskNoteWatermark(
                agent_instance_id=agent_instance_id,
                task_id=task_id,
                covered_until_seq=0,
                note_version=0,
                inherited_processed=False,
            )
        return self.watermarks[key]

    def update_watermark(
        self, agent_instance_id: str, task_id: str, covered_until_seq: int, note_version_inc: bool = True
    ) -> dict:
        key = f"{agent_instance_id}:{task_id}"
        wm = self.get_watermark(agent_instance_id, task_id)
        wm.covered_until_seq = covered_until_seq
        if note_version_inc:
            wm.note_version += 1
        return {"covered_until_seq": wm.covered_until_seq, "note_version": wm.note_version}

    def set_inherited_processed(self, agent_instance_id: str, task_id: str) -> bool:
        wm = self.get_watermark(agent_instance_id, task_id)
        wm.inherited_processed = True
        return True

    def get_note_snapshot(self, task_id: str, agent_instance_id: str) -> dict:
        return {"missions": [], "todos": []}


@pytest.mark.asyncio
async def test_scheduler_trigger_and_concurrency_lock():
    store = MockTaskNoteStore()
    slicer = TaskNoteUnitSlicer(trigger_threshold_tokens=20)

    # 模拟耗时 0.2 秒的观察者
    async def slow_runner(session_input: ObserverSessionInput) -> ObserverSessionResult:
        await asyncio.sleep(0.2)
        return ObserverSessionResult(
            success=True,
            covered_until_seq=session_input.target_seq,
            note_version=1,
        )

    scheduler = TaskNoteObserverScheduler(
        slicer=slicer,
        store=store,
        observer_runner=slow_runner,
        trigger_threshold_tokens=20,
    )

    messages = [
        {"id": "m1", "role": "user", "content": "问题一" * 20},
        {"id": "m2", "role": "assistant", "content": "回答一" * 20},
    ]

    # 1. 首次触发：成功拉起后台观察
    triggered = await scheduler.check_and_trigger(
        agent_instance_id="agent-01",
        task_id="task-01",
        messages=messages,
    )
    assert triggered is True
    assert scheduler.is_observer_running("agent-01") is True

    # 2. 并发保护：在运行期间再次尝试触发，应被跳过
    triggered_again = await scheduler.check_and_trigger(
        agent_instance_id="agent-01",
        task_id="task-01",
        messages=messages,
    )
    assert triggered_again is False

    # 3. 等待观察任务完成
    res = await scheduler.wait_for_observer("agent-01", timeout=1.0)
    assert res is not None
    assert res.success is True
    assert res.covered_until_seq == 2
    assert scheduler.is_observer_running("agent-01") is False

    # 4. 验证 watermark 已推进
    wm = store.get_watermark("agent-01", "task-01")
    assert wm.covered_until_seq == 2
    assert wm.note_version == 1


@pytest.mark.asyncio
async def test_scheduler_subagent_inherited_marking():
    store = MockTaskNoteStore()
    slicer = TaskNoteUnitSlicer(trigger_threshold_tokens=10)

    async def quick_runner(session_input: ObserverSessionInput) -> ObserverSessionResult:
        return ObserverSessionResult(
            success=True,
            covered_until_seq=session_input.target_seq,
            note_version=1,
        )

    scheduler = TaskNoteObserverScheduler(
        slicer=slicer,
        store=store,
        observer_runner=quick_runner,
        trigger_threshold_tokens=10,
    )

    messages = [
        {"id": "m1", "role": "user", "content": "父会话初始化"},
        {"id": "m_fork", "role": "user", "content": "委派边界"},
        {"id": "m3", "role": "assistant", "content": "子 Agent 工作中"},
    ]

    triggered = await scheduler.check_and_trigger(
        agent_instance_id="subagent-01",
        task_id="task-01",
        messages=messages,
        is_subagent=True,
        fork_message_id="m_fork",
        delegation_content="请完成子任务",
    )
    assert triggered is True

    await scheduler.wait_for_observer("subagent-01", timeout=1.0)
    wm = store.get_watermark("subagent-01", "task-01")
    assert wm.inherited_processed is True


@pytest.mark.asyncio
async def test_scheduler_invalid_covered_seq_rejected():
    store = MockTaskNoteStore()
    slicer = TaskNoteUnitSlicer(trigger_threshold_tokens=10)

    # 模拟观察者返回非法的已覆盖 seq=0（未能推进）
    async def bad_runner(session_input: ObserverSessionInput) -> ObserverSessionResult:
        return ObserverSessionResult(
            success=True,
            covered_until_seq=0,
        )

    scheduler = TaskNoteObserverScheduler(
        slicer=slicer,
        store=store,
        observer_runner=bad_runner,
        trigger_threshold_tokens=10,
    )

    messages = [
        {"id": "m1", "role": "user", "content": "测试指令"},
        {"id": "m2", "role": "assistant", "content": "测试回复"},
    ]

    await scheduler.check_and_trigger(
        agent_instance_id="agent-bad",
        task_id="task-01",
        messages=messages,
    )

    res = await scheduler.wait_for_observer("agent-bad", timeout=1.0)
    assert res.success is False
    # 水位保持为 0，不推进
    wm = store.get_watermark("agent-bad", "task-01")
    assert wm.covered_until_seq == 0

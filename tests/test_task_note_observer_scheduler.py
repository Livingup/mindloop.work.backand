# -*- coding: utf-8 -*-
"""TaskNoteObserverScheduler 单元测试。"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from agent_runtime.conversation_message.message.models import (
    ROLE_ASSISTANT,
    ROLE_TOOL,
    ROLE_USER,
    TextPart,
    ToolResultPart,
    ToolUsePart,
    UniMessage,
)
from agent_runtime.task_note.task_note_models import (
    TaskNoteUnit,
)
from agent_runtime.task_note.task_note_observer_scheduler import (
    ObserverSessionInput,
    ObserverSessionResult,
    TaskNoteObserverScheduler,
    build_observer_user_payload,
    serialize_units_to_list,
)
from agent_runtime.task_note.task_note_tracking_coordinator import ObserveTicket


@pytest.mark.asyncio
async def test_scheduler_start_and_cancel_lifecycle():
    """测试调度器 start 启动后台观察与 cancel 安全取消。"""
    executed = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_runner(session_input: ObserverSessionInput) -> ObserverSessionResult:
        executed.set()
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return ObserverSessionResult(success=True)

    scheduler = TaskNoteObserverScheduler(
        observer_runner=slow_runner,
        trigger_threshold_tokens=20,
    )
    scheduler.store = MagicMock()
    scheduler.store.get_note_snapshot.return_value = {}

    ticket = ObserveTicket(
        run_id="run-01",
        task_id="task-01",
        agent_instance_id="agent-01",
        conversation_id="conv-01",
        messages=[],
        start_after_message_id=None,
        end_message_id=None,
        inherited_until_message_id=None,
        snapshot_message_id=None,
        is_subagent=False,
    )

    task = scheduler.start(ticket)
    await asyncio.wait_for(executed.wait(), timeout=2.0)
    assert scheduler.is_observer_running("agent-01") is True

    await scheduler.cancel("agent-01")
    assert cancelled.is_set() is True
    assert scheduler.is_observer_running("agent-01") is False


@pytest.mark.asyncio
async def test_scheduler_run_sync():
    """测试调度器 run_sync 同步执行并返回结果。"""
    async def quick_runner(session_input: ObserverSessionInput) -> ObserverSessionResult:
        return ObserverSessionResult(
            success=True,
            covered_until_seq=1,
            note_version=1,
        )

    scheduler = TaskNoteObserverScheduler(
        observer_runner=quick_runner,
        trigger_threshold_tokens=20,
    )
    scheduler.store = MagicMock()
    scheduler.store.get_note_snapshot.return_value = {}

    ticket = ObserveTicket(
        run_id="run-sync-01",
        task_id="task-01",
        agent_instance_id="agent-sync",
        conversation_id="conv-01",
        messages=[],
        start_after_message_id=None,
        end_message_id=None,
        inherited_until_message_id=None,
        snapshot_message_id=None,
        is_subagent=False,
    )

    res = await scheduler.run_sync(ticket)
    assert res.success is True
    assert res.note_version == 1
    assert scheduler.is_observer_running("agent-sync") is False


@pytest.mark.asyncio
async def test_scheduler_concurrent_multi_instances():
    """测试两个实例的观察可以并行运行。"""
    started_1 = asyncio.Event()
    started_2 = asyncio.Event()

    async def concurrent_runner(session_input: ObserverSessionInput) -> ObserverSessionResult:
        if session_input.agent_instance_id == "inst-1":
            started_1.set()
        else:
            started_2.set()
        await asyncio.sleep(0.2)
        return ObserverSessionResult(success=True)

    scheduler = TaskNoteObserverScheduler(
        observer_runner=concurrent_runner,
        trigger_threshold_tokens=10,
    )
    scheduler.store = MagicMock()
    scheduler.store.get_note_snapshot.return_value = {}

    ticket1 = ObserveTicket(
        run_id="r1", task_id="t1", agent_instance_id="inst-1",
        conversation_id="c1", messages=[], start_after_message_id=None,
        end_message_id=None, inherited_until_message_id=None,
        snapshot_message_id=None, is_subagent=False,
    )
    ticket2 = ObserveTicket(
        run_id="r2", task_id="t1", agent_instance_id="inst-2",
        conversation_id="c2", messages=[], start_after_message_id=None,
        end_message_id=None, inherited_until_message_id=None,
        snapshot_message_id=None, is_subagent=False,
    )

    scheduler.start(ticket1)
    scheduler.start(ticket2)

    await asyncio.wait_for(started_1.wait(), timeout=1.0)
    await asyncio.wait_for(started_2.wait(), timeout=1.0)

    assert scheduler.is_observer_running("inst-1") is True
    assert scheduler.is_observer_running("inst-2") is True
    assert scheduler.get_running_observer_count() == 2

    await scheduler.cancel("inst-1")
    await scheduler.cancel("inst-2")


def test_serialize_units_to_list_with_tool_use_and_auto_record():
    """测试 serialize_units_to_list 正确展开 ToolUsePart、ToolResultPart 及提升 auto_record。"""
    msg_asst = UniMessage(
        role=ROLE_ASSISTANT,
        content=[
            ToolUsePart(name="file_modify", input={"file": "a.py"}, tool_use_id="tu-1"),
        ],
        message_id="m-asst",
    )
    msg_tool = UniMessage(
        role=ROLE_TOOL,
        content=[
            ToolResultPart(
                tool_use_ref="tu-1",
                is_error=False,
                output={"status": "ok", "auto_record": {"artifact_id": "art-01", "type": "artifact"}},
            ),
        ],
        message_id="m-tool",
    )

    unit = TaskNoteUnit(
        seq=1,
        unit_type="tool_interaction",
        messages=[msg_asst, msg_tool],
        token_count=100,
        end_message_id="m-tool",
    )

    result_list = serialize_units_to_list([unit])
    assert len(result_list) == 1
    u_dict = result_list[0]
    assert u_dict["seq"] == 1
    msgs = u_dict["messages"]
    assert len(msgs) == 2

    # 1. 验证 tool_use
    asst_part = msgs[0]["tool_use"]
    assert asst_part["name"] == "file_modify"
    assert asst_part["id"] == "tu-1"
    assert asst_part["input"] == {"file": "a.py"}

    # 2. 验证 tool_result
    tool_part = msgs[1]["tool_result"]
    assert tool_part["ref"] == "tu-1"
    assert tool_part["is_error"] is False

    # 3. 验证 auto_record 提升至消息级
    assert msgs[1]["auto_record"] == {"artifact_id": "art-01", "type": "artifact"}


def test_build_observer_user_payload():
    unit1 = TaskNoteUnit(
        seq=1,
        unit_type="tool_interaction",
        messages=[
            {"role": "assistant", "content": "正在执行创建文件", "tool_calls": [{"id": "c1", "function": {"name": "file_create"}}]},
            {"role": "tool", "content": "ok", "auto_record": {"artifact_id": "art-999", "type": "artifact"}},
        ],
        token_count=50,
        end_message_id="msg-tool-1",
    )

    session_input = ObserverSessionInput(
        task_id="task-test-01",
        agent_instance_id="agent-worker-01",
        run_id="run-1",
        conversation_id="conv-1",
        target_seq=1,
        note_snapshot={"missions": [{"title": "实现支付模块", "status": "in_progress"}]},
        incremental_units=[unit1],
        is_subagent=True,
        delegation_content="请独立负责支付对接",
    )

    payload = build_observer_user_payload(
        session_input=session_input,
        target_agent_definition_prompt="你是一个资深全栈工程师",
    )

    assert "agent-worker-01" in payload
    assert "实现支付模块" in payload
    assert "请独立负责支付对接" in payload
    assert "art-999" in payload
    assert "程序底层已自动插入 auto_record" in payload


@pytest.mark.asyncio
async def test_observer_runner_handles_assistant_message_already_appended():
    """验证当 send_prompt 已将 assistant 响应写入 conversation 时，观察者不会报 Duplicate message_id 异常。"""
    from unittest.mock import AsyncMock
    from agent_runtime.llm_client.llm_request_service import LlmPromptResponse
    from agent_runtime.task_note.task_note_observer_runner import TaskNoteObserverRunner

    prompt_provider = MagicMock()
    prompt_provider.render_system_prompt.return_value = "system prompt"
    prompt_provider.get_note_tools.return_value = ()

    mock_llm_svc = MagicMock()
    call_count = 0

    async def fake_send_prompt(req):
        nonlocal call_count
        call_count += 1
        msg = UniMessage(
            role=ROLE_ASSISTANT,
            content=[TextPart(text=f"turn {call_count}")],
            message_id=f"msg-test-{call_count}",
        )
        # 模拟真实运行时：ingest_response 已提前将 assistant message 追加到会话
        req.conversation.append_message(msg)
        if call_count > 1:
            raise RuntimeError("stop observer loop for test")
        return LlmPromptResponse(assistant_message=msg, raw_response={})

    mock_llm_svc.send_prompt = AsyncMock(side_effect=fake_send_prompt)

    runner = TaskNoteObserverRunner(
        prompt_provider=prompt_provider,
        llm_request_service=mock_llm_svc,
        local_tool_gateway=MagicMock(),
    )

    inp = ObserverSessionInput(
        task_id="t1",
        agent_instance_id="a1",
        run_id="r1",
        conversation_id="c1",
        target_seq=1,
        note_snapshot={},
        incremental_units=[],
    )

    res = await runner(inp)
    assert res.success is False
    assert "stop observer loop for test" in (res.error_message or "")

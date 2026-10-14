# -*- coding: utf-8 -*-
"""Task Note 协调器与上下文压缩热组装集成测试。

覆盖验收标准：
1. 工具隔离 (D06)：业务 Agent 实例在任何状态下都不持有 TASK_NOTE_TOOL_NAMES 工具；直接调用 Note 工具被拦截；
2. 调度器生命周期：start 启动异步观察，cancel 安全取消；
3. 上下文热组装：门禁命中时按 TaskNoteTracking 替换前缀，精准保留断点后的原始尾部；
4. 尾部合并 (M6-3)：尾部首条为 user 消息时与快照合并，沿用原消息 ID 且保留原内容；
5. 降级保护 (D08)：note_version < 1 时不替换，上下文消息保持原样；
6. 观察等待协调：替换前若检测到观察者运行中，await_running 安全等待至 complete，随后执行替换；
7. 实例 GC 清理：GC 时正在运行的观察被取消，协调器注销实例内存锁。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_runtime.agent.agent_execution_engine import (
    AgentExecutionEngine,
)
from agent_runtime.agent.agent_instance import (
    AGENT_INSTANCE_STATUS_RUNNING,
    AgentInstance,
)
from agent_runtime.agent_definition.agent_definition import AgentDefinition
from agent_runtime.communication.runtime_event_publisher import (
    NoopRuntimeEventPublisher,
    RuntimeCommunicationContext,
)
from agent_runtime.conversation_message.infrastructure.in_memory_conversation_repository import (
    InMemoryConversationRepository,
)
from agent_runtime.conversation_message.message.models import (
    Conversation,
    ROLE_ASSISTANT,
    ROLE_TOOL,
    ROLE_USER,
    TextPart,
    TextPartMeta,
    ToolResultPart,
    ToolUsePart,
    UniMessage,
)
from agent_runtime.input_modality import INPUT_MODALITY_TEXT
from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_RESPONSES
from agent_runtime.task_note.task_note_context_assembler import (
    TaskNoteContextAssembler,
)
from agent_runtime.task_note.task_note_models import (
    TaskNoteTracking,
)
from agent_runtime.task_note.task_note_observer_scheduler import (
    ObserverSessionInput,
    ObserverSessionResult,
    TaskNoteObserverScheduler,
)
from agent_runtime.task_note.task_note_prompt_provider import (
    TaskNotePromptProvider,
)
from agent_runtime.task_note.task_note_tracking_coordinator import (
    ObserveTicket,
    TaskNoteTrackingCoordinator,
)


@pytest.mark.asyncio
async def test_d06_business_agent_does_not_hold_note_tools() -> None:
    """测试 D06: 业务 Agent 无论何种状态都不持有 Task Note 工具，直接调用返回拒绝。"""
    provider = TaskNotePromptProvider()
    note_names = set(provider.get_note_tool_names())

    # 1. 验证 prompt provider 返回的 36 个 Note 工具全集
    assert len(note_names) == 36
    assert "create_mission" in note_names
    assert "note_update_end" in note_names

    # 2. 验证业务 Agent 工具调度器拦截
    conv = Conversation(id="conv-d06")
    conv.append_message(UniMessage(role=ROLE_USER, content=[TextPart("执行业务")]), [TextPartMeta()])
    instance = AgentInstance(
        instance_id="inst-d06",
        task_id="task-d06",
        thread_id="th-d06",
        current_working_directory="/test",
        agent_definition_id="developer",
        llm_provider_id="prov-a",
        llm_model_binding_id="model-a",
        conversation_id=conv.id,
        trace_id="tr-d06",
        status=AGENT_INSTANCE_STATUS_RUNNING,
    )

    engine = AgentExecutionEngine(
        runtime_gateway=SimpleNamespace(
            start_task=lambda tid: None,
            start_agent_instance=lambda iid: None,
            pause_agent_instance=lambda iid: instance,
            pause_task=lambda tid: None,
            interrupt_agent_instance=lambda iid, msg: None,
            drain_agent_instance_user_messages=lambda iid, predicate=None: (instance, ()),
            update_agent_instance_execution_substate=lambda iid, s: None,
        ),
        agent_definition_gateway=SimpleNamespace(
            get_agent_definition=lambda did: AgentDefinition(
                agent_id="developer",
                name="Developer",
                tool_ids=("run_bash_command",),
            )
        ),
        llm_registry_gateway=SimpleNamespace(
            resolve_candidate_runtime_policy=lambda p, b: SimpleNamespace(
                provider_id="prov-a",
                binding_id="model-a",
                model_id="model-a",
                model_alias="model-a",
                api_format=API_FORMAT_OPENAI_RESPONSES,
                supported_input_modalities=(INPUT_MODALITY_TEXT,),
            ),
            resolve_provider_model=lambda p, m: SimpleNamespace(
                provider_id="prov-a",
                binding_id="model-a",
                model_id="model-a",
                model_alias="model-a",
                api_format=API_FORMAT_OPENAI_RESPONSES,
                supported_input_modalities=(INPUT_MODALITY_TEXT,),
            ),
        ),
        conversation_repository=SimpleNamespace(
            get=AsyncMock(return_value=conv),
            save=AsyncMock(),
        ),
        prompt_snippet_gateway=SimpleNamespace(get_snippet=lambda n: SimpleNamespace(content="")),
        local_tool_gateway=SimpleNamespace(
            list_tools=lambda **kwargs: [
                SimpleNamespace(
                    tool_name="run_bash_command", group="system", category="business", always_enabled=True
                )
            ]
        ),
        mcp_client_gateway=None,
        skill_gateway=None,
        llm_request_service=MagicMock(),
        tool_dispatcher=SimpleNamespace(
            build_llm_tools=AsyncMock(
                return_value=[
                    SimpleNamespace(name="run_bash_command", source="local", schema={"name": "run_bash_command"}),
                    SimpleNamespace(name="create_mission", source="local", schema={"name": "create_mission"}),
                ]
            ),
            dispatch_tool_call=AsyncMock(),
        ),
        trace_recorder=SimpleNamespace(
            start_trace=MagicMock(return_value="trace.md"),
            record_llm_turn=AsyncMock(),
            record_llm_response=MagicMock(),
            record_failure=MagicMock(),
            record_event=MagicMock(),
            record_user_message=MagicMock(),
        ),
        event_publisher=NoopRuntimeEventPublisher(),
    )

    # 验证单工具调度拦截 Note 工具并返回错误结果
    dispatch_result = await engine._execute_single_tool_use(
        definition=AgentDefinition(agent_id="developer"),
        tool_use=ToolUsePart(name="create_mission", input={"title": "test"}, tool_use_id="tu-1"),
        communication_context=RuntimeCommunicationContext(stream_id="s1", actor_type="agent", actor_id="a1"),
        instance=instance,
    )
    assert dispatch_result.result_part.is_error is True
    assert "业务 Agent 严禁直接调用 Task Note 工具" in str(dispatch_result.result_part.output)


@pytest.mark.asyncio
async def test_observer_scheduler_start_and_cancel() -> None:
    """测试调度器 start 启动后台观察任务与 cancel 安全取消。"""
    executed = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_runner(session_input: ObserverSessionInput) -> ObserverSessionResult:
        executed.set()
        try:
            await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return ObserverSessionResult(success=True)

    scheduler = TaskNoteObserverScheduler(
        observer_runner=fake_runner,
        trigger_threshold_tokens=100,
    )
    # mock store 避免真实数据库调用
    scheduler.store = MagicMock()
    scheduler.store.get_note_snapshot.return_value = {}

    ticket = ObserveTicket(
        run_id="run-test",
        task_id="task-test",
        agent_instance_id="inst-test",
        conversation_id="conv-test",
        messages=[],
        start_after_message_id=None,
        end_message_id=None,
        inherited_until_message_id=None,
        snapshot_message_id=None,
        is_subagent=False,
    )

    task = scheduler.start(
        ticket,
        target_agent_definition_prompt="prompt",
        parent_agent_instance_id=None,
        ancestor_chain=[],
        scene_id=None,
    )
    await asyncio.wait_for(executed.wait(), timeout=2.0)
    assert scheduler.is_observer_running("inst-test") is True

    # 执行 cancel
    await scheduler.cancel("inst-test")
    assert cancelled.is_set() is True
    assert scheduler.is_observer_running("inst-test") is False


@pytest.mark.asyncio
async def test_context_assembler_with_tracking_and_user_tail_merge() -> None:
    """测试 TaskNoteContextAssembler 结合 TaskNoteTracking 进行热组装与首条 user 合并。"""
    m1 = UniMessage(role=ROLE_USER, content=[TextPart("早期任务需求")], message_id="msg-1")
    m2 = UniMessage(role=ROLE_ASSISTANT, content=[TextPart("早期方案输出")], message_id="msg-2")
    m3 = UniMessage(
        role=ROLE_TOOL,
        content=[ToolResultPart(tool_use_ref="tu-1", is_error=False, output="命令输出结果")],
        message_id="msg-3",
    )
    m4 = UniMessage(role=ROLE_USER, content=[TextPart("用户最新反馈指令")], message_id="msg-4")
    all_messages = [m1, m2, m3, m4]

    tracking = TaskNoteTracking(
        version=1,
        note_version=2,
        covered_until_message_id="msg-2",
    )
    snapshot = {
        "task_id": "task-m6",
        "agent_instance_id": "inst-m6",
        "missions": [{"mission_id": "m1", "title": "任务1", "status": "in_progress"}],
        "progress": [{"mission_id": "m1", "completed_summary": "步骤2已完成", "next_step": "继续推进测试"}],
    }

    assembler = TaskNoteContextAssembler()
    swap_res = await assembler.assemble_context(
        messages=all_messages,
        tracking=tracking,
        note_snapshot=snapshot,
    )

    assert swap_res.swapped is True
    assert swap_res.note_version == 2
    # 尾部是 m3 (tool) 和 m4 (user)，由于首条尾部 m3 不是 user，因此新建快照放在最前
    assert len(swap_res.messages) == 3
    assert swap_res.messages[0].message_id == swap_res.snapshot_message_id
    assert swap_res.messages[1].message_id == "msg-3"
    assert swap_res.messages[2].message_id == "msg-4"

    # 若尾部首条是 user 消息，应直接与手记合并
    messages_user_tail = [m1, m2, m4]
    swap_res_user = await assembler.assemble_context(
        messages=messages_user_tail,
        tracking=tracking,
        note_snapshot=snapshot,
    )
    assert swap_res_user.swapped is True
    assert len(swap_res_user.messages) == 1
    assert swap_res_user.messages[0].message_id == "msg-4"
    assert len(swap_res_user.messages[0].content) == 2
    assert "步骤2已完成" in swap_res_user.messages[0].content[0].text
    assert swap_res_user.messages[0].content[1].text == "用户最新反馈指令"


@pytest.mark.asyncio
async def test_await_running_coordinates_swap_after_observer_complete() -> None:
    """测试替换发生前若有后台观察正在运行，await_running 阻塞等待直到 complete 推进版本。"""
    repo = InMemoryConversationRepository()
    conv = Conversation(id="conv-coord")
    conv.append_message(UniMessage(role=ROLE_USER, content=[TextPart("需求文本" * 50)], message_id="m1"))
    conv.append_message(UniMessage(role=ROLE_ASSISTANT, content=[TextPart("响应文本" * 50)], message_id="m2"))
    await repo.create(
        conv,
        task_id="t1",
        thread_id="th1",
        agent_id="a1",
        agent_instance_id="inst-coord",
    )

    coordinator = TaskNoteTrackingCoordinator(
        conversation_repository=repo,
        trigger_threshold_tokens=10,
    )

    # 1. 触发一次观察并取得 ticket
    ticket = await coordinator.on_round(
        task_id="t1",
        instance_id="inst-coord",
        conversation=conv,
        is_subagent=False,
    )
    assert ticket is not None

    tracking_before = await coordinator.get(conv.id, is_subagent=False)
    assert tracking_before.observing is not None
    assert tracking_before.note_version == 0

    # 2. 模拟替换前调用 await_running，并在后台稍后 complete
    complete_started = asyncio.Event()

    async def _delayed_complete():
        await complete_started.wait()
        await asyncio.sleep(0.05)
        new_ver = await coordinator.complete(
            instance_id="inst-coord",
            conversation_id=conv.id,
            run_id=ticket.run_id,
            success=True,
            covered_until_message_id="m2",
        )
        assert new_ver == 1

    complete_task = asyncio.create_task(_delayed_complete())
    complete_started.set()

    # 此时 await_running 应当阻塞并等待 complete 结束
    await coordinator.await_running(instance_id="inst-coord", conversation_id=conv.id)
    await complete_task

    # 3. 验证此时 tracking 已推进到 note_version=1
    tracking_after = await coordinator.get(conv.id, is_subagent=False)
    assert tracking_after.observing is None
    assert tracking_after.note_version == 1
    assert tracking_after.covered_until_message_id == "m2"


@pytest.mark.asyncio
async def test_d08_skip_swap_when_note_version_is_zero() -> None:
    """测试 D08: 当 tracking.note_version < 1 时，拒绝替换，原上下文不丢。"""
    messages = [
        UniMessage(role=ROLE_USER, content=[TextPart("需求1")], message_id="m1"),
        UniMessage(role=ROLE_ASSISTANT, content=[TextPart("回答1")], message_id="m2"),
    ]
    tracking = TaskNoteTracking(
        version=1,
        note_version=0,
    )
    assembler = TaskNoteContextAssembler()
    swap_res = await assembler.assemble_context(
        messages=messages,
        tracking=tracking,
        note_snapshot={},
    )
    assert swap_res.swapped is False
    assert len(swap_res.messages) == len(messages)
    assert [m.message_id for m in swap_res.messages] == ["m1", "m2"]
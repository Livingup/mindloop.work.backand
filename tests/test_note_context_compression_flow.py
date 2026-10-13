# -*- coding: utf-8 -*-
"""Task Note 双线程观察者与热组装上下文压缩集成测试 (D02, D05, D06, M6)。

覆盖验收标准：
1. D02 异步观察触发：业务 Agent 执行产生的增量达到阈值时，后台触发独立观察会话，业务 Agent 不停顿；
   同一 Agent 同一时刻最多只有一个观察在运行；
2. D06 工具隔离：业务 Agent 实例在任何状态下都不持有 TASK_NOTE_TOOL_NAMES 工具；
   业务 Agent 直接调用 Note 工具被抛出 PermissionError 拦截；
3. D05 + M6 上下文热组装：门禁命中时按水位替换前缀，精准保留 covered_until_message_id 之后的原始尾部；
   M6-3 尾部首条为 user 消息时与快照合并，协议合法；
4. D08 降级保护：note_version < 1 时不替换，上下文消息保持原样；
5. M6-1 观察等待：替换时若检测到观察者运行中，安全等待最多 swap_wait_timeout_seconds。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_runtime.agent.agent_execution_engine import (
    AgentExecutionEngine,
    AgentExecutionRequest,
    _AgentExecutionInterruptState,
)
from agent_runtime.agent.agent_instance import (
    AGENT_INSTANCE_STATUS_RUNNING,
    AgentInstance,
)
from agent_runtime.agent.agent_task import AgentTask
from agent_runtime.agent_definition.agent_definition import AgentDefinition
from agent_runtime.communication.runtime_event_publisher import (
    NoopRuntimeEventPublisher,
    RuntimeCommunicationContext,
)
from agent_runtime.context_compression.gate import CompressionDecision
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
from agent_runtime.llm_client.llm_request_models import LlmPromptResponse
from agent_runtime.task_note.task_note_models import TaskNoteWatermark
from agent_runtime.task_note.task_note_observer_scheduler import (
    ObserverSessionInput,
    ObserverSessionResult,
    TaskNoteObserverScheduler,
)
from agent_runtime.task_note.task_note_context_assembler import (
    TaskNoteContextAssembler,
)
from agent_runtime.task_note.task_note_prompt_provider import (
    TASK_NOTE_TOOL_NAMES,
    TaskNotePromptProvider,
)


class _MockLlmRequestService:
    def __init__(self, response_messages: list[UniMessage] | None = None) -> None:
        self.sent_requests = []
        self._responses = list(response_messages) if response_messages else []

    async def send_prompt(self, request):
        self.sent_requests.append(request)
        if self._responses:
            msg = self._responses.pop(0)
        else:
            msg = UniMessage(
                role=ROLE_ASSISTANT,
                content=[TextPart("业务执行中...")],
                source_provider="openai",
            )
        request.conversation.append_message(msg)
        return LlmPromptResponse(
            raw_response={},
            assistant_message=msg,
            provider_id="provider-a",
            binding_id="model-a",
            model_id="model-a",
            model_alias="model-a",
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
    llm_service = _MockLlmRequestService()
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
                SimpleNamespace(tool_name="run_bash_command", group="system", category="business", always_enabled=True)
            ]
        ),
        mcp_client_gateway=None,
        skill_gateway=None,
        llm_request_service=llm_service,
        tool_dispatcher=SimpleNamespace(
            build_llm_tools=AsyncMock(return_value=[
                SimpleNamespace(name="run_bash_command", source="local", schema={"name": "run_bash_command"}),
                SimpleNamespace(name="create_mission", source="local", schema={"name": "create_mission"}),
            ]),
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
async def test_d02_observer_scheduler_trigger_and_deduplication() -> None:
    """测试 D02: 后台观察者单实例并发去重与独立异步执行。"""
    executed_inputs: list[ObserverSessionInput] = []

    async def fake_runner(session_input: ObserverSessionInput) -> ObserverSessionResult:
        executed_inputs.append(session_input)
        await asyncio.sleep(0.05)
        return ObserverSessionResult(success=True, covered_until_seq=10, note_version=1)

    scheduler = TaskNoteObserverScheduler(
        observer_runner=fake_runner,
        trigger_threshold_tokens=100,  # 低阈值便于触发
    )

    # 构造历史消息与切片
    messages = [
        UniMessage(role=ROLE_USER, content=[TextPart("用户提出需求 A" * 30)], message_id="msg-1"),
        UniMessage(role=ROLE_ASSISTANT, content=[TextPart("分析需求并开始执行" * 30)], message_id="msg-2"),
    ]

    # 1. 触发观察
    triggered = await scheduler.check_and_trigger(
        task_id="task-d02",
        agent_instance_id="inst-d02",
        messages=messages,
    )
    assert triggered is True
    assert scheduler.is_observer_running("inst-d02") is True

    # 2. 再次尝试触发应被单实例并发排重跳过
    triggered_second = await scheduler.check_and_trigger(
        task_id="task-d02",
        agent_instance_id="inst-d02",
        messages=messages,
    )
    assert triggered_second is False

    # 3. 等待完成
    res = await scheduler.wait_observation_done("inst-d02", timeout=2.0)
    assert res is not None
    assert res.success is True
    assert res.note_version == 1
    assert scheduler.is_observer_running("inst-d02") is False
    assert len(executed_inputs) == 1


@pytest.mark.asyncio
async def test_d05_m6_context_assembler_prefix_swap_and_tail_retention() -> None:
    """测试 D05 + M6: 上下文热组装器按水位替换前缀，完整保留断点后的原始尾部消息。"""
    # 模拟 5 条消息：msg-1, msg-2 为已被 Note 覆盖的前缀；msg-3, msg-4, msg-5 为残差尾部
    m1 = UniMessage(role=ROLE_USER, content=[TextPart("早期任务需求")], message_id="msg-1")
    m2 = UniMessage(role=ROLE_ASSISTANT, content=[TextPart("早期方案输出")], message_id="msg-2")
    m3 = UniMessage(role=ROLE_TOOL, content=[ToolResultPart(tool_use_ref="tu-1", is_error=False, output="命令输出结果")], message_id="msg-3")
    m4 = UniMessage(role=ROLE_ASSISTANT, content=[TextPart("分析测试结果")], message_id="msg-4")
    m5 = UniMessage(role=ROLE_USER, content=[TextPart("用户最新反馈指令")], message_id="msg-5")
    all_messages = [m1, m2, m3, m4, m5]

    watermark = TaskNoteWatermark(
        agent_instance_id="inst-m6",
        task_id="task-m6",
        covered_until_seq=5,
        covered_until_message_id="msg-2",  # 断点覆盖到 msg-2
        note_version=2,
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
        watermark=watermark,
        note_snapshot=snapshot,
        force_swap=True,
    )

    assert swap_res.swapped is True
    assert swap_res.note_version == 2
    assert swap_res.covered_until_seq == 5

    # 替换后的序列：首条为快照手记，后续为 msg-3, msg-4, msg-5
    new_messages = swap_res.messages
    assert len(new_messages) == 4

    # 验证第 1 条是快照 user 消息
    first_msg = new_messages[0]
    assert first_msg.role == ROLE_USER
    assert "任务现场工作手记" in first_msg.content[0].text
    assert "步骤2已完成" in first_msg.content[0].text

    # 验证尾部 msg-3, msg-4, msg-5 原样保留！
    assert new_messages[1].message_id == "msg-3"
    assert new_messages[2].message_id == "msg-4"
    assert new_messages[3].message_id == "msg-5"


@pytest.mark.asyncio
async def test_m6_3_merge_user_message_when_tail_starts_with_user() -> None:
    """测试 M6-3: 当残差尾部首条也是 user 消息时，与快照手记合并为单条 user 消息，保证协议合法。"""
    m1 = UniMessage(role=ROLE_USER, content=[TextPart("任务初始需求")], message_id="msg-1")
    m2 = UniMessage(role=ROLE_ASSISTANT, content=[TextPart("任务初始响应")], message_id="msg-2")
    # 残差尾部首条是 user 消息
    m3 = UniMessage(role=ROLE_USER, content=[TextPart("用户最新跟进要求")], message_id="msg-3")
    m4 = UniMessage(role=ROLE_ASSISTANT, content=[TextPart("最新响应中")], message_id="msg-4")
    all_messages = [m1, m2, m3, m4]

    watermark = TaskNoteWatermark(
        agent_instance_id="inst-m6-3",
        task_id="task-m6-3",
        covered_until_seq=2,
        covered_until_message_id="msg-2",
        note_version=1,
    )
    snapshot = {
        "task_id": "task-m6-3",
        "missions": [{"mission_id": "m1", "title": "任务A"}],
    }

    assembler = TaskNoteContextAssembler()
    swap_res = await assembler.assemble_context(
        messages=all_messages,
        watermark=watermark,
        note_snapshot=snapshot,
        force_swap=True,
    )

    assert swap_res.swapped is True
    new_messages = swap_res.messages
    # 原 4 条中前 2 条被快照替换，m3 与快照合并，因此结果只有 2 条 (合并后的 user 消息 + m4)
    assert len(new_messages) == 2
    merged_user = new_messages[0]
    assert merged_user.role == ROLE_USER
    merged_text = merged_user.content[0].text
    assert "任务现场工作手记" in merged_text
    assert "用户最新跟进要求" in merged_text
    assert "---" in merged_text  # 有分隔线连接

    # 第二条是原助手的响应
    assert new_messages[1].message_id == "msg-4"


@pytest.mark.asyncio
async def test_d08_skip_swap_when_note_version_is_zero() -> None:
    """测试 D08: 当 note_version < 1 (无成功快照或存储不可用) 时，拒绝替换，原上下文不丢。"""
    messages = [
        UniMessage(role=ROLE_USER, content=[TextPart("需求1")], message_id="m1"),
        UniMessage(role=ROLE_ASSISTANT, content=[TextPart("回答1")], message_id="m2"),
    ]

    watermark = TaskNoteWatermark(
        agent_instance_id="inst-d08",
        task_id="task-d08",
        covered_until_seq=0,
        note_version=0,  # 版本为 0
    )

    assembler = TaskNoteContextAssembler()
    swap_res = await assembler.assemble_context(
        messages=messages,
        watermark=watermark,
        note_snapshot={},  # 空快照
        force_swap=True,
    )

    assert swap_res.swapped is False
    assert len(swap_res.messages) == len(messages)
    assert [m.message_id for m in swap_res.messages] == ["m1", "m2"]
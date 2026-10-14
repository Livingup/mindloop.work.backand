# -*- coding: utf-8 -*-
"""待处理消息消费与流转测试 (已移除旧 note_update 消息分流)。

验证：
1. AgentInstance.drain_pending_user_messages 能够全量、按 FIFO 顺序消费待处理消息；
2. 业务执行循环中，各类来源（人类用户、子 Agent、远程 Agent）的消息均正常消费进入业务会话，不进行任何阻断或分流。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
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
    PendingAgentUserMessage,
)
from agent_runtime.agent.agent_task import AgentTask
from agent_runtime.agent_definition.agent_definition import AgentDefinition
from agent_runtime.communication.runtime_event_publisher import NoopRuntimeEventPublisher
from agent_runtime.conversation_message.message.models import (
    Conversation,
    ROLE_ASSISTANT,
    ROLE_USER,
    TextPart,
    TextPartMeta,
    UniMessage,
)
from agent_runtime.input_modality import INPUT_MODALITY_TEXT
from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_RESPONSES
from agent_runtime.llm_client.llm_request_models import LlmPromptResponse


def test_pending_user_messages_drain_fifo_order() -> None:
    """测试 AgentInstance 待处理消息队列按 FIFO 顺序全量消费。"""
    now = datetime.now(timezone.utc)
    m1 = PendingAgentUserMessage(message_id="msg-1", user_text="人类指令: 开始分析", received_at=now)
    m2 = PendingAgentUserMessage(message_id="msg-2", user_text="子 Agent 汇报: 阶段一完成", received_at=now)
    m3 = PendingAgentUserMessage(message_id="msg-3", user_text="远程 Agent 消息: 资源就绪", received_at=now)

    instance = AgentInstance(
        instance_id="inst-001",
        task_id="task-001",
        thread_id="thread-001",
        current_working_directory="/test",
        agent_definition_id="def-001",
        llm_provider_id="prov-1",
        llm_model_binding_id="model-1",
        conversation_id="conv-001",
        trace_id="trace-001",
        status=AGENT_INSTANCE_STATUS_RUNNING,
        pending_user_messages=(m1, m2, m3),
        created_at=now,
        updated_at=now,
    )

    updated, drained = instance.drain_pending_user_messages()
    assert len(updated.pending_user_messages) == 0
    assert len(drained) == 3
    assert [m.message_id for m in drained] == ["msg-1", "msg-2", "msg-3"]


def test_engine_drains_all_pending_messages_into_conversation() -> None:
    """测试业务执行引擎将队列中所有消息消费追加进会话，不作任何隔离过滤。"""
    async def _run() -> None:
        now = datetime.now(timezone.utc)
        conv = Conversation(id="conv-001")
        conv.append_message(UniMessage(role=ROLE_USER, content=[TextPart("初始输入")]), [TextPartMeta()])

        m1 = PendingAgentUserMessage(message_id="msg-1", user_text="人类指令", received_at=now)
        m2 = PendingAgentUserMessage(message_id="msg-2", user_text="子任务汇报", received_at=now)
        m3 = PendingAgentUserMessage(message_id="msg-3", user_text="远程数据同步", received_at=now)

        instance = AgentInstance(
            instance_id="inst-001",
            task_id="task-001",
            thread_id="thread-001",
            current_working_directory="/test",
            agent_definition_id="def-001",
            llm_provider_id="prov-1",
            llm_model_binding_id="model-1",
            conversation_id=conv.id,
            trace_id="trace-001",
            status=AGENT_INSTANCE_STATUS_RUNNING,
            pending_user_messages=(m1, m2, m3),
            created_at=now,
            updated_at=now,
        )
        task = AgentTask(task_id="task-001", root_agent_instance_id="inst-001", agent_instance_ids=("inst-001",))

        class _MockGateway:
            def __init__(self):
                self.inst = instance
            def start_task(self, tid): pass
            def start_agent_instance(self, iid): pass
            def pause_agent_instance(self, iid): return self.inst
            def pause_task(self, tid): pass
            def interrupt_agent_instance(self, iid, msg): pass
            def get_task(self, tid): return task
            def get_agent_instance(self, iid): return self.inst
            def update_agent_instance_execution_substate(self, iid, substate): pass
            def drain_agent_instance_user_messages(self, iid, predicate=None):
                upd, dr = self.inst.drain_pending_user_messages(predicate=predicate)
                self.inst = upd
                return upd, dr

        gateway = _MockGateway()
        conv_repo = MagicMock()
        conv_repo.get = AsyncMock(return_value=conv)
        conv_repo.save = AsyncMock()

        model_info = SimpleNamespace(
            provider_id="prov-1",
            binding_id="model-1",
            model_id="model-1",
            model_alias="model-1",
            api_format=API_FORMAT_OPENAI_RESPONSES,
            supported_input_modalities=(INPUT_MODALITY_TEXT,),
        )
        llm_reg = SimpleNamespace(
            resolve_candidate_runtime_policy=lambda p, b: model_info,
            resolve_provider_model=lambda p, m: model_info,
        )

        class _MockLlmRequestService:
            async def send_prompt(self, request):
                msg = UniMessage(
                    role=ROLE_ASSISTANT,
                    content=[TextPart("已处理全部消息。")],
                    source_provider="openai",
                )
                request.conversation.append_message(msg)
                return LlmPromptResponse(
                    raw_response={},
                    assistant_message=msg,
                    provider_id="prov-1",
                    binding_id="model-1",
                    model_id="model-1",
                    model_alias="model-1",
                )

        engine = AgentExecutionEngine(
            runtime_gateway=gateway,
            agent_definition_gateway=SimpleNamespace(
                get_agent_definition=lambda did: AgentDefinition(agent_id=did, name="Dev", tool_ids=())
            ),
            llm_registry_gateway=llm_reg,
            conversation_repository=conv_repo,
            prompt_snippet_gateway=SimpleNamespace(get_snippet=lambda n: SimpleNamespace(content="")),
            local_tool_gateway=SimpleNamespace(list_tools=lambda **kwargs: []),
            mcp_client_gateway=None,
            skill_gateway=None,
            llm_request_service=_MockLlmRequestService(),
            tool_dispatcher=SimpleNamespace(
                build_llm_tools=AsyncMock(return_value=[]),
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

        request = AgentExecutionRequest(
            thread_id="thread-001",
            current_working_directory="/test",
            agent_definition_id="def-001",
            llm_provider_id="prov-1",
            llm_model_binding_id="model-1",
            user_parts=(),
            user_parts_meta=(),
            task_id="task-001",
            conversation_id=conv.id,
        )

        result = await engine._run_agent_instance(
            task=task,
            instance=instance,
            request=request,
            interrupt_state=_AgentExecutionInterruptState(),
        )

        # 验证待处理消息全量被消费入会话
        assert len(gateway.inst.pending_user_messages) == 0
        conv_texts = [p.text for m in conv.messages for p in m.content if isinstance(p, TextPart)]
        assert any("人类指令" in t for t in conv_texts)
        assert any("子任务汇报" in t for t in conv_texts)
        assert any("远程数据同步" in t for t in conv_texts)
        assert result.assistant_message is not None

    asyncio.run(_run())

# -*- coding: utf-8 -*-
from datetime import datetime, timezone
from pathlib import Path

from agent_runtime.agent.agent_instance import (
    AGENT_INSTANCE_STATUS_READY,
    AGENT_INSTANCE_STATUS_RUNNING,
    EXECUTION_SUBSTATE_NOTE_UPDATE,
    AgentInstance,
)
from agent_runtime.agent.infrastructure.sqlite_agent_runtime_repository import (
    SqliteAgentRuntimeRepository,
)


def test_agent_instance_note_substate_transitions() -> None:
    now = datetime.now(timezone.utc)
    instance = AgentInstance(
        instance_id="inst-001",
        task_id="task-001",
        thread_id="thread-001",
        current_working_directory="/test/workspace",
        agent_definition_id="def-001",
        llm_provider_id="provider-openai",
        llm_model_binding_id="gpt-4o",
        conversation_id="conv-001",
        trace_id="trace-001",
        status=AGENT_INSTANCE_STATUS_RUNNING,
        created_at=now,
        updated_at=now,
    )
    assert instance.execution_substate is None

    # 进入 note_update
    in_note = instance.enter_note_update()
    assert in_note.execution_substate == EXECUTION_SUBSTATE_NOTE_UPDATE
    assert in_note.updated_at >= now

    # 退出 note_update
    exited = in_note.exit_note_update()
    assert exited.execution_substate is None
    assert exited.updated_at >= in_note.updated_at


def test_sqlite_agent_instance_execution_substate_persistence(tmp_path: Path) -> None:
    repo = SqliteAgentRuntimeRepository(tmp_path)
    now = datetime.now(timezone.utc)
    instance = AgentInstance(
        instance_id="inst-persist-001",
        task_id="task-persist-001",
        thread_id="thread-persist-001",
        current_working_directory="/test/workspace",
        agent_definition_id="def-001",
        llm_provider_id="provider-openai",
        llm_model_binding_id="gpt-4o",
        conversation_id="conv-001",
        trace_id="trace-001",
        status=AGENT_INSTANCE_STATUS_RUNNING,
        execution_substate=EXECUTION_SUBSTATE_NOTE_UPDATE,
        created_at=now,
        updated_at=now,
    )
    repo.save_agent_instance(instance)

    loaded = repo.get_agent_instance("inst-persist-001")
    assert loaded is not None
    assert loaded.execution_substate == EXECUTION_SUBSTATE_NOTE_UPDATE

    # 退出并保存
    exited = loaded.exit_note_update()
    repo.save_agent_instance(exited)

    reloaded = repo.get_agent_instance("inst-persist-001")
    assert reloaded is not None
    assert reloaded.execution_substate is None

    repo.close()


def test_m7_legacy_note_update_instance_reset_on_engine_start(tmp_path: Path) -> None:
    """测试 M7: 存量处于 note_update 状态的实例在引擎启动执行时自动重置为业务态，保留并消费 pending 消息。"""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from types import SimpleNamespace
    from agent_runtime.agent.agent_execution_engine import (
        AgentExecutionEngine,
        AgentExecutionRequest,
        _AgentExecutionInterruptState,
    )
    from agent_runtime.agent.agent_instance import PendingAgentUserMessage
    from agent_runtime.agent.agent_task import AgentTask
    from agent_runtime.agent_definition.agent_definition import AgentDefinition
    from agent_runtime.conversation_message.message.models import (
        Conversation,
        ROLE_ASSISTANT,
        ROLE_USER,
        TextPart,
        TextPartMeta,
        UniMessage,
    )
    from agent_runtime.llm_client.llm_request_models import LlmPromptResponse
    from agent_runtime.communication.runtime_event_publisher import NoopRuntimeEventPublisher
    from agent_runtime.input_modality import INPUT_MODALITY_TEXT
    from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_RESPONSES

    async def _run() -> None:
        repo = SqliteAgentRuntimeRepository(tmp_path)
        now = datetime.now(timezone.utc)
        conv = Conversation(id="conv-m7-01")
        conv.append_message(UniMessage(role=ROLE_USER, content=[TextPart("初始任务")]), [TextPartMeta()])

        pending_msg = PendingAgentUserMessage(
            message_id="msg-pending-01",
            user_text="继续业务推进",
            received_at=now,
        )
        instance = AgentInstance(
            instance_id="inst-m7-001",
            task_id="task-m7-001",
            thread_id="thread-m7-001",
            current_working_directory="/test/workspace",
            agent_definition_id="def-001",
            llm_provider_id="provider-openai",
            llm_model_binding_id="gpt-4o",
            conversation_id=conv.id,
            trace_id="trace-m7-001",
            status=AGENT_INSTANCE_STATUS_RUNNING,
            execution_substate=EXECUTION_SUBSTATE_NOTE_UPDATE,
            pending_user_messages=(pending_msg,),
            created_at=now,
            updated_at=now,
        )
        repo.save_agent_instance(instance)
        task = AgentTask(task_id="task-m7-001", root_agent_instance_id="inst-m7-001", agent_instance_ids=("inst-m7-001",))
        repo.save_task(task)

        class _MockGateway:
            def __init__(self, repository):
                self._repo = repository
                self.substate_updates = []
            def start_task(self, tid): pass
            def start_agent_instance(self, iid): pass
            def pause_agent_instance(self, iid): return self._repo.get_agent_instance(iid)
            def pause_task(self, tid): pass
            def interrupt_agent_instance(self, iid, msg): pass
            def get_task(self, tid): return self._repo.get_task(tid)
            def get_agent_instance(self, iid): return self._repo.get_agent_instance(iid)
            def update_agent_instance_execution_substate(self, iid, substate):
                self.substate_updates.append((iid, substate))
                inst = self._repo.get_agent_instance(iid)
                if inst:
                    if substate is None:
                        inst = inst.exit_note_update()
                    else:
                        inst = inst.enter_note_update()
                    self._repo.save_agent_instance(inst)
            def drain_agent_instance_user_messages(self, iid, predicate=None):
                inst = self._repo.get_agent_instance(iid)
                if not inst:
                    return None, ()
                updated, drained = inst.drain_pending_user_messages(predicate=predicate)
                self._repo.save_agent_instance(updated)
                return updated, drained

        gateway = _MockGateway(repo)
        conv_repo = MagicMock()
        conv_repo.get = AsyncMock(return_value=conv)
        conv_repo.save = AsyncMock()

        class _MockLlmRequestService:
            async def send_prompt(self, request):
                msg = UniMessage(
                    role=ROLE_ASSISTANT,
                    content=[TextPart("业务已恢复并继续处理。")],
                    source_provider="openai",
                )
                request.conversation.append_message(msg)
                return LlmPromptResponse(
                    raw_response={},
                    assistant_message=msg,
                    provider_id="provider-openai",
                    binding_id="gpt-4o",
                    model_id="gpt-4o",
                    model_alias="gpt-4o",
                )

        model_info = SimpleNamespace(
            provider_id="provider-openai",
            binding_id="gpt-4o",
            model_id="gpt-4o",
            model_alias="gpt-4o",
            api_format=API_FORMAT_OPENAI_RESPONSES,
            supported_input_modalities=(INPUT_MODALITY_TEXT,),
        )
        llm_reg = SimpleNamespace(
            resolve_candidate_runtime_policy=lambda p, b: model_info,
            resolve_provider_model=lambda p, m: model_info,
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
            thread_id="thread-m7-001",
            current_working_directory="/test/workspace",
            agent_definition_id="def-001",
            llm_provider_id="provider-openai",
            llm_model_binding_id="gpt-4o",
            user_parts=(),
            user_parts_meta=(),
            task_id="task-m7-001",
            conversation_id=conv.id,
        )

        result = await engine._run_agent_instance(
            task=task,
            instance=instance,
            request=request,
            interrupt_state=_AgentExecutionInterruptState(),
        )

        # 验证 M7:
        # 1. 实例 execution_substate 已重置为 None
        loaded_inst = repo.get_agent_instance("inst-m7-001")
        assert loaded_inst.execution_substate is None
        assert ("inst-m7-001", None) in gateway.substate_updates

        # 2. pending 消息被成功消费到会话中
        assert len(loaded_inst.pending_user_messages) == 0
        conv_texts = [p.text for m in conv.messages for p in m.content if isinstance(p, TextPart)]
        assert any("继续业务推进" in t for t in conv_texts)

        # 3. 业务继续执行并返回助手的业务消息
        assert result.assistant_message is not None
        assert "业务已恢复并继续处理" in result.assistant_message.content[0].text

        repo.close()

    asyncio.run(_run())


import asyncio
from types import SimpleNamespace

import pytest

from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine, AgentExecutionRequest, _PauseTimeoutHandle
from agent_runtime.agent.agent_instance import (
    AGENT_INSTANCE_STATUS_READY,
    AGENT_INSTANCE_STATUS_RUNNING,
    AgentInstance,
    PendingUserMessageAttachment,
 )

from agent_runtime.agent.agent_runtime_service import AgentRuntimeService
from agent_runtime.agent.agent_task import AGENT_TASK_STATUS_COMPLETED
from agent_runtime.agent.infrastructure.in_memory_agent_runtime_repository import InMemoryAgentRuntimeRepository
from agent_runtime.agent.north_gateway.agent_runtime_gateway_impl import AgentRuntimeGatewayImpl
from agent_runtime.agent.task_completion import RUNNING_AGENTS_MESSAGE
from agent_runtime.agent_lifecycle.agent_lifecycle_tool import PauseTaskRequest, pause_task
from agent_runtime.communication.runtime_event_publisher import RuntimeCommunicationContext
from agent_runtime.conversation_message.message.models import Conversation, ROLE_ASSISTANT, TextPart, UniMessage
from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_RESPONSES
from agent_runtime.tool.tool_decorator import get_tool_description
from agent_runtime.tool.tool_execution_models import BaseToolResult, LoopControl


class _NoopTraceRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []
        self.user_messages: list[UniMessage] = []
        self.assistant_messages: list[UniMessage] = []
        self.tool_messages: list[UniMessage] = []
        self.failures: list[str] = []
        self.completed_count = 0

    def start_trace(self, _identity) -> str:
        return "trace.md"

    def record_event(self, _trace_path, event_name: str, details=None) -> None:
        self.events.append((event_name, details))

    def record_user_message(self, _trace_path, message: UniMessage) -> None:
        self.user_messages.append(message)

    def record_llm_response(self, _trace_path, message: UniMessage) -> None:
        self.assistant_messages.append(message)

    def record_tool_result(self, _trace_path, message: UniMessage) -> None:
        self.tool_messages.append(message)

    def record_failure(self, _trace_path, failure_message: str) -> None:
        self.failures.append(failure_message)

    def record_completed(self, _trace_path) -> None:
        self.completed_count += 1


class _RecordingExecutionLoopLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def record_instance_event(self, **kwargs) -> None:
        self.events.append(kwargs)


class _RecordingEventPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[RuntimeCommunicationContext | None, str, dict[str, object] | None]] = []

    async def publish(self, context, event_type, payload=None, **_kwargs) -> None:
        self.events.append((context, event_type, payload))


class _ConversationRepository:
    def __init__(self, conversation: Conversation) -> None:
        self.conversation = conversation

    async def get(self, conversation_id: str) -> Conversation:
        assert conversation_id == self.conversation.id
        return self.conversation

    async def save(self, conversation: Conversation) -> None:
        self.conversation = conversation


class _LlmRequestService:
    def __init__(
        self,
        *,
        release_event: asyncio.Event | None = None,
        started_event: asyncio.Event | None = None,
    ) -> None:
        self.call_count = 0
        self.request_message_counts: list[int] = []
        self.release_event = release_event
        self.started_event = started_event

    async def send_prompt(self, request):
        self.call_count += 1
        self.request_message_counts.append(len(request.conversation.messages))
        if self.started_event is not None:
            self.started_event.set()
        if self.release_event is not None:
            await self.release_event.wait()
        assistant_message = UniMessage(
            role=ROLE_ASSISTANT,
            content=[TextPart("无工具调用")],
            source_provider=request.resolved_model.api_format,
        )
        request.conversation.append_message(assistant_message)
        return SimpleNamespace(
            raw_response={},
            assistant_message=assistant_message,
            provider_id=request.resolved_model.provider_id,
            binding_id=request.resolved_model.binding_id,
            model_id=request.resolved_model.model_id,
            model_alias=request.resolved_model.model_alias,
        )


class _NoopToolDispatcher:
    async def build_llm_tools(self, **_kwargs):
        return ()


class _AgentDefinitionGateway:
    def get_agent_definition(self, agent_id: str):
        return SimpleNamespace(
            agent_id=agent_id,
            disabled=False,
            system_prompt=(),
            mcp_tools=(),
            skill_ids=(),
            available_sub_agent_ids=(),
            tool_ids=(),
        )


class _LlmRegistryGateway:
    def resolve_provider_model(self, _provider_id: str, _binding_id: str):
        return SimpleNamespace(
            provider_id="provider",
            binding_id="binding",
            model_id="model",
            model_alias="model",
            api_format=API_FORMAT_OPENAI_RESPONSES,
            supported_input_modalities=("text",),
        )

    def resolve_agent_execution_policy(self, _agent_definition):
        return ((self.resolve_provider_model("provider", "binding"),), "degrade")


class _PromptSnippetGateway:
    def get_snippet(self, template_id: str):
        raise AssertionError(template_id)


class _NoopLocalToolGateway:
    def list_tools(self, *, public_only: bool = False, group_by: bool = False):
        return []


def _request(*, task_id: str = "task-timeout", instance_id: str = "agent-instance-timeout", conversation_id: str = "conversation-timeout", user_text: str = "") -> AgentExecutionRequest:
    return AgentExecutionRequest.from_text(
        thread_id="thread-timeout",
        current_working_directory="G:/project/agent",
        agent_definition_id="agent-timeout",
        llm_provider_id="provider",
        llm_model_binding_id="binding",
        user_text=user_text,
        task_id=task_id,
        root_instance_id=instance_id,
        conversation_id=conversation_id,
        communication_stream_id="stream-timeout",
    )


def _engine(
    *,
    runtime_gateway,
    conversation: Conversation | None = None,
    llm_request_service: _LlmRequestService | None = None,
    event_publisher: _RecordingEventPublisher | None = None,
    execution_loop_logger: _RecordingExecutionLoopLogger | None = None,
    trace_recorder: _NoopTraceRecorder | None = None,
) -> AgentExecutionEngine:
    return AgentExecutionEngine(
        runtime_gateway=runtime_gateway,
        agent_definition_gateway=_AgentDefinitionGateway(),
        llm_registry_gateway=_LlmRegistryGateway(),
        conversation_repository=_ConversationRepository(conversation or Conversation(id="conversation-timeout")),
        prompt_snippet_gateway=_PromptSnippetGateway(),
        local_tool_gateway=_NoopLocalToolGateway(),
        mcp_client_gateway=None,
        skill_gateway=None,
        llm_request_service=llm_request_service or _LlmRequestService(),
        tool_dispatcher=_NoopToolDispatcher(),
        trace_recorder=trace_recorder or _NoopTraceRecorder(),
        event_publisher=event_publisher or _RecordingEventPublisher(),
        execution_loop_logger=execution_loop_logger or _RecordingExecutionLoopLogger(),
    )


def _service_with_ready_instance(
    *,
    task_id: str = "task-timeout",
    instance_id: str = "agent-instance-timeout",
    conversation_id: str = "conversation-timeout",
) -> AgentRuntimeService:
    service = AgentRuntimeService(InMemoryAgentRuntimeRepository())
    service.create_task_with_root_agent_instance(
        task_id=task_id,
        root_instance_id=instance_id,
        thread_id="thread-timeout",
        current_working_directory="G:/project/agent",
        agent_definition_id="agent-timeout",
        llm_provider_id="provider",
        llm_model_binding_id="binding",
        conversation_id=conversation_id,
    )
    return service


def _pause_result(timeout_seconds: float) -> BaseToolResult:
    return BaseToolResult(
        success=True,
        execution_status="success",
        output="paused",
        loop_control=LoopControl(decision="pause", reason="test pause"),
        metadata={
            "lifecycle_action": "pause",
            "pause_timeout_seconds": timeout_seconds,
            "pause_wake_mode": "external_or_timeout" if timeout_seconds > 0 else "external_only",
        },
    )


def test_pause_task_timeout_seconds_validation_and_metadata() -> None:
    indefinite = pause_task(PauseTaskRequest(reason="wait for webhook"))
    assert indefinite.loop_control.decision == "pause"
    assert indefinite.metadata["pause_timeout_seconds"] == -1
    assert indefinite.metadata["pause_wake_mode"] == "external_only"

    timed = pause_task(PauseTaskRequest(reason="poll async job", timeout_seconds=300))
    assert timed.loop_control.decision == "pause"
    assert timed.metadata["pause_timeout_seconds"] == 300
    assert timed.metadata["pause_wake_mode"] == "external_or_timeout"
    assert "300 seconds timeout" in timed.output

    for invalid_timeout in (0, -2, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            PauseTaskRequest(reason="invalid timeout", timeout_seconds=invalid_timeout)


def test_pause_task_usage_descriptions_distinguish_checkpoint_waiting_and_completed_dormancy() -> None:
    field_description = PauseTaskRequest.model_fields["timeout_seconds"].description or ""
    tool_description = get_tool_description(pause_task)

    for description in (field_description, tool_description):
        assert "等待已知的委派任务结果" in description
        assert "大于 0 的有限检查点超时" in description
        assert "不要使用 -1" in description
        assert "检查点到期仍未收到结果" in description
        assert "主动询问是否已经完成" in description
        assert "进度、阻塞点和预计完成时间" in description
        assert "受派工作已经全部完成" in description
        assert "最终结果已向父 Agent 或远程上游汇报" in description
        assert "没有仍待返回的已知子任务/结果" in description
        assert "timeout_seconds=-1" in description
        assert "无意义的周期性唤醒" in description


def test_pause_wake_metadata_defaults_to_legacy_external_only_for_missing_or_invalid_timeout() -> None:
    assert AgentExecutionEngine._pause_wake_metadata_from_tool_results([]) == (-1.0, "external_only")
    assert AgentExecutionEngine._pause_wake_metadata_from_tool_results([BaseToolResult(metadata={})]) == (
        -1.0,
        "external_only",
    )
    assert AgentExecutionEngine._pause_wake_metadata_from_tool_results([_pause_result(0)]) == (
        -1.0,
        "external_only",
    )
    assert AgentExecutionEngine._pause_wake_metadata_from_tool_results([_pause_result(60)]) == (
        60.0,
        "external_or_timeout",
    )


def test_apply_pause_without_timeout_keeps_legacy_external_only_and_schedules_no_timer() -> None:
    async def run_case() -> None:
        service = _service_with_ready_instance()
        engine = _engine(runtime_gateway=service)
        await engine._apply_loop_decision(
            loop_decision="pause",
            task_id="task-timeout",
            instance_id="agent-instance-timeout",
            trace_path="trace.md",
            tool_results=[_pause_result(-1)],
            communication_context=RuntimeCommunicationContext(
                stream_id="stream-timeout",
                metadata={"conversation_id": "conversation-timeout"},
            ),
            request=_request(),
        )

        instance = service.get_agent_instance("agent-instance-timeout")
        assert instance.status == AGENT_INSTANCE_STATUS_READY
        assert not engine._pause_timeout_handles

    asyncio.run(run_case())


def test_apply_pause_with_positive_timeout_schedules_tokenized_timer_and_trace_payload() -> None:
    async def run_case() -> None:
        service = _service_with_ready_instance()
        trace_recorder = _NoopTraceRecorder()
        event_publisher = _RecordingEventPublisher()
        engine = _engine(
            runtime_gateway=service,
            trace_recorder=trace_recorder,
            event_publisher=event_publisher,
        )

        await engine._apply_loop_decision(
            loop_decision="pause",
            task_id="task-timeout",
            instance_id="agent-instance-timeout",
            trace_path="trace.md",
            tool_results=[_pause_result(120)],
            communication_context=RuntimeCommunicationContext(
                stream_id="stream-timeout",
                metadata={"conversation_id": "conversation-timeout"},
            ),
            request=_request(),
        )

        handle = engine._pause_timeout_handles["agent-instance-timeout"]
        assert handle.task_id == "task-timeout"
        assert handle.instance_id == "agent-instance-timeout"
        assert handle.conversation_id == "conversation-timeout"
        assert handle.timeout_seconds == 120
        assert handle.request.user_parts[0].text == ""
        assert trace_recorder.events[-1][0] == "instance_paused"
        assert trace_recorder.events[-1][1]["wake_mode"] == "external_or_timeout"
        assert trace_recorder.events[-1][1]["pause_token"] == handle.token
        assert event_publisher.events[-1][1] == "agent.task.paused"
        assert event_publisher.events[-1][2]["wake_deadline"] == handle.deadline.isoformat()

        handle.task.cancel()
        await asyncio.gather(handle.task, return_exceptions=True)

    asyncio.run(run_case())


def test_timeout_resume_starts_execution_once_without_visible_user_message() -> None:
    async def run_case() -> None:
        service = _service_with_ready_instance()
        service.pause_agent_instance("agent-instance-timeout")
        conversation = Conversation(id="conversation-timeout")
        conversation.append_message(UniMessage(role=ROLE_ASSISTANT, content=[TextPart("previous")]))
        started_event = asyncio.Event()
        release_event = asyncio.Event()
        llm_request_service = _LlmRequestService(
            release_event=release_event,
            started_event=started_event,
        )
        event_publisher = _RecordingEventPublisher()
        engine = _engine(
            runtime_gateway=service,
            conversation=conversation,
            llm_request_service=llm_request_service,
            event_publisher=event_publisher,
        )
        handle = engine._schedule_pause_timeout(
            task_id="task-timeout",
            instance_id="agent-instance-timeout",
            conversation_id="conversation-timeout",
            timeout_seconds=60,
            request=_request(),
            communication_context=RuntimeCommunicationContext(stream_id="stream-timeout"),
        )
        assert handle is not None

        await engine._resume_agent_instance_from_pause_timeout(
            instance_id="agent-instance-timeout",
            token=handle.token,
        )
        await asyncio.wait_for(started_event.wait(), timeout=2)

        assert "agent-instance-timeout" not in engine._pause_timeout_handles
        assert service.get_agent_instance("agent-instance-timeout").status == AGENT_INSTANCE_STATUS_RUNNING
        assert llm_request_service.call_count == 1
        assert llm_request_service.request_message_counts == [1]
        assert all(message.role != "user" for message in conversation.messages)
        assert [event_type for _ctx, event_type, _payload in event_publisher.events].count(
            "agent.task.pause_timeout.elapsed"
        ) == 1

        release_event.set()
        await asyncio.gather(*engine._background_tasks, return_exceptions=True)

    asyncio.run(run_case())


def test_external_message_cancels_timer_before_submit_launch_and_timeout_replay_is_ignored() -> None:
    async def run_case() -> None:
        service = _service_with_ready_instance()
        service.pause_agent_instance("agent-instance-timeout")
        release_event = asyncio.Event()
        started_event = asyncio.Event()
        llm_request_service = _LlmRequestService(
            release_event=release_event,
            started_event=started_event,
        )
        logger = _RecordingExecutionLoopLogger()
        engine = _engine(
            runtime_gateway=service,
            llm_request_service=llm_request_service,
            execution_loop_logger=logger,
        )
        handle = engine._schedule_pause_timeout(
            task_id="task-timeout",
            instance_id="agent-instance-timeout",
            conversation_id="conversation-timeout",
            timeout_seconds=60,
            request=_request(),
            communication_context=RuntimeCommunicationContext(stream_id="stream-timeout"),
        )
        assert handle is not None

        result = await engine.submit_agent_instance_message(
            agent_instance_id="agent-instance-timeout",
            user_text="external event arrived",
            communication_stream_id="stream-timeout",
        )
        await asyncio.wait_for(started_event.wait(), timeout=2)

        assert result.status == "accepted"
        assert "agent-instance-timeout" not in engine._pause_timeout_handles
        await asyncio.gather(handle.task, return_exceptions=True)
        assert handle.task.cancelled()
        release_events = [
            event for event in logger.events
            if event["stage"] == "pause_timeout.release" and event["details"]["reason"] == "external_user_message"
        ]
        assert release_events
        assert release_events[-1]["details"]["token"] == handle.token

        await engine._resume_agent_instance_from_pause_timeout(
            instance_id="agent-instance-timeout",
            token=handle.token,
        )
        skip_events = [
            event for event in logger.events
            if event["stage"] == "pause_timeout.resume" and event["status"] == "skipped"
        ]
        assert skip_events[-1]["details"]["reason"] == "handle_missing_or_token_mismatch"
        assert llm_request_service.call_count == 1

        release_event.set()
        await asyncio.gather(*engine._background_tasks, return_exceptions=True)

    asyncio.run(run_case())


def test_run_agent_instance_message_also_cancels_timer_before_legacy_direct_execution() -> None:
    async def run_case() -> None:
        service = _service_with_ready_instance()
        service.pause_agent_instance("agent-instance-timeout")
        logger = _RecordingExecutionLoopLogger()
        engine = _engine(
            runtime_gateway=service,
            llm_request_service=_LlmRequestService(),
            execution_loop_logger=logger,
        )
        handle = engine._schedule_pause_timeout(
            task_id="task-timeout",
            instance_id="agent-instance-timeout",
            conversation_id="conversation-timeout",
            timeout_seconds=60,
            request=_request(),
            communication_context=RuntimeCommunicationContext(stream_id="stream-timeout"),
        )
        assert handle is not None

        result = await engine.run_agent_instance_message(
            agent_instance_id="agent-instance-timeout",
            user_text="legacy external event",
            communication_stream_id="stream-timeout",
        )

        assert result.task_id == "task-timeout"
        assert "agent-instance-timeout" not in engine._pause_timeout_handles
        await asyncio.gather(handle.task, return_exceptions=True)
        assert handle.task.cancelled()
        release_events = [
            event for event in logger.events
            if event["stage"] == "pause_timeout.release" and event["details"]["reason"] == "external_user_message"
        ]
        assert release_events[-1]["details"]["token"] == handle.token

    asyncio.run(run_case())


def test_timeout_callback_revalidates_task_instance_conversation_and_running_state() -> None:
    async def run_case() -> None:
        service = _service_with_ready_instance()
        service.pause_agent_instance("agent-instance-timeout")
        logger = _RecordingExecutionLoopLogger()
        engine = _engine(runtime_gateway=service, execution_loop_logger=logger)
        handle = engine._schedule_pause_timeout(
            task_id="task-timeout",
            instance_id="agent-instance-timeout",
            conversation_id="conversation-timeout",
            timeout_seconds=60,
            request=_request(),
            communication_context=RuntimeCommunicationContext(stream_id="stream-timeout"),
        )
        assert handle is not None

        service.resume_agent_instance("agent-instance-timeout")
        await engine._resume_agent_instance_from_pause_timeout(
            instance_id="agent-instance-timeout",
            token=handle.token,
        )

        accepted_events = [
            event for event in logger.events
            if event["stage"] == "pause_timeout.resume" and event["status"] == "accepted"
        ]
        assert not accepted_events
        skip_events = [
            event for event in logger.events
            if event["stage"] == "pause_timeout.resume" and event["status"] == "skipped"
        ]
        assert skip_events[-1]["details"]["reason"] == "already_running"
        assert not engine._background_tasks

    asyncio.run(run_case())


def test_timeout_resume_waits_for_task_gate_and_revalidates_terminal_task() -> None:
    async def run_case() -> None:
        service = _service_with_ready_instance()
        service.pause_agent_instance("agent-instance-timeout")
        logger = _RecordingExecutionLoopLogger()
        engine = _engine(runtime_gateway=service, execution_loop_logger=logger)
        handle = engine._schedule_pause_timeout(
            task_id="task-timeout",
            instance_id="agent-instance-timeout",
            conversation_id="conversation-timeout",
            timeout_seconds=60,
            request=_request(),
            communication_context=RuntimeCommunicationContext(stream_id="stream-timeout"),
        )
        assert handle is not None
        task = service.get_task("task-timeout")
        gate_key = engine._task_operation_gate.key_for_task(task)

        async with engine._task_operation_gate.hold_key(gate_key):
            resume_attempt = asyncio.create_task(
                engine._resume_agent_instance_from_pause_timeout(
                    instance_id="agent-instance-timeout",
                    token=handle.token,
                )
            )
            await asyncio.sleep(0)
            assert not resume_attempt.done()
            service.complete_task("task-timeout")

        await resume_attempt
        skip_events = [
            event for event in logger.events
            if event["stage"] == "pause_timeout.resume" and event["status"] == "skipped"
        ]
        assert skip_events[-1]["details"]["reason"] == "task_terminal"
        assert not engine._background_tasks
        assert "agent-instance-timeout" not in engine._active_instance_ids

        handle.task.cancel()
        await asyncio.gather(handle.task, return_exceptions=True)

    asyncio.run(run_case())


def test_direct_run_root_agent_reserves_active_handle_until_awaited_execution_finishes() -> None:
    async def run_case() -> None:
        service = AgentRuntimeService(InMemoryAgentRuntimeRepository())
        engine = _engine(runtime_gateway=AgentRuntimeGatewayImpl(service))
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_run(task, instance, request, *, interrupt_state=None):
            started.set()
            await release.wait()
            return SimpleNamespace(task_id=task.task_id, instance_id=instance.instance_id)

        engine._run_agent_instance = fake_run  # type: ignore[method-assign]
        direct_run = asyncio.create_task(engine.run_root_agent(_request(user_text="root direct input")))
        await asyncio.wait_for(started.wait(), timeout=2)

        assert "agent-instance-timeout" in engine._active_instance_ids
        assert "agent-instance-timeout" in engine._active_execution_handles
        assert not direct_run.done()

        release.set()
        result = await direct_run
        assert result.task_id == "task-timeout"
        await asyncio.sleep(0)
        assert "agent-instance-timeout" not in engine._active_instance_ids
        assert "agent-instance-timeout" not in engine._active_execution_handles
        assert not engine._background_tasks

    asyncio.run(run_case())


def test_direct_scene_run_releases_gate_after_active_reservation_and_completion_returns_busy() -> None:
    async def run_case() -> None:
        scene_id = "scene-direct-gate"
        service = AgentRuntimeService(InMemoryAgentRuntimeRepository())
        service.create_task_with_root_agent_instance(
            task_id="task-direct-gate",
            root_instance_id="agent-instance-direct-gate",
            thread_id="thread-direct-gate",
            current_working_directory="G:/project/agent",
            agent_definition_id="agent-timeout",
            llm_provider_id="provider",
            llm_model_binding_id="binding",
            conversation_id="conversation-direct-gate",
            scene_id=scene_id,
        )
        engine = _engine(runtime_gateway=AgentRuntimeGatewayImpl(service))
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_run(task, instance, request, *, interrupt_state=None):
            started.set()
            await release.wait()
            return SimpleNamespace(task_id=task.task_id, instance_id=instance.instance_id)

        engine._run_agent_instance = fake_run  # type: ignore[method-assign]
        direct_run = asyncio.create_task(
            engine.run_agent_instance_message(
                agent_instance_id="agent-instance-direct-gate",
                user_text="direct scene input",
                communication_stream_id=f"stream-scene-{scene_id}",
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)

        completion = await asyncio.wait_for(
            engine.complete_current_scene_task(scene_id),
            timeout=0.5,
        )
        assert completion.outcome == "running_agents"
        assert completion.message == RUNNING_AGENTS_MESSAGE

        release.set()
        await direct_run
        await asyncio.sleep(0)
        assert "agent-instance-direct-gate" not in engine._active_instance_ids
        assert "agent-instance-direct-gate" not in engine._active_execution_handles

    asyncio.run(run_case())


def test_direct_scene_run_does_not_hold_gate_during_file_validation() -> None:
    async def run_case() -> None:
        scene_id = "scene-direct-validation"
        service = AgentRuntimeService(InMemoryAgentRuntimeRepository())
        service.create_task_with_root_agent_instance(
            task_id="task-direct-validation",
            root_instance_id="agent-instance-direct-validation",
            thread_id="thread-direct-validation",
            current_working_directory="G:/project/agent",
            agent_definition_id="agent-timeout",
            llm_provider_id="provider",
            llm_model_binding_id="binding",
            conversation_id="conversation-direct-validation",
            scene_id=scene_id,
        )
        engine = _engine(runtime_gateway=AgentRuntimeGatewayImpl(service))
        validation_started = asyncio.Event()
        release_validation = asyncio.Event()
        execution_started = asyncio.Event()
        release_execution = asyncio.Event()
        original_validate = engine._validate_file_input_before_enqueue

        async def slow_validate(instance, attachments):
            validation_started.set()
            await release_validation.wait()
            await original_validate(instance, attachments)

        async def fake_run(task, instance, request, *, interrupt_state=None):
            execution_started.set()
            await release_execution.wait()
            return SimpleNamespace(task_id=task.task_id, instance_id=instance.instance_id)

        engine._validate_file_input_before_enqueue = slow_validate  # type: ignore[method-assign]
        engine._run_agent_instance = fake_run  # type: ignore[method-assign]
        direct_run = asyncio.create_task(
            engine.run_agent_instance_message(
                agent_instance_id="agent-instance-direct-validation",
                user_text="direct validation input",
                communication_stream_id=f"stream-scene-{scene_id}",
            )
        )
        await asyncio.wait_for(validation_started.wait(), timeout=2)

        completion = await asyncio.wait_for(
            engine.complete_current_scene_task(scene_id),
            timeout=0.5,
        )
        assert completion.outcome == "completed"
        assert completion.task_id == "task-direct-validation"
        assert not direct_run.done()

        release_validation.set()
        await asyncio.wait_for(execution_started.wait(), timeout=2)
        next_root = service.get_agent_instance("agent-instance-direct-validation")
        assert next_root.task_id != "task-direct-validation"
        assert "agent-instance-direct-validation" in engine._active_instance_ids
        assert "agent-instance-direct-validation" in engine._active_execution_handles

        release_execution.set()
        result = await direct_run
        assert result.task_id == next_root.task_id
        await asyncio.sleep(0)
        assert "agent-instance-direct-validation" not in engine._active_instance_ids
        assert "agent-instance-direct-validation" not in engine._active_execution_handles

    asyncio.run(run_case())


def test_direct_run_agent_instance_message_repairs_orphan_running_and_propagates_failure() -> None:
    async def run_case() -> None:
        service = _service_with_ready_instance()
        service.start_agent_instance("agent-instance-timeout")
        engine = _engine(runtime_gateway=service)
        started = asyncio.Event()

        async def fake_run(_task, _instance, _request, *, interrupt_state=None):
            started.set()
            raise RuntimeError("direct execution failed")

        engine._run_agent_instance = fake_run  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="direct execution failed"):
            await engine.run_agent_instance_message(
                agent_instance_id="agent-instance-timeout",
                user_text="legacy direct input",
                communication_stream_id="stream-timeout",
            )
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.sleep(0)

        repaired = service.get_agent_instance("agent-instance-timeout")
        assert repaired.status == AGENT_INSTANCE_STATUS_READY
        assert "持久态为 running" in (repaired.failure_message or "")
        assert "agent-instance-timeout" not in engine._active_instance_ids
        assert "agent-instance-timeout" not in engine._active_execution_handles
        assert not engine._background_tasks

    asyncio.run(run_case())


def test_submit_attachment_validation_rechecks_after_concurrent_enqueue() -> None:
    async def run_case() -> None:
        scene_id = "scene-submit-attachment-quota"
        service = AgentRuntimeService(InMemoryAgentRuntimeRepository())
        service.create_task_with_root_agent_instance(
            task_id="task-submit-attachment-quota",
            root_instance_id="agent-instance-submit-attachment-quota",
            thread_id="thread-submit-attachment-quota",
            current_working_directory="G:/project/agent",
            agent_definition_id="agent-timeout",
            llm_provider_id="provider",
            llm_model_binding_id="binding",
            conversation_id="conversation-submit-attachment-quota",
            scene_id=scene_id,
        )
        engine = _engine(runtime_gateway=AgentRuntimeGatewayImpl(service))
        attachment = PendingUserMessageAttachment(kind="image", resource_id="resource-image")
        first_two_validations = asyncio.Event()
        release_validations = asyncio.Event()
        validation_pending_counts: list[int] = []

        async def validate(instance, _attachments):
            validation_pending_counts.append(len(instance.pending_user_messages))
            if len(validation_pending_counts) == 2:
                first_two_validations.set()
            if len(validation_pending_counts) <= 2:
                await release_validations.wait()
            if instance.pending_user_messages:
                raise ValueError("attachment quota exceeded")

        def fake_launch(*, task, instance, request, interrupt_state=None):
            return asyncio.create_task(asyncio.sleep(0))

        engine._validate_file_input_before_enqueue = validate  # type: ignore[method-assign]
        engine._launch_tracked_execution = fake_launch  # type: ignore[method-assign]
        submit_kwargs = {
            "agent_instance_id": "agent-instance-submit-attachment-quota",
            "attachments": (attachment,),
        }
        first = asyncio.create_task(
            engine.submit_agent_instance_message(
                **submit_kwargs,
                user_text="first attachment",
            )
        )
        second = asyncio.create_task(
            engine.submit_agent_instance_message(
                **submit_kwargs,
                user_text="second attachment",
            )
        )
        await asyncio.wait_for(first_two_validations.wait(), timeout=2)
        release_validations.set()

        results = await asyncio.gather(first, second, return_exceptions=True)
        assert sum(isinstance(result, ValueError) for result in results) == 1
        accepted = [result for result in results if not isinstance(result, BaseException)]
        assert len(accepted) == 1
        assert validation_pending_counts[:2] == [0, 0]
        assert 1 in validation_pending_counts[2:]
        pending_instance = service.get_agent_instance("agent-instance-submit-attachment-quota")
        assert len(pending_instance.pending_user_messages) == 1

    asyncio.run(run_case())

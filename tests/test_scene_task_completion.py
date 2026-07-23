from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.agent.agent_runtime_service import AgentRuntimeService
from agent_runtime.agent.agent_task import AGENT_TASK_STATUS_COMPLETED
from agent_runtime.agent.infrastructure.in_memory_agent_runtime_repository import InMemoryAgentRuntimeRepository
from agent_runtime.agent.north_gateway.agent_runtime_contracts import (
    CreateChildAgentInstanceRequest,
    CreateTaskWithRootAgentInstanceRequest,
)
from agent_runtime.agent.north_gateway.agent_runtime_gateway_impl import AgentRuntimeGatewayImpl
from agent_runtime.agent.north_gateway.agent_runtime_rest_adapter import create_agent_runtime_router
from agent_runtime.agent.task_completion import (
    AgentTaskCompletionCoordinator,
    RUNNING_AGENTS_MESSAGE,
    SceneTaskCompletionResult,
    TASK_ENDED_MESSAGE,
    TASK_NOT_STARTED_MESSAGE,
    TaskOperationGate,
)
from agent_runtime.agent_lifecycle.agent_lifecycle_tool import EndTaskRequest, end_task
from agent_runtime.communication.runtime_event_publisher import RuntimeCommunicationContext


SCENE_ID = "scene-completion"
TASK_ID = "task-completion"
ROOT_ID = "agent-instance-root"
CHILD_ID = "agent-instance-child"
GRANDCHILD_ID = "agent-instance-grandchild"


class _RecordingTraceRecorder:
    def __init__(self) -> None:
        self.started_identities: list[object] = []
        self.events: list[tuple[str, object]] = []
        self.completed_count = 0

    def start_trace(self, identity):
        self.started_identities.append(identity)
        return Path("trace.md")

    def record_event(self, _trace_path, event_name: str, details=None) -> None:
        self.events.append((event_name, details))

    def record_completed(self, _trace_path) -> None:
        self.completed_count += 1


class _RecordingEventPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[RuntimeCommunicationContext, str, dict[str, object], str | None]] = []

    async def publish(self, context, event_type, payload=None, *, source=None, **_kwargs) -> None:
        self.events.append((context, event_type, dict(payload or {}), source))


class _CompletionHarness:
    def __init__(self, *, with_task: bool = True, conversation_id: str | None = None) -> None:
        self.service = AgentRuntimeService(InMemoryAgentRuntimeRepository())
        self.gateway = AgentRuntimeGatewayImpl(self.service)
        self.trace_recorder = _RecordingTraceRecorder()
        self.event_publisher = _RecordingEventPublisher()
        self.active_instance_ids: set[str] = set()
        self.deleted_terminal_task_ids: list[str] = []
        self.released_pause_timeouts: list[tuple[str, str]] = []
        self.coordinator = AgentTaskCompletionCoordinator(
            runtime_gateway=self.gateway,
            trace_recorder=self.trace_recorder,
            event_publisher=self.event_publisher,
            operation_gate=TaskOperationGate(),
            delete_task_terminals=self._delete_task_terminals,
            is_instance_active=self.active_instance_ids.__contains__,
            release_pause_timeout=lambda instance_id, reason: self.released_pause_timeouts.append((
                instance_id,
                reason,
            )),
        )
        if with_task:
            self.gateway.create_task_with_root_agent_instance(
                CreateTaskWithRootAgentInstanceRequest(
                    task_id=TASK_ID,
                    root_instance_id=ROOT_ID,
                    thread_id="thread-root",
                    current_working_directory="G:/project/agent",
                    agent_definition_id="root-agent",
                    llm_provider_id="provider",
                    llm_model_binding_id="binding",
                    conversation_id=conversation_id,
                    scene_id=SCENE_ID,
                )
            )

    async def _delete_task_terminals(self, task_id: str) -> None:
        self.deleted_terminal_task_ids.append(task_id)

    def add_descendants(self) -> None:
        self.gateway.create_child_agent_instance(
            CreateChildAgentInstanceRequest(
                task_id=TASK_ID,
                parent_instance_id=ROOT_ID,
                child_instance_id=CHILD_ID,
                thread_id="thread-child",
                current_working_directory="G:/project/agent",
                agent_definition_id="child-agent",
                llm_provider_id="provider",
                llm_model_binding_id="binding",
            )
        )
        self.gateway.create_child_agent_instance(
            CreateChildAgentInstanceRequest(
                task_id=TASK_ID,
                parent_instance_id=CHILD_ID,
                child_instance_id=GRANDCHILD_ID,
                thread_id="thread-grandchild",
                current_working_directory="G:/project/agent",
                agent_definition_id="grandchild-agent",
                llm_provider_id="provider",
                llm_model_binding_id="binding",
            )
        )

    def add_scene_task(
        self,
        *,
        task_id: str,
        root_instance_id: str,
        conversation_id: str | None = None,
    ) -> None:
        self.gateway.create_task_with_root_agent_instance(
            CreateTaskWithRootAgentInstanceRequest(
                task_id=task_id,
                root_instance_id=root_instance_id,
                thread_id=f"thread-{task_id}",
                current_working_directory="G:/project/agent",
                agent_definition_id="root-agent",
                llm_provider_id="provider",
                llm_model_binding_id="binding",
                conversation_id=conversation_id,
                scene_id=SCENE_ID,
            )
        )


def test_complete_current_scene_task_reports_not_started_without_task_or_execution_evidence() -> None:
    async def run_case() -> None:
        no_task = _CompletionHarness(with_task=False)
        missing_result = await no_task.coordinator.complete_current_scene_task(SCENE_ID)
        assert missing_result == SceneTaskCompletionResult(
            outcome="task_not_started",
            message=TASK_NOT_STARTED_MESSAGE,
            scene_id=SCENE_ID,
            task_id=None,
            root_agent_instance_id=None,
        )

        unstarted = _CompletionHarness()
        unstarted_result = await unstarted.coordinator.complete_current_scene_task(SCENE_ID)
        assert unstarted_result.outcome == "task_not_started"
        assert unstarted_result.message == TASK_NOT_STARTED_MESSAGE
        assert unstarted.service.get_task(TASK_ID).status == "unfinished"
        assert unstarted.deleted_terminal_task_ids == []
        assert unstarted.event_publisher.events == []
        assert unstarted.trace_recorder.completed_count == 0

    asyncio.run(run_case())


def test_complete_current_scene_task_reports_terminal_task_idempotently() -> None:
    async def run_case() -> None:
        harness = _CompletionHarness()
        harness.service.fail_task(TASK_ID, "test failure")

        result = await harness.coordinator.complete_current_scene_task(SCENE_ID)

        assert result.outcome == "task_ended"
        assert result.message == TASK_ENDED_MESSAGE
        assert result.task_id == TASK_ID
        assert result.root_agent_instance_id == ROOT_ID
        assert harness.deleted_terminal_task_ids == []
        assert harness.event_publisher.events == []
        assert harness.trace_recorder.completed_count == 0

    asyncio.run(run_case())


@pytest.mark.parametrize("busy_kind", ["persistent_running", "active_handle", "pending_input"])
def test_complete_current_scene_task_rejects_any_busy_descendant_without_side_effects(busy_kind: str) -> None:
    async def run_case() -> None:
        harness = _CompletionHarness()
        harness.add_descendants()
        harness.service.start_task(TASK_ID)

        if busy_kind == "persistent_running":
            harness.service.start_agent_instance(GRANDCHILD_ID)
        elif busy_kind == "active_handle":
            harness.active_instance_ids.add(GRANDCHILD_ID)
        else:
            harness.service.enqueue_agent_instance_user_message(
                GRANDCHILD_ID,
                user_text="queued user input",
            )

        result = await harness.coordinator.complete_current_scene_task(SCENE_ID)

        assert result.outcome == "running_agents"
        assert result.message == RUNNING_AGENTS_MESSAGE
        assert harness.service.get_task(TASK_ID).status == "unfinished"
        assert harness.service.get_agent_instance(GRANDCHILD_ID).instance_id == GRANDCHILD_ID
        assert harness.deleted_terminal_task_ids == []
        assert harness.event_publisher.events == []
        assert harness.trace_recorder.completed_count == 0

    asyncio.run(run_case())


def test_complete_current_scene_task_uses_legacy_instance_start_evidence() -> None:
    async def run_case() -> None:
        harness = _CompletionHarness()
        harness.service.start_agent_instance(ROOT_ID)
        harness.service.pause_agent_instance(ROOT_ID)
        assert harness.service.get_task(TASK_ID).started_at is None
        assert harness.service.get_agent_instance(ROOT_ID).started_at is not None

        result = await harness.coordinator.complete_current_scene_task(SCENE_ID)

        assert result.outcome == "completed"
        assert harness.service.get_task(TASK_ID).status == AGENT_TASK_STATUS_COMPLETED

    asyncio.run(run_case())


def test_historical_terminal_task_does_not_block_current_started_task_completion() -> None:
    async def run_case() -> None:
        harness = _CompletionHarness(conversation_id="conversation-historical")
        harness.service.start_task(TASK_ID)
        harness.service.complete_scene_task_and_reset_root_agent_instance(TASK_ID, ROOT_ID)
        await asyncio.sleep(0.001)
        current_task, current_root = harness.service.prepare_scene_root_agent_instance_for_new_task(ROOT_ID)
        harness.service.start_task(current_task.task_id)

        result = await harness.coordinator.complete_current_scene_task(SCENE_ID)

        assert result.outcome == "completed"
        assert result.task_id == current_task.task_id
        assert result.root_agent_instance_id == ROOT_ID
        assert harness.service.get_task(TASK_ID).status == AGENT_TASK_STATUS_COMPLETED
        assert harness.service.get_task(current_task.task_id).status == AGENT_TASK_STATUS_COMPLETED
        assert harness.service.get_agent_instance(ROOT_ID).instance_id == current_root.instance_id
        assert harness.deleted_terminal_task_ids == [current_task.task_id]
        assert len(harness.event_publisher.events) == 1

    asyncio.run(run_case())


@pytest.mark.parametrize("busy_kind", ["persistent_running", "active_handle", "pending_input"])
def test_historical_task_residual_busy_agent_blocks_idle_current_task_without_side_effects(
    busy_kind: str,
 ) -> None:
    async def run_case() -> None:
        harness = _CompletionHarness(conversation_id="conversation-historical")
        harness.service.start_task(TASK_ID)
        harness.service.complete_scene_task_and_reset_root_agent_instance(TASK_ID, ROOT_ID)
        await asyncio.sleep(0.001)
        historical_child_id = "agent-instance-historical-residual"
        harness.gateway.create_child_agent_instance(
            CreateChildAgentInstanceRequest(
                task_id=TASK_ID,
                parent_instance_id=ROOT_ID,
                child_instance_id=historical_child_id,
                thread_id="thread-historical-residual",
                current_working_directory="G:/project/agent",
                agent_definition_id="historical-agent",
                llm_provider_id="provider",
                llm_model_binding_id="binding",
            )
        )
        current_task, _current_root = harness.service.prepare_scene_root_agent_instance_for_new_task(ROOT_ID)

        if busy_kind == "persistent_running":
            harness.service.start_agent_instance(historical_child_id)
        elif busy_kind == "active_handle":
            harness.active_instance_ids.add(historical_child_id)
        else:
            harness.service.enqueue_agent_instance_user_message(
                historical_child_id,
                user_text="historical queued input",
            )

        result = await harness.coordinator.complete_current_scene_task(SCENE_ID)

        assert result.outcome == "running_agents"
        assert result.message == RUNNING_AGENTS_MESSAGE
        assert result.task_id == current_task.task_id
        assert harness.service.get_task(TASK_ID).status == AGENT_TASK_STATUS_COMPLETED
        assert harness.service.get_task(current_task.task_id).status == "unfinished"
        assert harness.service.get_agent_instance(historical_child_id).instance_id == historical_child_id
        assert harness.deleted_terminal_task_ids == []
        assert harness.event_publisher.events == []
        assert harness.trace_recorder.completed_count == 0

    asyncio.run(run_case())


def test_latest_terminal_task_wins_over_older_unfinished_task_boundary() -> None:
    async def run_case() -> None:
        harness = _CompletionHarness()
        newer_task_id = "task-newer-terminal"
        newer_root_id = "agent-instance-newer-terminal"
        await asyncio.sleep(0.001)
        harness.add_scene_task(task_id=newer_task_id, root_instance_id=newer_root_id)
        harness.service.fail_task(newer_task_id, "newer task reached terminal state")

        tasks = harness.gateway.list_scene_tasks(SCENE_ID)
        assert tasks[0].task_id == newer_task_id
        result = await harness.coordinator.complete_current_scene_task(SCENE_ID)

        assert result.outcome == "task_ended"
        assert result.task_id == newer_task_id
        assert result.root_agent_instance_id == newer_root_id
        assert harness.service.get_task(TASK_ID).status == "unfinished"
        assert harness.service.get_task(newer_task_id).status == "failed"
        assert harness.deleted_terminal_task_ids == []
        assert harness.event_publisher.events == []
        assert harness.trace_recorder.started_identities == []
        assert harness.trace_recorder.events == []
        assert harness.trace_recorder.completed_count == 0

    asyncio.run(run_case())


def test_user_completion_performs_shared_teardown_once_and_keeps_scene_root_reusable() -> None:
    async def run_case() -> None:
        harness = _CompletionHarness(conversation_id="conversation-root")
        harness.add_descendants()
        harness.service.start_task(TASK_ID)

        first, second = await asyncio.gather(
            harness.coordinator.complete_current_scene_task(SCENE_ID),
            harness.coordinator.complete_current_scene_task(SCENE_ID),
        )

        assert {first.outcome, second.outcome} == {"completed", "task_ended"}
        assert first.message == TASK_ENDED_MESSAGE
        assert second.message == TASK_ENDED_MESSAGE

        completed_task = harness.service.get_task(TASK_ID)
        assert completed_task.status == AGENT_TASK_STATUS_COMPLETED
        assert completed_task.finished_at is not None
        assert all(relation.is_released for relation in completed_task.parent_child_relations)

        root = harness.service.get_agent_instance(ROOT_ID)
        assert root.instance_id == ROOT_ID
        assert root.task_id == TASK_ID
        assert root.status == "ready"
        assert root.conversation_id is None
        assert root.agent_definition_id == "root-agent"
        assert root.current_working_directory == "G:/project/agent"
        with pytest.raises(KeyError):
            harness.service.get_agent_instance(CHILD_ID)
        with pytest.raises(KeyError):
            harness.service.get_agent_instance(GRANDCHILD_ID)

        assert harness.deleted_terminal_task_ids == [TASK_ID]
        assert harness.released_pause_timeouts == [
            (ROOT_ID, "task_completed"),
            (CHILD_ID, "task_completed"),
            (GRANDCHILD_ID, "task_completed"),
        ]
        assert harness.trace_recorder.completed_count == 1
        assert harness.trace_recorder.events == [
            (
                "task_completion_requested",
                {
                    "completion_source": "user_action",
                    "completion_summary": "用户从主 Agent 聊天面板主动结束当前 Task 会话",
                },
            )
        ]
        assert len(harness.event_publisher.events) == 1
        context, event_type, payload, source = harness.event_publisher.events[0]
        assert context.stream_id == f"stream-scene-{SCENE_ID}"
        assert event_type == "agent.task.completed"
        assert source == "agent_execution_engine"
        assert payload["task_id"] == TASK_ID
        assert payload["agent_instance_id"] == ROOT_ID
        assert payload["completion_source"] == "user_action"
        assert payload["completion_summary"] == "用户从主 Agent 聊天面板主动结束当前 Task 会话"
        assert payload["frontend_messages"] == []

        next_task, next_root = harness.service.prepare_scene_root_agent_instance_for_new_task(ROOT_ID)
        assert next_task.task_id != TASK_ID
        assert next_task.status == "unfinished"
        assert next_root.instance_id == ROOT_ID
        assert next_root.task_id == next_task.task_id
        assert next_root.conversation_id is None
        assert next_root.thread_id != "thread-root"

    asyncio.run(run_case())


def test_root_lifecycle_end_task_uses_same_completion_path_while_root_is_running() -> None:
    async def run_case() -> None:
        harness = _CompletionHarness(conversation_id="conversation-root")
        harness.service.start_task(TASK_ID)
        harness.service.start_agent_instance(ROOT_ID)
        tool_result = end_task(EndTaskRequest(summary="用户已验收交付"))

        completed = await harness.coordinator.complete_root_from_lifecycle(
            task_id=TASK_ID,
            root_instance_id=ROOT_ID,
            trace_path=Path("trace.md"),
            communication_context=RuntimeCommunicationContext(
                stream_id=f"stream-scene-{SCENE_ID}",
                metadata={"task_id": TASK_ID, "scene_id": SCENE_ID},
            ),
            tool_results=[tool_result],
            completion_summary="用户已验收交付",
        )

        assert completed is True
        assert harness.service.get_task(TASK_ID).status == AGENT_TASK_STATUS_COMPLETED
        assert harness.service.get_agent_instance(ROOT_ID).status == "ready"
        assert harness.deleted_terminal_task_ids == [TASK_ID]
        assert harness.trace_recorder.completed_count == 1
        assert len(harness.event_publisher.events) == 1
        payload = harness.event_publisher.events[0][2]
        assert payload["completion_source"] == "agent_lifecycle_tool"
        assert payload["completion_summary"] == "用户已验收交付"
        assert payload["tool_result_count"] == 1

    asyncio.run(run_case())


class _RestSceneGateway:
    def get_scene(self, workspace_path: str, scene_id: str):
        if workspace_path != "G:/project/agent" or scene_id != SCENE_ID:
            raise KeyError(scene_id)
        return SimpleNamespace(scene_id=scene_id)


class _RestCompletionEngine:
    def __init__(self, result: SceneTaskCompletionResult) -> None:
        self.result = result
        self.scene_ids: list[str] = []

    async def complete_current_scene_task(self, scene_id: str) -> SceneTaskCompletionResult:
        self.scene_ids.append(scene_id)
        return self.result


@pytest.mark.parametrize(
    ("outcome", "message", "expected_status"),
    [
        ("completed", TASK_ENDED_MESSAGE, 200),
        ("task_ended", TASK_ENDED_MESSAGE, 200),
        ("task_not_started", TASK_NOT_STARTED_MESSAGE, 200),
        ("running_agents", RUNNING_AGENTS_MESSAGE, 409),
    ],
)
def test_complete_current_scene_task_rest_contract(
    outcome: str,
    message: str,
    expected_status: int,
) -> None:
    result = SceneTaskCompletionResult(
        outcome=outcome,  # type: ignore[arg-type]
        message=message,
        scene_id=SCENE_ID,
        task_id=TASK_ID,
        root_agent_instance_id=ROOT_ID,
    )
    engine = _RestCompletionEngine(result)
    app = FastAPI()
    app.include_router(
        create_agent_runtime_router(
            gateway=object(),  # type: ignore[arg-type]
            execution_engine=engine,  # type: ignore[arg-type]
            scene_gateway=_RestSceneGateway(),  # type: ignore[arg-type]
        ),
        prefix="/api/agent-runtime",
    )

    response = TestClient(app).post(
        f"/api/agent-runtime/scenes/{SCENE_ID}/tasks/current/complete",
        json={"workspace_path": "G:/project/agent"},
    )

    assert response.status_code == expected_status
    assert response.json() == {
        "outcome": outcome,
        "message": message,
        "scene_id": SCENE_ID,
        "task_id": TASK_ID,
        "root_agent_instance_id": ROOT_ID,
    }
    assert engine.scene_ids == [SCENE_ID]

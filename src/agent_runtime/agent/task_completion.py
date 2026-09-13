# -*- coding: utf-8 -*-
"""Agent Task 成功完成的统一应用编排。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Literal, Sequence
from weakref import WeakValueDictionary

from agent_runtime.agent.agent_instance import (
    AGENT_INSTANCE_STATUS_RUNNING,
    AgentInstance,
)
from agent_runtime.agent.agent_task import AgentTask
from agent_runtime.agent.agent_trace_recorder import (
    AgentTraceIdentity,
    AgentTraceRecorder,
)
from agent_runtime.agent.north_gateway.agent_runtime_contracts import AgentRuntimeGateway
from agent_runtime.communication.runtime_event_publisher import (
    RuntimeCommunicationContext,
    RuntimeEventPublisher,
)
from agent_runtime.tool.tool_execution_models import BaseToolResult


CompletionSource = Literal["agent_lifecycle_tool", "user_action"]
SceneTaskCompletionOutcome = Literal[
    "completed",
    "task_ended",
    "task_not_started",
    "running_agents",
]

TASK_ENDED_MESSAGE = "任务已结束"
TASK_NOT_STARTED_MESSAGE = "任务尚未开始"
RUNNING_AGENTS_MESSAGE = "仍有 Agent 在运行中，请先暂停他们"
USER_COMPLETION_SUMMARY = "用户从主 Agent 聊天面板主动结束当前 Task 会话"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SceneTaskCompletionResult:
    """用户主动完成 Scene 当前 Task 的结构化业务结果。"""

    outcome: SceneTaskCompletionOutcome
    message: str
    scene_id: str
    task_id: str | None
    root_agent_instance_id: str | None


class TaskOperationGate:
    """在单 Runtime 进程内串行化同一 Scene/Task 的提交与完成准入。"""

    def __init__(self) -> None:
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    @asynccontextmanager
    async def hold_key(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        async with lock:
            yield

    def key_for_task(self, task: AgentTask) -> str:
        if task.scene_id is not None:
            return self.key_for_scene(task.scene_id)
        return f"task:{task.task_id}"

    @staticmethod
    def key_for_scene(scene_id: str) -> str:
        return f"scene:{scene_id}"


class AgentTaskCompletionCoordinator:
    """把 lifecycle end_task 与用户按钮汇入同一 Task 完成提交路径。"""

    def __init__(
        self,
        *,
        runtime_gateway: AgentRuntimeGateway,
        trace_recorder: AgentTraceRecorder,
        event_publisher: RuntimeEventPublisher,
        operation_gate: TaskOperationGate,
        delete_task_terminals: Callable[[str], Awaitable[None]],
        is_instance_active: Callable[[str], bool],
        release_pause_timeout: Callable[[str, str], None],
        cleanup_task_notes: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._runtime_gateway = runtime_gateway
        self._trace_recorder = trace_recorder
        self._event_publisher = event_publisher
        self._operation_gate = operation_gate
        self._delete_task_terminals = delete_task_terminals
        self._is_instance_active = is_instance_active
        self._release_pause_timeout = release_pause_timeout
        self._cleanup_task_notes = cleanup_task_notes

    async def complete_current_scene_task(
        self,
        scene_id: str,
    ) -> SceneTaskCompletionResult:
        """按用户入口的严格准入规则成功完成 Scene 当前 Task。

        Scene 可以保留历史 terminal task，但正常情况下最多一个 unfinished current task；
        先扫描所有历史任务的现存实例防御残留 busy，再按 current task 状态矩阵决定结果。
        """
        async with self._operation_gate.hold_key(self._operation_gate.key_for_scene(scene_id)):
            tasks = self._runtime_gateway.list_scene_tasks(scene_id)
            if not tasks:
                return SceneTaskCompletionResult(
                    outcome="task_not_started",
                    message=TASK_NOT_STARTED_MESSAGE,
                    scene_id=scene_id,
                    task_id=None,
                    root_agent_instance_id=None,
                )

            task = tasks[0]
            if task.is_terminal and any(not historical_task.is_terminal for historical_task in tasks[1:]):
                logger.warning(
                    "Scene 最新 Task 已终态但历史仍存在 unfinished Task，按最新 Task 边界拒绝回退完成: scene_id=%s latest_task_id=%s historical_unfinished_task_ids=%s",
                    scene_id,
                    task.task_id,
                    [historical_task.task_id for historical_task in tasks[1:] if not historical_task.is_terminal],
                )
            instances_by_task = [
                (scene_task, self._runtime_gateway.list_task_agent_instances(scene_task.task_id))
                for scene_task in tasks
            ]
            if any(self._task_is_busy(instances) for _scene_task, instances in instances_by_task):
                return self._result_for_task(
                    task,
                    outcome="running_agents",
                    message=RUNNING_AGENTS_MESSAGE,
                )
            if task.is_terminal:
                return self._result_for_task(
                    task,
                    outcome="task_ended",
                    message=TASK_ENDED_MESSAGE,
                )

            root_instance = self._runtime_gateway.get_agent_instance(task.root_agent_instance_id)
            if root_instance.task_id != task.task_id:
                raise ValueError(
                    "Scene 当前 Task 与长期 root Agent 实例绑定不一致，不能结束 Task 会话。"
                )
            instances = next(
                instances
                for scene_task, instances in instances_by_task
                if scene_task.task_id == task.task_id
            )
            if not self._task_has_started(task, instances):
                return self._result_for_task(
                    task,
                    outcome="task_not_started",
                    message=TASK_NOT_STARTED_MESSAGE,
                )

            trace_path = self._trace_recorder.start_trace(_trace_identity(root_instance))
            communication_context = _scene_communication_context(task, root_instance)
            completed = await self._complete_root_task(
                task=task,
                root_instance=root_instance,
                trace_path=trace_path,
                communication_context=communication_context,
                tool_results=(),
                completion_source="user_action",
                completion_summary=USER_COMPLETION_SUMMARY,
            )
            if not completed:
                return self._result_for_task(
                    task,
                    outcome="task_ended",
                    message=TASK_ENDED_MESSAGE,
                )
            return self._result_for_task(
                task,
                outcome="completed",
                message=TASK_ENDED_MESSAGE,
            )

    async def complete_root_from_lifecycle(
        self,
        *,
        task_id: str,
        root_instance_id: str,
        trace_path: Path,
        communication_context: RuntimeCommunicationContext,
        tool_results: Sequence[BaseToolResult],
        completion_summary: str,
    ) -> bool:
        """保留 root Agent 在自身运行循环中调用 end_task 的完成语义。"""
        initial_task = self._runtime_gateway.get_task(task_id)
        async with self._operation_gate.hold_key(self._operation_gate.key_for_task(initial_task)):
            task = self._runtime_gateway.get_task(task_id)
            if task.is_terminal:
                return False
            if task.root_agent_instance_id != root_instance_id:
                raise ValueError("只有 Task root Agent 可以完成整个 Task。")
            root_instance = self._runtime_gateway.get_agent_instance(root_instance_id)
            return await self._complete_root_task(
                task=task,
                root_instance=root_instance,
                trace_path=trace_path,
                communication_context=communication_context,
                tool_results=tool_results,
                completion_source="agent_lifecycle_tool",
                completion_summary=completion_summary,
            )

    async def _complete_root_task(
        self,
        *,
        task: AgentTask,
        root_instance: AgentInstance,
        trace_path: Path,
        communication_context: RuntimeCommunicationContext,
        tool_results: Sequence[BaseToolResult],
        completion_source: CompletionSource,
        completion_summary: str,
    ) -> bool:
        """唯一不可逆完成提交路径；调用方必须已经持有 operation gate。"""
        current_task = self._runtime_gateway.get_task(task.task_id)
        if current_task.is_terminal:
            return False
        if current_task.root_agent_instance_id != root_instance.instance_id:
            raise ValueError("Agent 实例不是当前 Task 的 root Agent。")

        live_instances = self._runtime_gateway.list_task_agent_instances(current_task.task_id)
        for instance in live_instances:
            self._release_pause_timeout(instance.instance_id, "task_completed")

        self._trace_recorder.record_event(
            trace_path,
            "task_completion_requested",
            {
                "completion_source": completion_source,
                "completion_summary": completion_summary,
            },
        )
        if current_task.scene_id is not None:
            self._runtime_gateway.complete_scene_task_and_reset_root_agent_instance(
                current_task.task_id,
                root_instance.instance_id,
            )
        else:
            self._runtime_gateway.complete_agent_instance(root_instance.instance_id)
            self._runtime_gateway.complete_task(current_task.task_id)

        await self._delete_task_terminals(current_task.task_id)
        if self._cleanup_task_notes is not None:
            try:
                try:
                    await self._cleanup_task_notes(current_task.task_id)
                except TypeError:
                    await self._cleanup_task_notes(task_id=current_task.task_id)
            except Exception as exc:
                logger.error("完成清理 Task Note 异常: %s", exc, exc_info=True)
        self._trace_recorder.record_completed(trace_path)
        await self._publish_completed_event(
            communication_context=communication_context,
            task=current_task,
            root_instance=root_instance,
            tool_results=tool_results,
            completion_source=completion_source,
            completion_summary=completion_summary,
        )
        return True

    def _task_is_busy(self, instances: Sequence[AgentInstance]) -> bool:
        return any(
            instance.status == AGENT_INSTANCE_STATUS_RUNNING
            or self._is_instance_active(instance.instance_id)
            or bool(instance.pending_user_messages)
            for instance in instances
        )

    @staticmethod
    def _task_has_started(task: AgentTask, instances: Sequence[AgentInstance]) -> bool:
        if task.started_at is not None:
            return True
        return any(
            instance.started_at is not None
            or instance.conversation_id is not None
            or bool(instance.pending_user_messages)
            for instance in instances
        )

    async def _publish_completed_event(
        self,
        *,
        communication_context: RuntimeCommunicationContext,
        task: AgentTask,
        root_instance: AgentInstance,
        tool_results: Sequence[BaseToolResult],
        completion_source: CompletionSource,
        completion_summary: str,
    ) -> None:
        frontend_messages = [
            result.frontend_message.model_dump(mode="json")
            for result in tool_results
            if result.frontend_message is not None
        ]
        await self._event_publisher.publish(
            communication_context,
            "agent.task.completed",
            {
                "task_id": task.task_id,
                "agent_instance_id": root_instance.instance_id,
                "loop_decision": "complete",
                "tool_result_count": len(tool_results),
                "frontend_messages": frontend_messages,
                "failure_message": None,
                "completion_source": completion_source,
                "completion_summary": completion_summary,
            },
            source="agent_execution_engine",
        )

    @staticmethod
    def _result_for_task(
        task: AgentTask,
        *,
        outcome: SceneTaskCompletionOutcome,
        message: str,
    ) -> SceneTaskCompletionResult:
        return SceneTaskCompletionResult(
            outcome=outcome,
            message=message,
            scene_id=task.scene_id or "",
            task_id=task.task_id,
            root_agent_instance_id=task.root_agent_instance_id,
        )


def _trace_identity(instance: AgentInstance) -> AgentTraceIdentity:
    return AgentTraceIdentity(
        task_id=instance.task_id,
        instance_id=instance.instance_id,
        thread_id=instance.thread_id,
        agent_definition_id=instance.agent_definition_id,
        trace_id=instance.trace_id,
    )


def _scene_communication_context(
    task: AgentTask,
    root_instance: AgentInstance,
) -> RuntimeCommunicationContext:
    if task.scene_id is None:
        raise ValueError("用户主动结束入口只支持绑定 Scene 的 Task。")
    return RuntimeCommunicationContext(
        stream_id=f"stream-scene-{task.scene_id}",
        actor_type="agent_instance",
        actor_id=root_instance.instance_id,
        trace_id=root_instance.trace_id,
        metadata={
            "task_id": task.task_id,
            "scene_id": task.scene_id,
            "thread_id": root_instance.thread_id,
            "agent_instance_id": root_instance.instance_id,
            "agent_definition_id": root_instance.agent_definition_id,
        },
    )

# -*- coding: utf-8 -*-
"""本地子 Agent 协作运行时门面。"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agent_runtime.agent.agent_instance import AGENT_INSTANCE_STATUS_RUNNING
from agent_runtime.agent.agent_task import AgentParentChildRelation
from agent_runtime.agent.north_gateway.agent_runtime_contracts import CreateChildAgentInstanceRequest
from agent_runtime.communication.runtime_event_publisher import RuntimeCommunicationContext

if TYPE_CHECKING:
    from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine
    from agent_runtime.agent.north_gateway.agent_runtime_contracts import AgentRuntimeGateway
    from agent_runtime.agent_definition.north_gateway.agent_definition_contracts import AgentDefinitionNorthGateway
    from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import LlmModelRegistryGateway
    from agent_runtime.communication.runtime_event_publisher import RuntimeEventPublisher

logger = logging.getLogger(__name__)



class SubagentRuntime:
    """本地子 Agent 工具的运行时编排门面。

    该对象隔离本地工具函数和执行引擎/运行态网关，保证工具本身只表达父子 Agent 协作语义，
    具体创建、投递、状态查询和释放动作都通过既有运行时网关与执行引擎完成。
    """

    def __init__(
        self,
        *,
        runtime_gateway: "AgentRuntimeGateway",
        agent_definition_gateway: "AgentDefinitionNorthGateway",
        llm_registry_gateway: "LlmModelRegistryGateway",
        execution_engine: "AgentExecutionEngine",
        event_publisher: "RuntimeEventPublisher",
    ) -> None:
        self._runtime_gateway = runtime_gateway
        self._agent_definition_gateway = agent_definition_gateway
        self._llm_registry_gateway = llm_registry_gateway
        self._execution_engine = execution_engine
        self._event_publisher = event_publisher

    async def create_subagent(
        self,
        *,
        parent_instance_id: str,
        agent_definition_id: str,
        llm_policy_group_id: str | None = None,
        initial_message: str | None = None,
    ) -> dict[str, Any]:
        parent = self._runtime_gateway.get_agent_instance(parent_instance_id)
        task = self._runtime_gateway.get_task(parent.task_id)
        if task.is_terminal:
            raise ValueError(f"当前任务已结束，不能创建子 Agent: {task.task_id}")
        parent_definition = self._agent_definition_gateway.get_agent_definition(parent.agent_definition_id)
        if agent_definition_id not in parent_definition.available_sub_agent_ids:
            raise PermissionError(
                f"父 Agent 定义未授权创建该子 Agent: {parent.agent_definition_id}/{agent_definition_id}"
            )
        child_definition = self._agent_definition_gateway.get_agent_definition(agent_definition_id)
        if child_definition.disabled:
            raise ValueError(f"子 Agent 定义已禁用: {agent_definition_id}")
        selection = self._resolve_child_llm_selection(
            parent_llm_provider_id=parent.llm_provider_id,
            parent_llm_model_binding_id=parent.llm_model_binding_id,
            parent_llm_policy_group_id=parent.llm_policy_group_id,
            child_agent_definition_id=agent_definition_id,
            requested_policy_group_id=llm_policy_group_id,
            default_policy_group_id=child_definition.default_llm_policy_group_id,
            allowed_policy_group_ids=child_definition.allowed_llm_policy_group_ids,
        )
        _updated_task, child, relation = self._runtime_gateway.create_child_agent_instance(
            CreateChildAgentInstanceRequest(
                task_id=task.task_id,
                parent_instance_id=parent.instance_id,
                thread_id=f"thread-{uuid4().hex}",
                current_working_directory=parent.current_working_directory,
                agent_definition_id=agent_definition_id,
                llm_provider_id=selection["llm_provider_id"],
                llm_model_binding_id=selection["llm_model_binding_id"],
                llm_policy_group_id=selection.get("llm_policy_group_id"),
            )
        )
        await self._publish_subagent_created(
            parent_instance_id=parent.instance_id,
            child_instance_id=child.instance_id,
            relation=relation,
        )
        if initial_message and initial_message.strip():
            await self.send_agent_message(
                from_instance_id=parent.instance_id,
                to_instance_id=child.instance_id,
                content=initial_message,
            )
        return {
            "status": "created",
            "parent_agent_instance_id": parent.instance_id,
            "child_agent_instance_id": child.instance_id,
            "child_agent_definition_id": child.agent_definition_id,
            "task_id": child.task_id,
            "thread_id": child.thread_id,
            "relation_id": relation.relation_id,
            "llm_policy_group_id": child.llm_policy_group_id,
            "llm_provider_id": child.llm_provider_id,
            "llm_model_binding_id": child.llm_model_binding_id,
        }

    async def send_agent_message(
        self,
        *,
        from_instance_id: str,
        to_instance_id: str,
        content: str,
    ) -> dict[str, Any]:
        content = content.strip()
        if not content:
            raise ValueError("content 不能为空。")
        source = self._runtime_gateway.get_agent_instance(from_instance_id)
        target = self._runtime_gateway.get_agent_instance(to_instance_id)
        task = self._runtime_gateway.get_task(source.task_id)
        if source.task_id != target.task_id:
            raise PermissionError("只能在同一任务内向父/子 Agent 发送消息。")
        if task.is_terminal:
            raise ValueError(f"当前任务已结束，不能发送子 Agent 消息: {task.task_id}")
        if not self._can_message(task_id=task.task_id, from_instance_id=source.instance_id, to_instance_id=target.instance_id):
            raise PermissionError("只能在未释放的直接父子 Agent 之间发送消息。")
        source_definition = self._agent_definition_gateway.get_agent_definition(source.agent_definition_id)
        source_name = source_definition.name or source_definition.agent_id
        user_text = f"【@本地Agent {source_name} {source.instance_id}】{content}"
        metadata = {
            "source_type": "local_agent",
            "transport": "subagent",
            "channel": "subagent",
            "display_text": content,
            "local_agent": {
                "name": source_name,
                "agent_definition_id": source.agent_definition_id,
                "instance_id": source.instance_id,
            },
            "subagent": {
                "from_instance_id": source.instance_id,
                "to_instance_id": target.instance_id,
                "task_id": task.task_id,
            },
        }
        result = await self._execution_engine.submit_agent_instance_message(
            agent_instance_id=target.instance_id,
            user_text=user_text,
            message_metadata=metadata,
        )
        return {
            "status": result.status,
            "task_id": result.task_id,
            "target_agent_instance_id": result.instance_id,
            "pending_message_id": result.pending_message_id,
            "queued_for_later": result.queued_for_later,
            "delivery": "accepted",
        }

    def get_agent_status(self, requester_instance_id: str, target_instance_id: str) -> dict[str, Any]:
        requester = self._runtime_gateway.get_agent_instance(requester_instance_id)
        target = self._runtime_gateway.get_agent_instance(target_instance_id)
        if requester.task_id != target.task_id:
            raise PermissionError("只能查询同一任务内的 Agent 状态。")
        task = self._runtime_gateway.get_task(requester.task_id)
        if target.instance_id != requester.instance_id and not self._is_direct_parent_child(task.task_id, requester.instance_id, target.instance_id):
            raise PermissionError("只能查询当前 Agent 自身或直接父子 Agent 的状态。")
        relation = task.child_relation_of(target.instance_id)
        released = bool(relation and relation.is_released)
        if task.is_terminal or released:
            public_status = "ended"
        elif target.status == AGENT_INSTANCE_STATUS_RUNNING:
            public_status = "running"
        else:
            public_status = "paused"
        return {
            "agent_instance_id": target.instance_id,
            "agent_definition_id": target.agent_definition_id,
            "task_id": target.task_id,
            "status": public_status,
            "runtime_status": target.status,
            "released": released,
            "release_reason": relation.release_reason if relation else None,
        }

    def list_subagents(self, parent_instance_id: str) -> list[dict[str, Any]]:
        children = self._runtime_gateway.list_unreleased_child_agent_instances(parent_instance_id)
        return [
            {
                "child_agent_instance_id": child.instance_id,
                "child_agent_definition_id": child.agent_definition_id,
                "task_id": child.task_id,
                "status": "running" if child.status == AGENT_INSTANCE_STATUS_RUNNING else "paused",
                "runtime_status": child.status,
            }
            for child in children
        ]

    def end_subagent(
        self,
        *,
        parent_instance_id: str,
        child_instance_id: str,
        reason: str,
    ) -> dict[str, Any]:
        updated_task, released_instances = self._runtime_gateway.release_child_agent_instance(
            parent_instance_id,
            child_instance_id,
            reason=reason,
        )
        return {
            "status": "ended",
            "task_id": updated_task.task_id,
            "parent_agent_instance_id": parent_instance_id,
            "child_agent_instance_id": child_instance_id,
            "released_agent_instance_ids": [instance.instance_id for instance in released_instances],
            "reason": reason,
        }

    def _resolve_child_llm_selection(
        self,
        *,
        parent_llm_provider_id: str,
        parent_llm_model_binding_id: str,
        parent_llm_policy_group_id: str | None,
        child_agent_definition_id: str,
        requested_policy_group_id: str | None,
        default_policy_group_id: str | None,
        allowed_policy_group_ids: tuple[str, ...],
    ) -> dict[str, str | None]:
        policy_group_id = requested_policy_group_id or default_policy_group_id or parent_llm_policy_group_id
        if allowed_policy_group_ids and policy_group_id not in allowed_policy_group_ids:
            raise ValueError(
                f"子 Agent 定义不允许使用该 LLM 执行策略组: {child_agent_definition_id}/{policy_group_id}"
            )
        if policy_group_id:
            policy = self._llm_registry_gateway.resolve_execution_policy(policy_group_id)
            first = policy.candidates[0]
            return {
                "llm_provider_id": first.provider_id,
                "llm_model_binding_id": first.model_binding_id,
                "llm_policy_group_id": policy.policy_group_id,
            }
        return {
            "llm_provider_id": parent_llm_provider_id,
            "llm_model_binding_id": parent_llm_model_binding_id,
            "llm_policy_group_id": None,
        }

    def _is_direct_parent_child(self, task_id: str, first_instance_id: str, second_instance_id: str) -> bool:
        task = self._runtime_gateway.get_task(task_id)
        first_relation = task.child_relation_of(first_instance_id)
        if first_relation is not None and first_relation.parent_instance_id == second_instance_id:
            return True
        second_relation = task.child_relation_of(second_instance_id)
        return bool(second_relation is not None and second_relation.parent_instance_id == first_instance_id)

    def _can_message(self, *, task_id: str, from_instance_id: str, to_instance_id: str) -> bool:
        task = self._runtime_gateway.get_task(task_id)
        relation = task.child_relation_of(to_instance_id)
        if relation is not None and not relation.is_released and relation.parent_instance_id == from_instance_id:
            return True
        reverse_relation = task.child_relation_of(from_instance_id)
        return bool(
            reverse_relation is not None
            and not reverse_relation.is_released
            and reverse_relation.parent_instance_id == to_instance_id
        )

    async def _publish_subagent_created(
        self,
        *,
        parent_instance_id: str,
        child_instance_id: str,
        relation: AgentParentChildRelation,
    ) -> None:
        child = self._runtime_gateway.get_agent_instance(child_instance_id)
        task = self._runtime_gateway.get_task(child.task_id)
        context = RuntimeCommunicationContext(
            stream_id=_default_stream_id_for_task(task.scene_id, task.task_id),
            actor_type="agent_instance",
            actor_id=parent_instance_id,
            trace_id=child.trace_id,
            metadata={
                "task_id": task.task_id,
                "scene_id": task.scene_id,
                "thread_id": child.thread_id,
                "agent_instance_id": child.instance_id,
                "agent_definition_id": child.agent_definition_id,
                "parent_agent_instance_id": parent_instance_id,
            },
        )
        await self._event_publisher.publish(
            context,
            "agent.subagent.created",
            {
                "task_id": task.task_id,
                "parent_agent_instance_id": parent_instance_id,
                "child_agent_instance_id": child.instance_id,
                "child_agent_definition_id": child.agent_definition_id,
                "relation_id": relation.relation_id,
                "released": relation.is_released,
                "created_at": relation.created_at.isoformat(),
                "agent_instance": {
                    "instance_id": child.instance_id,
                    "task_id": child.task_id,
                    "thread_id": child.thread_id,
                    "current_working_directory": child.current_working_directory,
                    "agent_definition_id": child.agent_definition_id,
                    "llm_policy_group_id": child.llm_policy_group_id,
                    "llm_provider_id": child.llm_provider_id,
                    "llm_model_binding_id": child.llm_model_binding_id,
                    "conversation_id": child.conversation_id,
                    "trace_id": child.trace_id,
                    "status": child.status,
                    "pending_user_message_count": len(child.pending_user_messages),
                    "pending_user_messages": [],
                    "context_usage": None,
                    "created_at": child.created_at.isoformat(),
                    "updated_at": child.updated_at.isoformat(),
                },
            },
            source="subagent_runtime",
        )


_runtime: SubagentRuntime | None = None


def configure_subagent_runtime(
    *,
    runtime_gateway: "AgentRuntimeGateway",
    agent_definition_gateway: "AgentDefinitionNorthGateway",
    llm_registry_gateway: "LlmModelRegistryGateway",
    execution_engine: "AgentExecutionEngine",
    event_publisher: "RuntimeEventPublisher",
) -> SubagentRuntime:
    global _runtime
    _runtime = SubagentRuntime(
        runtime_gateway=runtime_gateway,
        agent_definition_gateway=agent_definition_gateway,
        llm_registry_gateway=llm_registry_gateway,
        execution_engine=execution_engine,
        event_publisher=event_publisher,
    )
    return _runtime


def get_subagent_runtime() -> SubagentRuntime:
    if _runtime is None:
        raise RuntimeError("Subagent runtime has not been configured.")
    return _runtime


def _default_stream_id_for_task(scene_id: str | None, task_id: str) -> str:
    if scene_id:
        return f"stream-scene-{scene_id}"
    return f"stream-agent-task-{task_id}"


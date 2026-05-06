# -*- coding: utf-8 -*-
"""Agent 运行时任务与实例管理领域服务。"""
from __future__ import annotations

import logging
from threading import RLock
from typing import Any
from uuid import uuid4

from agent_runtime.agent.agent_instance import (
    AGENT_INSTANCE_STATUS_CANCELLED,
    AGENT_INSTANCE_STATUS_COMPLETED,
    AGENT_INSTANCE_STATUS_FAILED,
    AGENT_INSTANCE_STATUS_PAUSED,
    AGENT_INSTANCE_STATUS_RUNNING,
    AgentInstance,
    AgentInstanceStatus,
)
from agent_runtime.agent.agent_runtime_repository import AgentRuntimeRepository
from agent_runtime.agent.agent_task import (
    AGENT_TASK_STATUS_CANCELLED,
    AGENT_TASK_STATUS_COMPLETED,
    AGENT_TASK_STATUS_FAILED,
    AGENT_TASK_STATUS_PAUSED,
    AGENT_TASK_STATUS_RUNNING,
    AgentParentChildRelation,
    AgentTask,
    AgentTaskStatus,
)

logger = logging.getLogger(__name__)


def _new_id(prefix: str) -> str:
    """生成带业务前缀的运行态 ID，方便日志和排障识别对象类型。"""
    return f"{prefix}-{uuid4().hex}"


def _require_non_blank(value: str, field_name: str) -> None:
    """校验运行态创建请求中的字符串字段不能为空。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空。")
    if value != value.strip():
        raise ValueError(f"{field_name} 不能包含首尾空白字符。")


class AgentRuntimeService:
    """Agent 任务与实例运行态领域服务。

    该服务只负责程序内部动态运行态管理，不执行 LLM 请求循环。
    它保证一个任务只有一个根 Agent 实例入口，任务内部可以维护任意数量的
    Agent 实例以及一棵父子关系树；多个任务、同一任务内多个实例均不设数量上限。
    """

    def __init__(self, repository: AgentRuntimeRepository) -> None:
        # Agent 运行态仓储。该仓储保存任务、实例和任务内父子关系树的当前事实。
        self._repository = repository

        # 服务级操作锁。创建 task+root、创建 child+关系、状态流转都需要跨多个对象保持原子性。
        self._lock = RLock()

    def create_task_with_root_agent_instance(
        self,
        *,
        thread_id: str,
        current_working_directory: str,
        agent_definition_id: str,
        llm_provider_id: str,
        llm_model_binding_id: str,
        llm_policy_group_id: str | None = None,
        llm_policy_snapshot_json: dict[str, Any] | None = None,
        task_id: str | None = None,
        root_instance_id: str | None = None,
        conversation_id: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[AgentTask, AgentInstance]:
        """创建任务并同时创建唯一根 Agent 实例。

        业务规则：
        - 任务创建必须带根 Agent 实例，避免出现无入口任务；
        - 一个任务只能创建一次根实例；
        - 任务数量不设上限；
        - 根实例没有父实例，是 task 内父子关系树的根节点。
        """
        actual_task_id = task_id or _new_id("task")
        actual_root_instance_id = root_instance_id or _new_id("agent-instance")
        actual_trace_id = trace_id or _new_id("trace")
        with self._lock:
            self._ensure_task_not_exists(actual_task_id)
            self._ensure_instance_not_exists(actual_root_instance_id)
            root_instance = AgentInstance(
                instance_id=actual_root_instance_id,
                task_id=actual_task_id,
                thread_id=thread_id,
                current_working_directory=current_working_directory,
                agent_definition_id=agent_definition_id,
                llm_provider_id=llm_provider_id,
                llm_model_binding_id=llm_model_binding_id,
                conversation_id=conversation_id,
                trace_id=actual_trace_id,
                llm_policy_group_id=llm_policy_group_id,
                llm_policy_snapshot_json=llm_policy_snapshot_json or {},
            )
            task = AgentTask(
                task_id=actual_task_id,
                root_agent_instance_id=actual_root_instance_id,
                agent_instance_ids=(actual_root_instance_id,),
            )
            self._repository.save_task(task)
            self._repository.save_agent_instance(root_instance)
            logger.info(
                "创建 Agent 任务及根实例: task_id=%s root_instance_id=%s",
                actual_task_id,
                actual_root_instance_id,
            )
            return task, root_instance

    def create_child_agent_instance(
        self,
        *,
        task_id: str,
        parent_instance_id: str,
        thread_id: str,
        current_working_directory: str,
        agent_definition_id: str,
        llm_provider_id: str,
        llm_model_binding_id: str,
        llm_policy_group_id: str | None = None,
        llm_policy_snapshot_json: dict[str, Any] | None = None,
        child_instance_id: str | None = None,
        conversation_id: str | None = None,
        trace_id: str | None = None,
        relation_id: str | None = None,
    ) -> tuple[AgentTask, AgentInstance, AgentParentChildRelation]:
        """在任务父子关系树中创建一个子 Agent 实例。

        业务规则：
        - task 必须存在；
        - parent_instance_id 必须属于该 task；
        - child_instance_id 不能已存在；
        - 子实例创建后立即挂到 parent 下；
        - 非根实例必须有父实例。
        """
        actual_child_instance_id = child_instance_id or _new_id("agent-instance")
        actual_trace_id = trace_id or _new_id("trace")
        actual_relation_id = relation_id or self._build_relation_id(
            task_id,
            parent_instance_id,
            actual_child_instance_id,
        )
        with self._lock:
            task = self._get_required_task(task_id)
            parent_instance = self._get_required_instance(parent_instance_id)
            if parent_instance.task_id != task_id:
                raise ValueError("父 Agent 实例不属于指定任务。")
            self._ensure_instance_not_exists(actual_child_instance_id)
            child_instance = AgentInstance(
                instance_id=actual_child_instance_id,
                task_id=task_id,
                thread_id=thread_id,
                current_working_directory=current_working_directory,
                agent_definition_id=agent_definition_id,
                llm_provider_id=llm_provider_id,
                llm_model_binding_id=llm_model_binding_id,
                conversation_id=conversation_id,
                trace_id=actual_trace_id,
                llm_policy_group_id=llm_policy_group_id,
                llm_policy_snapshot_json=llm_policy_snapshot_json or {},
            )
            updated_task = task.add_child_relation(
                child_instance_id=actual_child_instance_id,
                parent_instance_id=parent_instance_id,
                relation_id=actual_relation_id,
            )
            relation = updated_task.parent_child_relations[-1]
            self._repository.save_task(updated_task)
            self._repository.save_agent_instance(child_instance)
            logger.info(
                "创建子 Agent 实例: task_id=%s parent=%s child=%s",
                task_id,
                parent_instance_id,
                actual_child_instance_id,
            )
            return updated_task, child_instance, relation

    def list_tasks(self) -> list[AgentTask]:
        """列出当前项目运行时内所有任务。任务数量不设上限。"""
        return self._repository.list_tasks()

    def get_task(self, task_id: str) -> AgentTask:
        """按 task_id 获取任务，不存在时抛出 KeyError。"""
        return self._get_required_task(task_id)

    def get_agent_instance(self, instance_id: str) -> AgentInstance:
        """按 instance_id 获取 Agent 实例，不存在时抛出 KeyError。"""
        return self._get_required_instance(instance_id)

    def list_task_agent_instances(self, task_id: str) -> list[AgentInstance]:
        """列出任务内所有 Agent 实例。"""
        self._get_required_task(task_id)
        return self._repository.list_agent_instances_by_task(task_id)

    def list_thread_agent_instances(self, thread_id: str) -> list[AgentInstance]:
        """列出某个线程下的全部 Agent 实例。"""
        _require_non_blank(thread_id, "thread_id")
        return self._repository.list_agent_instances_by_thread(thread_id)

    def list_task_parent_child_relations(
        self,
        task_id: str,
    ) -> tuple[AgentParentChildRelation, ...]:
        """列出任务内的 Agent 父子关系树边集合。"""
        task = self._get_required_task(task_id)
        return task.parent_child_relations

    def list_child_agent_instances(self, parent_instance_id: str) -> list[AgentInstance]:
        """列出指定 Agent 实例的直接子实例。"""
        parent_instance = self._get_required_instance(parent_instance_id)
        task = self._get_required_task(parent_instance.task_id)
        child_ids = task.children_of(parent_instance_id)
        return [self._get_required_instance(child_id) for child_id in child_ids]

    def get_parent_agent_instance(self, child_instance_id: str) -> AgentInstance | None:
        """查询指定 Agent 实例的父实例；根实例返回 None。"""
        child_instance = self._get_required_instance(child_instance_id)
        task = self._get_required_task(child_instance.task_id)
        parent_id = task.parent_of(child_instance_id)
        if parent_id is None:
            return None
        return self._get_required_instance(parent_id)

    def start_task(self, task_id: str) -> AgentTask:
        """将任务状态流转为 running。"""
        return self._transition_task(task_id, AGENT_TASK_STATUS_RUNNING)

    def pause_task(self, task_id: str) -> AgentTask:
        """将任务状态流转为 paused。"""
        return self._transition_task(task_id, AGENT_TASK_STATUS_PAUSED)

    def resume_task(self, task_id: str) -> AgentTask:
        """将暂停任务恢复为 running。"""
        return self._transition_task(task_id, AGENT_TASK_STATUS_RUNNING)

    def complete_task(self, task_id: str) -> AgentTask:
        """显式标记任务完成。任务完成不由单个实例完成自动推导。"""
        return self._transition_task(task_id, AGENT_TASK_STATUS_COMPLETED)

    def fail_task(self, task_id: str, failure_message: str) -> AgentTask:
        """标记任务失败。"""
        return self._transition_task(
            task_id,
            AGENT_TASK_STATUS_FAILED,
            failure_message=failure_message,
        )

    def cancel_task(self, task_id: str) -> AgentTask:
        """取消任务。"""
        return self._transition_task(task_id, AGENT_TASK_STATUS_CANCELLED)

    def start_agent_instance(self, instance_id: str) -> AgentInstance:
        """将 Agent 实例状态流转为 running。"""
        return self._transition_instance(instance_id, AGENT_INSTANCE_STATUS_RUNNING)

    def pause_agent_instance(self, instance_id: str) -> AgentInstance:
        """将 Agent 实例状态流转为 paused。"""
        return self._transition_instance(instance_id, AGENT_INSTANCE_STATUS_PAUSED)

    def resume_agent_instance(self, instance_id: str) -> AgentInstance:
        """将暂停 Agent 实例恢复为 running。"""
        return self._transition_instance(instance_id, AGENT_INSTANCE_STATUS_RUNNING)

    def complete_agent_instance(self, instance_id: str) -> AgentInstance:
        """显式标记 Agent 实例完成。"""
        return self._transition_instance(instance_id, AGENT_INSTANCE_STATUS_COMPLETED)

    def fail_agent_instance(
        self,
        instance_id: str,
        failure_message: str,
    ) -> AgentInstance:
        """标记 Agent 实例失败。"""
        return self._transition_instance(
            instance_id,
            AGENT_INSTANCE_STATUS_FAILED,
            failure_message=failure_message,
        )

    def cancel_agent_instance(self, instance_id: str) -> AgentInstance:
        """取消 Agent 实例。"""
        return self._transition_instance(instance_id, AGENT_INSTANCE_STATUS_CANCELLED)

    def bind_agent_instance_conversation(
        self,
        instance_id: str,
        conversation_id: str,
    ) -> AgentInstance:
        """为 Agent 实例绑定历史会话 ID。

        执行循环创建 Conversation 发生在实例创建之后时，需要把 conversation_id 写回实例运行态，
        让 task/instance 管理视图能够直接追溯该实例对应的历史对话。
        """
        with self._lock:
            instance = self._get_required_instance(instance_id)
            updated = instance.bind_conversation(conversation_id)
            self._repository.save_agent_instance(updated)
            return updated

    def switch_agent_instance_llm_policy(
        self,
        instance_id: str,
        *,
        llm_policy_group_id: str,
        llm_policy_snapshot_json: dict[str, Any],
        llm_provider_id: str,
        llm_model_binding_id: str,
    ) -> AgentInstance:
        with self._lock:
            instance = self._get_required_instance(instance_id)
            if instance.is_terminal:
                raise ValueError(f"Agent 实例已结束，不能切换 LLM 执行策略组: {instance_id}")
            updated = instance.switch_llm_policy(
                llm_policy_group_id=llm_policy_group_id,
                llm_policy_snapshot_json=llm_policy_snapshot_json,
                llm_provider_id=llm_provider_id,
                llm_model_binding_id=llm_model_binding_id,
            )
            self._repository.save_agent_instance(updated)
            return updated

    def _transition_task(
        self,
        task_id: str,
        target_status: AgentTaskStatus,
        *,
        failure_message: str | None = None,
    ) -> AgentTask:
        """执行任务状态流转并保存。"""
        with self._lock:
            task = self._get_required_task(task_id)
            updated = task.transition_to(
                target_status,
                failure_message=failure_message,
            )
            self._repository.save_task(updated)
            return updated

    def _transition_instance(
        self,
        instance_id: str,
        target_status: AgentInstanceStatus,
        *,
        failure_message: str | None = None,
    ) -> AgentInstance:
        """执行 Agent 实例状态流转并保存。"""
        with self._lock:
            instance = self._get_required_instance(instance_id)
            updated = instance.transition_to(
                target_status,
                failure_message=failure_message,
            )
            self._repository.save_agent_instance(updated)
            return updated

    def _get_required_task(self, task_id: str) -> AgentTask:
        """获取必然存在的任务。"""
        _require_non_blank(task_id, "task_id")
        task = self._repository.get_task(task_id)
        if task is None:
            raise KeyError(f"Agent 任务不存在: {task_id}")
        return task

    def _get_required_instance(self, instance_id: str) -> AgentInstance:
        """获取必然存在的 Agent 实例。"""
        _require_non_blank(instance_id, "instance_id")
        instance = self._repository.get_agent_instance(instance_id)
        if instance is None:
            raise KeyError(f"Agent 实例不存在: {instance_id}")
        return instance

    def _ensure_task_not_exists(self, task_id: str) -> None:
        """确保任务 ID 尚未被占用。"""
        _require_non_blank(task_id, "task_id")
        if self._repository.contains_task(task_id):
            raise ValueError(f"Agent 任务已存在: {task_id}")

    def _ensure_instance_not_exists(self, instance_id: str) -> None:
        """确保实例 ID 尚未被占用。"""
        _require_non_blank(instance_id, "instance_id")
        if self._repository.contains_agent_instance(instance_id):
            raise ValueError(f"Agent 实例已存在: {instance_id}")

    def _build_relation_id(
        self,
        task_id: str,
        parent_instance_id: str,
        child_instance_id: str,
    ) -> str:
        """生成任务内父子关系 ID。"""
        return f"relation-{task_id}-{parent_instance_id}-{child_instance_id}"

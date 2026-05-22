# -*- coding: utf-8 -*-
"""Agent 运行时任务与实例管理领域服务。"""
from __future__ import annotations

import logging
from threading import RLock
from typing import Any
from uuid import uuid4

from agent_runtime.agent.agent_instance import (
    AGENT_INSTANCE_STATUS_READY,
    AGENT_INSTANCE_STATUS_RUNNING,
    AgentInstance,
    AgentInstanceStatus,
    PendingAgentUserMessage,
    PendingUserMessageAttachment,
)
from agent_runtime.agent.agent_runtime_repository import AgentRuntimeRepository
from agent_runtime.agent.agent_task import (
    AGENT_TASK_STATUS_COMPLETED,
    AGENT_TASK_STATUS_FAILED,
    AGENT_TASK_STATUS_UNFINISHED,
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
    Agent 实例以及一棵父子关系树。Scene 绑定的 root agent instance 是长期复用的
    主 Agent 运行主体：同一 Scene 下可以依次创建多个 task，并把同一个 root instance
    切换到最新 task 上执行，从而隔离 task/conversation 数据但复用 Agent 身份与策略。
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
        scene_id: str | None = None,
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
                scene_id=scene_id,
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

    def list_tasks_by_scene_id(self, scene_id: str) -> list[AgentTask]:
        """列出指定 scene 下的全部任务，按 created_at 倒序（最近在前）。"""
        _require_non_blank(scene_id, "scene_id")
        return self._repository.list_tasks_by_scene_id(scene_id)

    def get_active_root_agent_instance_by_scene(
        self,
        scene_id: str,
    ) -> tuple[AgentTask, AgentInstance] | None:
        """查询 scene 下当前活跃的 root agent instance。

        判定规则：
        - 取该 scene 下最近一条 task（created_at DESC），若 task 已终态则视为无活跃；
        - 再取该 task 的 root_agent_instance_id 对应实例；
        - task 未终态且 root instance 存在时返回 (task, instance)。

        本方法不抛错，scene 不存在 / 无任务 / 已终态都返回 None。
        """
        _require_non_blank(scene_id, "scene_id")
        tasks = self._repository.list_tasks_by_scene_id(scene_id)
        if not tasks:
            return None
        latest_task = tasks[0]
        if latest_task.is_terminal:
            return None
        instance = self._repository.get_agent_instance(latest_task.root_agent_instance_id)
        if instance is None:
            return None
        return latest_task, instance

    def get_last_root_agent_instance_by_scene(
        self,
        scene_id: str,
    ) -> tuple[AgentTask, AgentInstance] | None:
        """查询 scene 下最近一条 root agent instance，无论是否终态。

        用于实例终态后重建时继承上一个实例的 LLM 策略与 conversation_id。
        scene 不存在 / 无任务 / 实例缺失 都返回 None。
        """
        _require_non_blank(scene_id, "scene_id")
        tasks = self._repository.list_tasks_by_scene_id(scene_id)
        if not tasks:
            return None
        latest_task = tasks[0]
        instance = self._repository.get_agent_instance(latest_task.root_agent_instance_id)
        if instance is None:
            return None
        return latest_task, instance

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
        """确认任务仍处于未完成生命周期状态。

        Task 不再表达 running，执行循环是否正在运行由 AgentInstance.status 表达。
        """
        return self._transition_task(task_id, AGENT_TASK_STATUS_UNFINISHED)

    def pause_task(self, task_id: str) -> AgentTask:
        """确认任务仍处于未完成生命周期状态。

        暂停是执行载体语义，不改变 Task 生命周期状态。
        """
        return self._transition_task(task_id, AGENT_TASK_STATUS_UNFINISHED)

    def resume_task(self, task_id: str) -> AgentTask:
        """确认任务仍处于未完成生命周期状态。"""
        return self._transition_task(task_id, AGENT_TASK_STATUS_UNFINISHED)

    def complete_task(self, task_id: str) -> AgentTask:
        """显式标记任务完成。任务完成不由单个实例完成自动推导。"""
        return self._transition_task(task_id, AGENT_TASK_STATUS_COMPLETED)

    def complete_scene_task_and_reset_root_agent_instance(
        self,
        task_id: str,
        root_instance_id: str,
    ) -> tuple[AgentTask, AgentInstance]:
        """完成 Scene 当前 task，并把长期复用的 root agent instance 恢复为待命状态。

        end_task 在 Scene 语境下只表示“当前用户任务完成”，不能销毁 Scene 的主 Agent。
        该方法在同一服务锁内完成 task 终态保存与 root instance 运行态清理：
        - task 进入 completed，保留历史审计记录；
        - root instance 保留 instance_id、Agent 定义、LLM 策略和工作目录；
        - root instance 清空本轮 conversation_id，状态回到 ready，等待下一条用户消息创建新 task。
        """
        with self._lock:
            task = self._get_required_task(task_id)
            instance = self._get_required_instance(root_instance_id)
            self._ensure_scene_root_instance(task, instance)
            updated_task = task.transition_to(AGENT_TASK_STATUS_COMPLETED)
            updated_instance = instance.reset_for_next_scene_task()
            self._repository.save_task(updated_task)
            self._repository.save_agent_instance(updated_instance)
            return updated_task, updated_instance

    def reset_scene_root_agent_instance_for_next_task(
        self,
        task_id: str,
        root_instance_id: str,
    ) -> AgentInstance:
        """把已完成 task 上的 Scene root agent instance 重置为可接收新任务。

        该方法用于兼容旧版本 end_task 曾把 root instance 置为 completed 的历史数据，
        以及 attach 到已完成 task 时恢复 Scene 主 Agent 的待命态。它不会创建新 task。
        """
        with self._lock:
            task = self._get_required_task(task_id)
            instance = self._get_required_instance(root_instance_id)
            self._ensure_scene_root_instance(task, instance)
            if not task.is_terminal:
                raise ValueError(f"Scene task 尚未结束，不能重置 root agent instance: {task_id}")
            updated_instance = instance.reset_for_next_scene_task()
            self._repository.save_agent_instance(updated_instance)
            return updated_instance

    def prepare_scene_root_agent_instance_for_new_task(
        self,
        root_instance_id: str,
    ) -> tuple[AgentTask, AgentInstance]:
        """为同一 Scene 的下一条用户消息创建新 task，并复用既有 root agent instance。

        当前 root instance 仍指向上一轮已终态 task。下一条用户消息到来时，
        系统在这里创建新的 task_id/thread_id/trace_id，把同一个 root instance
        切换到新 task，并保持 conversation_id 为空，确保新 task 会创建独立 conversation。
        """
        with self._lock:
            instance = self._get_required_instance(root_instance_id)
            previous_task = self._get_required_task(instance.task_id)
            self._ensure_scene_root_instance(previous_task, instance)
            if not previous_task.is_terminal:
                return previous_task, instance
            new_task_id = _new_id("task")
            new_thread_id = _new_id("thread")
            new_trace_id = _new_id("trace")
            self._ensure_task_not_exists(new_task_id)
            next_task = AgentTask(
                task_id=new_task_id,
                root_agent_instance_id=instance.instance_id,
                agent_instance_ids=(instance.instance_id,),
                scene_id=previous_task.scene_id,
            )
            updated_instance = instance.assign_to_new_scene_task(
                task_id=new_task_id,
                thread_id=new_thread_id,
                trace_id=new_trace_id,
            )
            self._repository.save_task(next_task)
            self._repository.save_agent_instance(updated_instance)
            logger.info(
                "Scene root Agent 实例复用并切换到新任务: scene_id=%s root_instance_id=%s previous_task_id=%s next_task_id=%s",
                previous_task.scene_id,
                root_instance_id,
                previous_task.task_id,
                new_task_id,
            )
            return next_task, updated_instance

    def fail_task(self, task_id: str, failure_message: str) -> AgentTask:
        """标记任务失败。"""
        return self._transition_task(
            task_id,
            AGENT_TASK_STATUS_FAILED,
            failure_message=failure_message,
        )

    def cancel_task(self, task_id: str) -> AgentTask:
        """取消任务。取消属于失败结束的一种业务原因。"""
        return self._transition_task(
            task_id,
            AGENT_TASK_STATUS_FAILED,
            failure_message="任务被用户或系统取消。",
        )

    def start_agent_instance(self, instance_id: str) -> AgentInstance:
        """将 Agent 实例执行载体标记为 running。"""
        return self._transition_instance(instance_id, AGENT_INSTANCE_STATUS_RUNNING)

    def pause_agent_instance(self, instance_id: str) -> AgentInstance:
        """将 Agent 实例执行载体恢复为 ready。"""
        return self._transition_instance(instance_id, AGENT_INSTANCE_STATUS_READY)

    def resume_agent_instance(self, instance_id: str) -> AgentInstance:
        """将 Agent 实例执行载体重新标记为 running。"""
        return self._transition_instance(instance_id, AGENT_INSTANCE_STATUS_RUNNING)

    def complete_agent_instance(self, instance_id: str) -> AgentInstance:
        """任务完成后将 Agent 实例执行载体恢复为 ready。"""
        return self._transition_instance(instance_id, AGENT_INSTANCE_STATUS_READY)

    def fail_agent_instance(
        self,
        instance_id: str,
        failure_message: str,
    ) -> AgentInstance:
        """记录实例最近失败原因，并将执行载体恢复为 ready。"""
        return self._transition_instance(
            instance_id,
            AGENT_INSTANCE_STATUS_READY,
            failure_message=failure_message,
        )

    def interrupt_agent_instance(
        self,
        instance_id: str,
        interruption_message: str,
    ) -> AgentInstance:
        """记录技术性执行中断，并将执行载体恢复为 ready。

        该方法用于 LLM 请求失败、网络异常、上下文超限等可恢复中断；它不会改变 Task
        生命周期状态，用户后续发消息可继续同一个任务。
        """
        return self._transition_instance(
            instance_id,
            AGENT_INSTANCE_STATUS_READY,
            failure_message=interruption_message,
        )

    def cancel_agent_instance(self, instance_id: str) -> AgentInstance:
        """取消当前实例执行载体，把它恢复为 ready。"""
        return self._transition_instance(
            instance_id,
            AGENT_INSTANCE_STATUS_READY,
            failure_message="Agent 实例执行被取消。",
        )

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

    def switch_agent_instance_conversation(
        self,
        instance_id: str,
        conversation_id: str,
    ) -> AgentInstance:
        with self._lock:
            instance = self._get_required_instance(instance_id)
            updated = instance.switch_conversation(conversation_id)
            self._repository.save_agent_instance(updated)
            return updated

    def enqueue_agent_instance_user_message(
        self,
        instance_id: str,
        *,
        user_text: str,
        attachments: tuple[PendingUserMessageAttachment, ...] = (),
        client_message_id: str | None = None,
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[AgentInstance, PendingAgentUserMessage]:
        """把用户消息追加到 Agent 实例待处理队列。

        所有用户发给 AgentInstance 的消息都先进入该 FIFO 队列；执行循环只在完整轮次边界
        drain 队列并追加到 conversation，避免打断当前 LLM 请求或工具执行。
        """
        if not isinstance(user_text, str):
            raise ValueError("user_text 必须是字符串。")
        if not user_text.strip() and not attachments:
            raise ValueError("user_text 和 attachments 必须至少提供一个。")
        actual_message_id = message_id or _new_id("pending-user-message")
        pending_message = PendingAgentUserMessage(
            message_id=actual_message_id,
            user_text=user_text,
            attachments=attachments,
            client_message_id=client_message_id,
            metadata=dict(metadata or {}),
        )

        with self._lock:
            instance = self._get_required_instance(instance_id)
            updated = instance.enqueue_user_message(pending_message)
            self._repository.save_agent_instance(updated)
            return updated, pending_message

    def drain_agent_instance_user_messages(
        self,
        instance_id: str,
    ) -> tuple[AgentInstance, tuple[PendingAgentUserMessage, ...]]:
        """按 FIFO 顺序取出并清空 Agent 实例待处理用户消息队列。"""
        with self._lock:
            instance = self._get_required_instance(instance_id)
            updated, drained_messages = instance.drain_pending_user_messages()
            if drained_messages:
                self._repository.save_agent_instance(updated)
            return updated, drained_messages

    def remove_agent_instance_user_message(
        self,
        instance_id: str,
        message_id: str,
    ) -> tuple[AgentInstance, PendingAgentUserMessage | None]:
        _require_non_blank(message_id, "message_id")
        with self._lock:
            instance = self._get_required_instance(instance_id)
            updated, removed_message = instance.remove_pending_user_message(message_id)
            if removed_message is not None:
                self._repository.save_agent_instance(updated)
            return updated, removed_message

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

    def _ensure_scene_root_instance(self, task: AgentTask, instance: AgentInstance) -> None:
        """校验实例是指定 Scene task 的 root agent instance。"""
        if task.scene_id is None:
            raise ValueError(f"任务未绑定 Scene，不能按 Scene root agent instance 复用: {task.task_id}")
        if task.root_agent_instance_id != instance.instance_id:
            raise ValueError(
                f"Agent 实例不是 Scene task 的 root instance: "
                f"task_id={task.task_id} root={task.root_agent_instance_id} instance={instance.instance_id}"
            )

    def _build_relation_id(
        self,
        task_id: str,
        parent_instance_id: str,
        child_instance_id: str,
    ) -> str:
        """生成任务内父子关系 ID。"""
        return f"relation-{task_id}-{parent_instance_id}-{child_instance_id}"
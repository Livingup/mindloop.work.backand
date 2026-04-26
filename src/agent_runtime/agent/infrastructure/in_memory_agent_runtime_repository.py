# -*- coding: utf-8 -*-
"""线程安全的 Agent 运行时内存仓储。"""
from __future__ import annotations

from threading import RLock

from agent_runtime.agent.agent_instance import AgentInstance
from agent_runtime.agent.agent_runtime_repository import AgentRuntimeRepository
from agent_runtime.agent.agent_task import AgentTask


class InMemoryAgentRuntimeRepository(AgentRuntimeRepository):
    """进程内 Agent 任务与实例运行态仓储。

    该实现用于单 Python 进程内的任务/实例动态管理。它使用 RLock 保护所有读写操作，
    支持同一进程内多个 task 并发、同一 task 内多个 agent instance 并发的运行态登记。
    多进程或多机器共享运行态需要后续替换为 SQLite/PostgreSQL 等共享仓储实现。
    """

    def __init__(self) -> None:
        # 运行态仓储锁。task、instance 和索引需要在同一临界区内保持一致。
        self._lock = RLock()

        # task_id -> AgentTask。保存任务状态、唯一根实例和父子关系树。
        self._tasks: dict[str, AgentTask] = {}

        # instance_id -> AgentInstance。保存每个 Agent 实例独立状态和 LLM 选择信息。
        self._instances: dict[str, AgentInstance] = {}

        # thread_id -> instance_id 集合。用于按线程查询 Agent 实例。
        self._instances_by_thread: dict[str, set[str]] = {}

    def save_task(self, task: AgentTask) -> None:
        """保存或覆盖任务运行态。"""
        with self._lock:
            self._tasks[task.task_id] = task

    def get_task(self, task_id: str) -> AgentTask | None:
        """按 task_id 获取任务运行态。"""
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self) -> list[AgentTask]:
        """列出当前仓储中的全部任务。"""
        with self._lock:
            return sorted(self._tasks.values(), key=lambda item: item.created_at)

    def save_agent_instance(self, instance: AgentInstance) -> None:
        """保存或覆盖 Agent 实例运行态，并维护线程索引。"""
        with self._lock:
            old = self._instances.get(instance.instance_id)
            if old is not None and old.thread_id != instance.thread_id:
                old_bucket = self._instances_by_thread.get(old.thread_id)
                if old_bucket is not None:
                    old_bucket.discard(instance.instance_id)
                    if not old_bucket:
                        self._instances_by_thread.pop(old.thread_id, None)
            self._instances[instance.instance_id] = instance
            self._instances_by_thread.setdefault(instance.thread_id, set()).add(
                instance.instance_id
            )

    def get_agent_instance(self, instance_id: str) -> AgentInstance | None:
        """按 instance_id 获取 Agent 实例运行态。"""
        with self._lock:
            return self._instances.get(instance_id)

    def list_agent_instances_by_task(self, task_id: str) -> list[AgentInstance]:
        """按任务内实例顺序列出某个任务下的全部 Agent 实例。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return []
            instances: list[AgentInstance] = []
            for instance_id in task.agent_instance_ids:
                instance = self._instances.get(instance_id)
                if instance is not None:
                    instances.append(instance)
            return instances

    def list_agent_instances_by_thread(self, thread_id: str) -> list[AgentInstance]:
        """列出某个线程下的全部 Agent 实例。"""
        with self._lock:
            instance_ids = self._instances_by_thread.get(thread_id, set())
            instances = [
                self._instances[instance_id]
                for instance_id in instance_ids
                if instance_id in self._instances
            ]
            return sorted(instances, key=lambda item: item.created_at)

    def contains_task(self, task_id: str) -> bool:
        """判断任务是否存在。"""
        with self._lock:
            return task_id in self._tasks

    def contains_agent_instance(self, instance_id: str) -> bool:
        """判断 Agent 实例是否存在。"""
        with self._lock:
            return instance_id in self._instances

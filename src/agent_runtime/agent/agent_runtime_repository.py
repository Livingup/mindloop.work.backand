# -*- coding: utf-8 -*-
"""Agent 运行时任务与实例仓储接口。"""
from __future__ import annotations

from typing import Protocol

from agent_runtime.agent.agent_instance import AgentInstance
from agent_runtime.agent.agent_task import AgentTask


class AgentRuntimeRepository(Protocol):
    """Agent 运行时仓储接口。

    该仓储以集合语义管理任务和任务内 Agent 实例运行态。
    具体实现可以是进程内内存、SQLite 或其他共享存储；领域服务只依赖该接口。
    """

    def save_task(self, task: AgentTask) -> None:
        """保存或覆盖任务运行态。"""

    def get_task(self, task_id: str) -> AgentTask | None:
        """按 task_id 获取任务运行态。"""

    def list_tasks(self) -> list[AgentTask]:
        """列出当前仓储中的全部任务。"""

    def save_agent_instance(self, instance: AgentInstance) -> None:
        """保存或覆盖 Agent 实例运行态。"""

    def get_agent_instance(self, instance_id: str) -> AgentInstance | None:
        """按 instance_id 获取 Agent 实例运行态。"""

    def list_agent_instances_by_task(self, task_id: str) -> list[AgentInstance]:
        """列出某个任务下的全部 Agent 实例。"""

    def list_agent_instances_by_thread(self, thread_id: str) -> list[AgentInstance]:
        """列出某个线程下的全部 Agent 实例。"""

    def contains_task(self, task_id: str) -> bool:
        """判断任务是否存在。"""

    def contains_agent_instance(self, instance_id: str) -> bool:
        """判断 Agent 实例是否存在。"""

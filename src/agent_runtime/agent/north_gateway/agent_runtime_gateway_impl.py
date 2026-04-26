# -*- coding: utf-8 -*-
"""Agent 运行时任务与实例管理北向网关实现。"""
from __future__ import annotations

from agent_runtime.agent.agent_instance import AgentInstance
from agent_runtime.agent.agent_runtime_service import AgentRuntimeService
from agent_runtime.agent.agent_task import AgentParentChildRelation, AgentTask
from agent_runtime.agent.north_gateway.agent_runtime_contracts import (
    AgentRuntimeGateway,
    CreateChildAgentInstanceRequest,
    CreateTaskWithRootAgentInstanceRequest,
)


class AgentRuntimeGatewayImpl(AgentRuntimeGateway):
    """Agent 运行时任务与实例管理北向网关实现。

    该实现只做契约到领域服务的转发，保持北向调用方与领域服务内部实现解耦。
    后续 Agent 执行引擎、内部调度器或管理接口都应依赖 AgentRuntimeGateway 契约。
    """

    def __init__(self, service: AgentRuntimeService) -> None:
        # Agent 运行时领域服务。负责维护任务、实例状态和任务内父子关系树。
        self._service = service

    def create_task_with_root_agent_instance(
        self,
        request: CreateTaskWithRootAgentInstanceRequest,
    ) -> tuple[AgentTask, AgentInstance]:
        """创建任务并同时创建唯一根 Agent 实例。"""
        return self._service.create_task_with_root_agent_instance(
            task_id=request.task_id,
            root_instance_id=request.root_instance_id,
            thread_id=request.thread_id,
            agent_definition_id=request.agent_definition_id,
            llm_provider_id=request.llm_provider_id,
            llm_model_binding_id=request.llm_model_binding_id,
            conversation_id=request.conversation_id,
            trace_id=request.trace_id,
        )

    def create_child_agent_instance(
        self,
        request: CreateChildAgentInstanceRequest,
    ) -> tuple[AgentTask, AgentInstance, AgentParentChildRelation]:
        """在任务父子关系树中创建一个子 Agent 实例。"""
        return self._service.create_child_agent_instance(
            task_id=request.task_id,
            parent_instance_id=request.parent_instance_id,
            child_instance_id=request.child_instance_id,
            thread_id=request.thread_id,
            agent_definition_id=request.agent_definition_id,
            llm_provider_id=request.llm_provider_id,
            llm_model_binding_id=request.llm_model_binding_id,
            conversation_id=request.conversation_id,
            trace_id=request.trace_id,
            relation_id=request.relation_id,
        )

    def list_tasks(self) -> list[AgentTask]:
        """列出当前运行时内所有任务。"""
        return self._service.list_tasks()

    def get_task(self, task_id: str) -> AgentTask:
        """按 task_id 获取任务。"""
        return self._service.get_task(task_id)

    def get_agent_instance(self, instance_id: str) -> AgentInstance:
        """按 instance_id 获取 Agent 实例。"""
        return self._service.get_agent_instance(instance_id)

    def list_task_agent_instances(self, task_id: str) -> list[AgentInstance]:
        """列出任务内全部 Agent 实例。"""
        return self._service.list_task_agent_instances(task_id)

    def list_thread_agent_instances(self, thread_id: str) -> list[AgentInstance]:
        """列出线程下全部 Agent 实例。"""
        return self._service.list_thread_agent_instances(thread_id)

    def list_task_parent_child_relations(
        self,
        task_id: str,
    ) -> tuple[AgentParentChildRelation, ...]:
        """列出任务内 Agent 父子关系树边集合。"""
        return self._service.list_task_parent_child_relations(task_id)

    def list_child_agent_instances(self, parent_instance_id: str) -> list[AgentInstance]:
        """列出指定 Agent 实例的直接子实例。"""
        return self._service.list_child_agent_instances(parent_instance_id)

    def get_parent_agent_instance(self, child_instance_id: str) -> AgentInstance | None:
        """查询指定 Agent 实例的父实例；根实例返回 None。"""
        return self._service.get_parent_agent_instance(child_instance_id)

    def start_task(self, task_id: str) -> AgentTask:
        """将任务状态流转为 running。"""
        return self._service.start_task(task_id)

    def pause_task(self, task_id: str) -> AgentTask:
        """将任务状态流转为 paused。"""
        return self._service.pause_task(task_id)

    def resume_task(self, task_id: str) -> AgentTask:
        """将任务状态从 paused 恢复为 running。"""
        return self._service.resume_task(task_id)

    def complete_task(self, task_id: str) -> AgentTask:
        """显式标记任务完成。"""
        return self._service.complete_task(task_id)

    def fail_task(self, task_id: str, failure_message: str) -> AgentTask:
        """标记任务失败。"""
        return self._service.fail_task(task_id, failure_message)

    def cancel_task(self, task_id: str) -> AgentTask:
        """取消任务。"""
        return self._service.cancel_task(task_id)

    def start_agent_instance(self, instance_id: str) -> AgentInstance:
        """将 Agent 实例状态流转为 running。"""
        return self._service.start_agent_instance(instance_id)

    def pause_agent_instance(self, instance_id: str) -> AgentInstance:
        """将 Agent 实例状态流转为 paused。"""
        return self._service.pause_agent_instance(instance_id)

    def resume_agent_instance(self, instance_id: str) -> AgentInstance:
        """将暂停 Agent 实例恢复为 running。"""
        return self._service.resume_agent_instance(instance_id)

    def complete_agent_instance(self, instance_id: str) -> AgentInstance:
        """显式标记 Agent 实例完成。"""
        return self._service.complete_agent_instance(instance_id)

    def fail_agent_instance(
        self,
        instance_id: str,
        failure_message: str,
    ) -> AgentInstance:
        """标记 Agent 实例失败。"""
        return self._service.fail_agent_instance(instance_id, failure_message)

    def cancel_agent_instance(self, instance_id: str) -> AgentInstance:
        """取消 Agent 实例。"""
        return self._service.cancel_agent_instance(instance_id)

# -*- coding: utf-8 -*-
"""Agent 运行时任务与实例管理北向网关实现。"""
from __future__ import annotations

from uuid import uuid4

from agent_runtime.agent.agent_instance import AgentInstance
from agent_runtime.agent.agent_runtime_service import AgentRuntimeService
from agent_runtime.agent.agent_task import AgentParentChildRelation, AgentTask
from agent_runtime.agent.north_gateway.agent_runtime_contracts import (
    AgentRuntimeGateway,
    AttachSceneRequest,
    AttachSceneResponse,
    CreateChildAgentInstanceRequest,
    CreateTaskWithRootAgentInstanceRequest,
)
from agent_runtime.scene.north_gateway.scene_contracts import SceneGateway


class AgentRuntimeGatewayImpl(AgentRuntimeGateway):
    """Agent 运行时任务与实例管理北向网关实现。

    该实现只做契约到领域服务的转发，保持北向调用方与领域服务内部实现解耦。
    后续 Agent 执行引擎、内部调度器或管理接口都应依赖 AgentRuntimeGateway 契约。

    可选注入 SceneGateway 以支持 attach_scene 编排：当不需要 scene 维度入口时（例如
    单元测试或纯任务管理脚本）可以省略，调用 attach_scene 会抛 RuntimeError。
    """

    def __init__(
        self,
        service: AgentRuntimeService,
        scene_gateway: SceneGateway | None = None,
    ) -> None:
        # Agent 运行时领域服务。负责维护任务、实例状态和任务内父子关系树。
        self._service = service
        # Scene 北向网关。attach_scene 用其读取 bound_agent_definition_id /
        # bound_root_conversation_id / project.workspace_path 以及回写 conversation 绑定。
        self._scene_gateway = scene_gateway

    def create_task_with_root_agent_instance(
        self,
        request: CreateTaskWithRootAgentInstanceRequest,
    ) -> tuple[AgentTask, AgentInstance]:
        """创建任务并同时创建唯一根 Agent 实例。"""
        return self._service.create_task_with_root_agent_instance(
            task_id=request.task_id,
            root_instance_id=request.root_instance_id,
            thread_id=request.thread_id,
            current_working_directory=request.current_working_directory,
            agent_definition_id=request.agent_definition_id,
            llm_provider_id=request.llm_provider_id,
            llm_model_binding_id=request.llm_model_binding_id,
            llm_policy_group_id=request.llm_policy_group_id,
            llm_policy_snapshot_json=request.llm_policy_snapshot_json,
            conversation_id=request.conversation_id,
            trace_id=request.trace_id,
            scene_id=request.scene_id,
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
            current_working_directory=request.current_working_directory,
            agent_definition_id=request.agent_definition_id,
            llm_provider_id=request.llm_provider_id,
            llm_model_binding_id=request.llm_model_binding_id,
            llm_policy_group_id=request.llm_policy_group_id,
            llm_policy_snapshot_json=request.llm_policy_snapshot_json,
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

    def bind_agent_instance_conversation(
        self,
        instance_id: str,
        conversation_id: str,
    ) -> AgentInstance:
        """为 Agent 实例绑定历史会话 ID。"""
        return self._service.bind_agent_instance_conversation(
            instance_id,
            conversation_id,
        )

    def switch_agent_instance_llm_policy(
        self,
        instance_id: str,
        *,
        llm_policy_group_id: str,
        llm_policy_snapshot_json: dict,
        llm_provider_id: str,
        llm_model_binding_id: str,
    ) -> AgentInstance:
        return self._service.switch_agent_instance_llm_policy(
            instance_id,
            llm_policy_group_id=llm_policy_group_id,
            llm_policy_snapshot_json=llm_policy_snapshot_json,
            llm_provider_id=llm_provider_id,
            llm_model_binding_id=llm_model_binding_id,
        )

    def attach_scene(
        self,
        scene_id: str,
        request: AttachSceneRequest,
    ) -> AttachSceneResponse:
        """连接 scene 并自动 resume / recreate / create root agent instance。

        调用顺序：
        1. 通过 SceneGateway（按 ``request.workspace_path`` 路由）读取 scene
           ——拿 ``bound_agent_definition_id`` 与 ``bound_root_conversation_id``；
           工作目录直接取 ``request.workspace_path``，不再单独读项目。
        2. 拒绝 archived scene。
        3. 查 scene 下「活跃 root instance」，存在即返回 mode=resumed。
        4. 否则查 scene 下「最近 root instance」：
           - 存在 → 继承其 LLM 与 conversation_id 重建（mode=recreated）；
           - 不存在 → 用 request 中的 fallback LLM 首次创建（mode=created）。
        5. 必要时回写 scene.bound_root_conversation_id 以保证 conversation 上下文跨 instance 重建可复用。
        """
        if self._scene_gateway is None:
            raise RuntimeError(
                "AgentRuntimeGatewayImpl 未注入 SceneGateway，无法处理 attach_scene。"
            )

        workspace_path = request.workspace_path
        scene = self._scene_gateway.get_scene(workspace_path, scene_id)
        if scene.status == "archived":
            raise ValueError(f"Scene 已归档，不允许 attach: {scene_id}")

        stream_id = f"stream-scene-{scene_id}"
        event_stream_url = f"/api/communication/streams/{stream_id}/events"

        # 1. 活跃 root instance 直接复用
        active = self._service.get_active_root_agent_instance_by_scene(scene_id)
        if active is not None:
            task, instance = active
            return AttachSceneResponse(
                mode="resumed",
                task=task,
                instance=instance,
                communication_stream_id=stream_id,
                event_stream_url=event_stream_url,
            )

        # 2. 决定 LLM 与 conversation_id 来源
        last = self._service.get_last_root_agent_instance_by_scene(scene_id)
        if last is not None:
            _, last_instance = last
            llm_provider_id = last_instance.llm_provider_id
            llm_model_binding_id = last_instance.llm_model_binding_id
            llm_policy_group_id = last_instance.llm_policy_group_id
            llm_policy_snapshot_json = dict(last_instance.llm_policy_snapshot_json or {})
            # conversation_id 优先用 scene 已绑定的；否则用上一条 instance 的（应当一致，
            # 但 scene 字段允许在首次执行后异步回填，做一次兜底）。
            conversation_id = (
                scene.bound_root_conversation_id or last_instance.conversation_id
            )
            mode = "recreated"
        else:
            if not request.fallback_llm_provider_id or not request.fallback_llm_model_binding_id:
                raise ValueError(
                    f"Scene 下首次创建 root agent instance 必须提供 fallback LLM"
                    f" provider 与 model binding: {scene_id}"
                )
            llm_provider_id = request.fallback_llm_provider_id
            llm_model_binding_id = request.fallback_llm_model_binding_id
            llm_policy_group_id = request.fallback_llm_policy_group_id
            llm_policy_snapshot_json = dict(request.fallback_llm_policy_snapshot_json or {})
            conversation_id = scene.bound_root_conversation_id  # 一般为 None
            mode = "created"

        # 3. 创建新 task + root instance
        new_thread_id = f"thread-{uuid4()}"
        task, instance = self._service.create_task_with_root_agent_instance(
            thread_id=new_thread_id,
            current_working_directory=workspace_path,
            agent_definition_id=scene.bound_agent_definition_id,
            llm_provider_id=llm_provider_id,
            llm_model_binding_id=llm_model_binding_id,
            llm_policy_group_id=llm_policy_group_id,
            llm_policy_snapshot_json=llm_policy_snapshot_json,
            conversation_id=conversation_id,
            scene_id=scene_id,
        )

        # 4. 必要时回写 scene.bound_root_conversation_id：
        #    instance.conversation_id 在 mode=recreated 时已存在；在 mode=created 时通常为 None
        #    （由执行引擎首次运行后 bind），所以仅当 instance 已绑定且 scene 未绑定时回写。
        if instance.conversation_id and not scene.bound_root_conversation_id:
            self._scene_gateway.set_bound_root_conversation_id(
                workspace_path, scene_id, instance.conversation_id,
            )

        return AttachSceneResponse(
            mode=mode,
            task=task,
            instance=instance,
            communication_stream_id=stream_id,
            event_stream_url=event_stream_url,
        )
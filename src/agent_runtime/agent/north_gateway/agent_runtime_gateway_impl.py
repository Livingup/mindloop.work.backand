# -*- coding: utf-8 -*-
"""Agent 运行时任务与实例管理北向网关实现。"""
from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

logger = logging.getLogger(__name__)


def _safe_runtime_task_label(value: str | None, *, max_length: int = 80) -> str:
    raw = (value or "unknown").strip()
    safe = "".join(char if char.isalnum() or char in {".", "_", "-"} else "-" for char in raw)
    return (safe.strip("-._") or "unknown")[:max_length]


from agent_runtime.agent.agent_instance import AgentInstance
from agent_runtime.agent.agent_runtime_service import AgentRuntimeService
from agent_runtime.agent.agent_task import AgentParentChildRelation, AgentTask
from agent_runtime.agent.north_gateway.agent_runtime_contracts import (
    AgentRuntimeGateway,
    AttachSceneRequest,
    AttachSceneResponse,
    CreateChildAgentInstanceRequest,
    CreateTaskWithRootAgentInstanceRequest,
    DetachSceneRequest,
    DetachSceneResponse,
)
from agent_runtime.communication.north_gateway.communication_contracts import (
    CommunicationGateway,
    CreateCommunicationStreamRequest,
)
from agent_runtime.infrastructure.runtime_task_registry import get_runtime_task_registry
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
        communication_gateway: CommunicationGateway | None = None,
    ) -> None:
        # Agent 运行时领域服务。负责维护任务、实例状态和任务内父子关系树。
        self._service = service
        # Scene 北向网关。attach_scene 用其读取 bound_agent_definition_id /
        # project.workspace_path，并校验 scene 与 workspace 归属；conversation_id 按 task 维度管理。
        self._scene_gateway = scene_gateway
        self._communication_gateway = communication_gateway

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
            conversation_id=request.conversation_id,
            trace_id=request.trace_id,
            relation_id=request.relation_id,
        )

    def list_tasks(self) -> list[AgentTask]:
        """列出当前运行时内所有任务。"""
        return self._service.list_tasks()

    def list_scene_tasks(self, scene_id: str) -> list[AgentTask]:
        """列出指定 scene 下的全部任务，按 created_at 倒序。"""
        return self._service.list_tasks_by_scene_id(scene_id)

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
        """列出指定 Agent 实例的历史直接子实例。"""
        return self._service.list_child_agent_instances(parent_instance_id)

    def list_unreleased_child_agent_instances(self, parent_instance_id: str) -> list[AgentInstance]:
        """列出指定 Agent 实例当前未释放的直接子实例。"""
        return self._service.list_unreleased_child_agent_instances(parent_instance_id)

    def release_child_agent_instance(
        self,
        parent_instance_id: str,
        child_instance_id: str,
        *,
        reason: str,
    ) -> tuple[AgentTask, list[AgentInstance]]:
        """释放父 Agent 直接创建的子 Agent 及其未释放后代。"""
        return self._service.release_child_agent_instance(
            parent_instance_id,
            child_instance_id,
            reason=reason,
        )

    def release_task_child_agent_instances(
        self,
        task_id: str,
        *,
        reason: str,
    ) -> tuple[AgentTask, list[AgentInstance]]:
        """释放任务内全部未释放子 Agent。"""
        return self._service.release_task_child_agent_instances(
            task_id,
            reason=reason,
        )

    def get_parent_agent_instance(self, child_instance_id: str) -> AgentInstance | None:
        """查询指定 Agent 实例的父实例；根实例返回 None。"""
        return self._service.get_parent_agent_instance(child_instance_id)

    def start_task(self, task_id: str) -> AgentTask:
        """确认任务仍处于未完成生命周期状态。"""
        return self._service.start_task(task_id)

    def pause_task(self, task_id: str) -> AgentTask:
        """暂停不改变 Task 生命周期状态，仅保持未完成。"""
        return self._service.pause_task(task_id)

    def resume_task(self, task_id: str) -> AgentTask:
        """恢复不改变 Task 生命周期状态，仅保持未完成。"""
        return self._service.resume_task(task_id)

    def complete_task(self, task_id: str) -> AgentTask:
        """显式标记任务完成。"""
        return self._service.complete_task(task_id)

    def complete_scene_task_and_reset_root_agent_instance(
        self,
        task_id: str,
        root_instance_id: str,
    ) -> tuple[AgentTask, AgentInstance]:
        """完成 Scene 当前 task，并把长期复用的 root agent instance 恢复为待命状态。"""
        return self._service.complete_scene_task_and_reset_root_agent_instance(
            task_id,
            root_instance_id,
        )

    def reset_scene_root_agent_instance_for_next_task(
        self,
        task_id: str,
        root_instance_id: str,
    ) -> AgentInstance:
        """兼容恢复已完成 task 上的 Scene root agent instance，使其可接收下一轮任务。"""
        return self._service.reset_scene_root_agent_instance_for_next_task(
            task_id,
            root_instance_id,
        )

    def prepare_scene_root_agent_instance_for_new_task(
        self,
        root_instance_id: str,
    ) -> tuple[AgentTask, AgentInstance]:
        """为下一条用户消息创建新的 Scene task，并复用同一个 root agent instance。"""
        return self._service.prepare_scene_root_agent_instance_for_new_task(root_instance_id)

    def fail_task(self, task_id: str, failure_message: str) -> AgentTask:
        """标记任务失败。"""
        return self._service.fail_task(task_id, failure_message)

    def cancel_task(self, task_id: str) -> AgentTask:
        """取消任务。"""
        return self._service.cancel_task(task_id)

    def start_agent_instance(self, instance_id: str) -> AgentInstance:
        """将 Agent 实例执行载体标记为 running。"""
        return self._service.start_agent_instance(instance_id)

    def pause_agent_instance(self, instance_id: str) -> AgentInstance:
        """将 Agent 实例执行载体恢复为 ready。"""
        return self._service.pause_agent_instance(instance_id)

    def resume_agent_instance(self, instance_id: str) -> AgentInstance:
        """将 Agent 实例执行载体重新标记为 running。"""
        return self._service.resume_agent_instance(instance_id)

    def complete_agent_instance(self, instance_id: str) -> AgentInstance:
        """任务结束后将 Agent 实例执行载体恢复为 ready。"""
        return self._service.complete_agent_instance(instance_id)

    def fail_agent_instance(
        self,
        instance_id: str,
        failure_message: str,
    ) -> AgentInstance:
        """记录最近失败原因并将 Agent 实例执行载体恢复为 ready。"""
        return self._service.fail_agent_instance(instance_id, failure_message)

    def interrupt_agent_instance(
        self,
        instance_id: str,
        interruption_message: str,
    ) -> AgentInstance:
        """记录技术性执行中断并将 Agent 实例执行载体恢复为 ready。"""
        return self._service.interrupt_agent_instance(instance_id, interruption_message)

    def cancel_agent_instance(self, instance_id: str) -> AgentInstance:
        """取消当前实例执行并恢复为 ready。"""
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

    def switch_agent_instance_conversation(
        self,
        instance_id: str,
        conversation_id: str,
    ) -> AgentInstance:
        return self._service.switch_agent_instance_conversation(
            instance_id,
            conversation_id,
        )

    def enqueue_agent_instance_user_message(
        self,
        instance_id: str,
        *,
        user_text: str,
        attachments=(),
        client_message_id: str | None = None,
        message_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ):
        """把用户消息追加到 Agent 实例待处理队列。"""
        return self._service.enqueue_agent_instance_user_message(
            instance_id,
            user_text=user_text,
            attachments=attachments,
            client_message_id=client_message_id,
            message_id=message_id,
            metadata=metadata,
        )

    def drain_agent_instance_user_messages(
        self,
        instance_id: str,
    ):
        """按 FIFO 顺序取出并清空 Agent 实例待处理用户消息队列。"""
        return self._service.drain_agent_instance_user_messages(instance_id)

    def remove_agent_instance_user_message(
        self,
        instance_id: str,
        message_id: str,
    ):
        return self._service.remove_agent_instance_user_message(instance_id, message_id)

    def switch_agent_instance_llm_policy(
        self,
        instance_id: str,
        *,
        llm_policy_group_id: str,
        llm_provider_id: str,
        llm_model_binding_id: str,
    ) -> AgentInstance:
        return self._service.switch_agent_instance_llm_policy(
            instance_id,
            llm_policy_group_id=llm_policy_group_id,
            llm_provider_id=llm_provider_id,
            llm_model_binding_id=llm_model_binding_id,
        )

    def attach_scene(
        self,
        scene_id: str,
        request: AttachSceneRequest,
    ) -> AttachSceneResponse:
        """连接 scene 并自动恢复或首次创建 root agent instance。

        调用顺序：
        1. 通过 SceneGateway（按 ``request.workspace_path`` 路由）读取 scene
           ——拿 ``bound_agent_definition_id`` 与工作目录，工作目录直接取 ``request.workspace_path``。
        2. 拒绝 archived scene。
        3. 查 scene 下「活跃 root instance」，存在即返回 mode=resumed。
        4. 否则查 scene 下「最近 root instance」：存在则恢复同一个 root instance 的待命态并返回 mode=resumed。
        5. Scene 首次 attach 才使用 fallback LLM 创建首个 task + root instance。
        6. conversation_id 按 task 维度创建和清理，不再通过 scene 做跨 task 复用。
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

        # 1. Scene 下存在非终态 task 时，直接复用当前 root instance。
        active = self._service.get_active_root_agent_instance_by_scene(scene_id)
        if active is not None:
            task, instance = active
            self._ensure_scene_communication_stream(
                stream_id=stream_id,
                scene_id=scene_id,
                workspace_path=workspace_path,
                task=task,
                instance=instance,
            )
            return AttachSceneResponse(
                mode="resumed",
                task=task,
                instance=instance,
                communication_stream_id=stream_id,
                event_stream_url=event_stream_url,
            )

        # 2. 若 Scene 已有历史 root instance，则复用同一个 instance 身份。
        #    旧版本 end_task 可能把 root instance 置为 completed；这里只做待命态恢复，
        #    不创建新的 root agent instance。下一条用户消息到来时再创建新的 task/conversation。
        last = self._service.get_last_root_agent_instance_by_scene(scene_id)
        if last is not None:
            task, instance = last
            if task.is_terminal:
                instance = self._service.reset_scene_root_agent_instance_for_next_task(
                    task.task_id,
                    instance.instance_id,
                )
            self._ensure_scene_communication_stream(
                stream_id=stream_id,
                scene_id=scene_id,
                workspace_path=workspace_path,
                task=task,
                instance=instance,
            )
            return AttachSceneResponse(
                mode="resumed",
                task=task,
                instance=instance,
                communication_stream_id=stream_id,
                event_stream_url=event_stream_url,
            )

        # 3. Scene 首次 attach：创建第一条 task + root instance。
        if not request.fallback_llm_provider_id or not request.fallback_llm_model_binding_id:
            raise ValueError(
                f"Scene 下首次创建 root agent instance 必须提供 fallback LLM"
                f" provider 与 model binding: {scene_id}"
            )
        llm_provider_id = request.fallback_llm_provider_id
        llm_model_binding_id = request.fallback_llm_model_binding_id
        llm_policy_group_id = request.fallback_llm_policy_group_id
        mode = "created"

        # 4. 创建首个 task + root instance；conversation_id 留空，由第一次用户消息创建独立 conversation。
        new_thread_id = f"thread-{uuid4()}"
        task, instance = self._service.create_task_with_root_agent_instance(
            thread_id=new_thread_id,
            current_working_directory=workspace_path,
            agent_definition_id=scene.bound_agent_definition_id,
            llm_provider_id=llm_provider_id,
            llm_model_binding_id=llm_model_binding_id,
            llm_policy_group_id=llm_policy_group_id,
            conversation_id=None,
            scene_id=scene_id,
        )

        # 5. Scene 通信流按 scene 维度复用；conversation_id 按 task 维度由执行引擎创建，
        #    不再回写到 scene 形成跨 task 的对话上下文复用。
        self._ensure_scene_communication_stream(
            stream_id=stream_id,
            scene_id=scene_id,
            workspace_path=workspace_path,
            task=task,
            instance=instance,
        )

        return AttachSceneResponse(
            mode=mode,
            task=task,
            instance=instance,
            communication_stream_id=stream_id,
            event_stream_url=event_stream_url,
        )

    def _ensure_scene_communication_stream(
        self,
        *,
        stream_id: str,
        scene_id: str,
        workspace_path: str,
        task: AgentTask,
        instance: AgentInstance,
    ) -> None:
        if self._communication_gateway is None:
            return
        try:
            self._communication_gateway.get_stream(stream_id)
            return
        except KeyError:
            pass
        try:
            self._communication_gateway.create_stream(
                CreateCommunicationStreamRequest(
                    stream_id=stream_id,
                    topic="agent.scene",
                    owner_id=instance.agent_definition_id,
                    metadata={
                        "scene_id": scene_id,
                        "workspace_path": workspace_path,
                        "task_id": task.task_id,
                        "thread_id": instance.thread_id,
                        "agent_instance_id": instance.instance_id,
                        "agent_definition_id": instance.agent_definition_id,
                        "trace_id": instance.trace_id,
                    },
                ),
            )
        except ValueError:
            return

    def detach_scene(
        self,
        scene_id: str,
        request: DetachSceneRequest,
    ) -> DetachSceneResponse:
        """断开 scene，将其下未结束 task 标记为失败结束，并让关联 instance 回到 ready。

        遍历 scene 下全部 task（按 created_at DESC），对每个未结束 task：
        - 取消该 task 下全部 instance 的当前执行载体；
        - 将 task 本身标记为失败结束。
        已结束的 task 不受影响（幂等）。
        """
        tasks = self._service.list_tasks_by_scene_id(scene_id)
        cancelled_task_count = 0
        cancelled_instance_count = 0

        for task in tasks:
            if task.is_terminal:
                continue
            for instance_id in task.agent_instance_ids:
                try:
                    self._service.get_agent_instance(instance_id)
                except KeyError:
                    continue
                self._service.cancel_agent_instance(instance_id)
                cancelled_instance_count += 1
            _delete_task_terminals(task.task_id)
            self._service.cancel_task(task.task_id)
            cancelled_task_count += 1

        logger.info(
            "detach_scene 完成: scene_id=%s, workspace=%s, "
            "cancelled_tasks=%d, cancelled_instances=%d",
            scene_id,
            request.workspace_path,
            cancelled_task_count,
            cancelled_instance_count,
        )
        return DetachSceneResponse(
            cancelled_task_count=cancelled_task_count,
            cancelled_instance_count=cancelled_instance_count,
        )


def _delete_task_terminals(task_id: str) -> None:
    async def delete() -> None:
        try:
            from agent_runtime.terminal.terminal_factory import TerminalFactory

            deleted = await TerminalFactory.create().delete_task_terminals(task_id)
            get_runtime_task_registry().mark_progress(
                asyncio.current_task() or task_id,
                diagnostic_context={
                    "phase": "scene_detach_terminal_cleanup.completed",
                    "task_id": task_id,
                    "agent_task_id": task_id,
                    "target_task_id": task_id,
                    "reason": "scene_detach",
                    "deleted_terminal_count": len(deleted),
                },
            )
            if deleted:
                logger.info("Scene detach 清理任务终端完成: task_id=%s terminal_count=%s", task_id, len(deleted))
        except Exception as exc:
            get_runtime_task_registry().mark_progress(
                asyncio.current_task() or task_id,
                diagnostic_context={
                    "phase": "scene_detach_terminal_cleanup.failed",
                    "task_id": task_id,
                    "agent_task_id": task_id,
                    "target_task_id": task_id,
                    "reason": "scene_detach",
                    "error_type": exc.__class__.__name__,
                },
            )
            logger.exception("Scene detach 清理任务终端失败: task_id=%s", task_id)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(delete())
    else:
        get_runtime_task_registry().create_task(
            delete(),
            task_name=f"scene-detach-terminal-cleanup-{_safe_runtime_task_label(task_id)}",
            owner_type="agent_runtime_gateway",
            owner_id=task_id,
            task_class="scene_detach_terminal_cleanup",
            diagnostic_context={
                "phase": "scene_detach_terminal_cleanup.scheduled",
                "task_id": task_id,
                "agent_task_id": task_id,
                "target_task_id": task_id,
                "reason": "scene_detach",
            },
        )

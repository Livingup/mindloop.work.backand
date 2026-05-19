# -*- coding: utf-8 -*-
"""Agent 运行时任务与实例管理北向网关契约。

该模块定义程序内部动态运行态管理能力。后续 Agent 执行引擎应通过该契约
创建任务、创建 Agent 实例、维护父子关系树和更新状态，而不是直接操作仓储。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from agent_runtime.agent.agent_instance import (
    AgentInstance,
    PendingAgentUserMessage,
    PendingUserMessageAttachment,
)
from agent_runtime.agent.agent_task import AgentParentChildRelation, AgentTask


@dataclass(frozen=True, slots=True)
class CreateTaskWithRootAgentInstanceRequest:
    """创建任务和根 Agent 实例请求。

    一个 task 必须且只能有一个根 Agent 实例。该请求用于一次性创建任务入口，
    避免出现“任务已存在但没有根 Agent 实例”的中间状态。
    """

    # 所属线程 ID。线程用于区分同一任务内不同对话执行上下文。
    thread_id: str

    current_working_directory: str

    # 静态 Agent 定义 ID，对应 agent_definition 模块的 AgentDefinition.agent_id。
    agent_definition_id: str

    # 本根实例执行时使用的 LLM 服务商 ID。
    llm_provider_id: str

    # 本根实例执行时使用的服务商模型接入项 ID。
    llm_model_binding_id: str

    llm_policy_group_id: str | None = None

    llm_policy_snapshot_json: dict[str, Any] | None = None

    # 可选任务 ID。不传时由领域服务生成。
    task_id: str | None = None

    # 可选根实例 ID。不传时由领域服务生成。
    root_instance_id: str | None = None

    # 可选历史会话 ID。完整 Conversation 对象由 conversation_message 仓储维护。
    conversation_id: str | None = None

    # 可选执行日志 trace ID。不传时由领域服务生成。
    trace_id: str | None = None

    # 可选场景 ID（scene 模块聚合根）。同一 scene 下多 task 共享同一通信流。
    # 为 None 表示任务未挂到任何场景，执行引擎将沿用旧的 stream-agent-task-{task_id} 通信流。
    scene_id: str | None = None


@dataclass(frozen=True, slots=True)
class AttachSceneRequest:
    """连接（attach）到 scene 的请求。

    attach 是 scene 维度的统一入口：
    - 若 scene 下已有可复用 root agent instance → 返回 mode="resumed"，复用同一主 Agent；
    - 若 scene 下只有旧版本终态 root instance → 重置为待命态后返回 mode="resumed"；
    - 否则首次创建 → 使用本请求中的 fallback LLM 信息创建首个 root instance，返回 mode="created"。

    Fallback LLM 字段仅在「mode=created」时实际生效；其余路径忽略本请求中的 LLM 字段，
    以保证 RuntimePolicySwitcher 在运行中调整过的 LLM 策略不会被前端 attach 调用回滚。
    LLM 字段应在 REST 层解析为已绑定 provider/model 后传入；不在网关层做 policy_group 解析。
    """

    # 项目目录绝对路径。服务端以此读取 ``<workspace>/.spread-ai/project.json`` manifest
    # 获取项目身份并与全局 scene 数据库交叉校验；同时作为新建任务的 ``current_working_directory``。
    workspace_path: str

    # Fallback：本根实例执行时使用的 LLM 服务商 ID。仅 mode=created 时使用。
    fallback_llm_provider_id: str | None = None

    # Fallback：本根实例执行时使用的服务商模型接入项 ID。仅 mode=created 时使用。
    fallback_llm_model_binding_id: str | None = None

    # Fallback：策略组 ID。仅 mode=created 时使用。
    fallback_llm_policy_group_id: str | None = None

    # Fallback：策略组解析快照（不含密钥）。仅 mode=created 时使用。
    fallback_llm_policy_snapshot_json: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AttachSceneResponse:
    """attach scene 的响应。

    mode 取值：
    - "resumed"：复用已有 Scene root agent instance，可能已从历史终态恢复为待命态；
    - "created"：scene 下首次创建 root instance；
    - "recreated"：历史兼容值，旧设计中表示终态后重建实例，新设计默认不再主动使用。
    """

    # attach 结果：resumed / recreated / created
    mode: str

    # 当前 scene 持有的 root agent instance 任务（可能是新创建的）。
    task: AgentTask

    # 当前 scene 持有的 root agent instance（可能是新创建的）。
    instance: AgentInstance

    # 通信流 ID：固定 stream-scene-{scene_id}。
    communication_stream_id: str

    # SSE 事件流地址：/api/communication/streams/{stream_id}/events。
    event_stream_url: str


@dataclass(frozen=True, slots=True)
class DetachSceneRequest:
    """断开（detach）scene 的请求。

    由 SceneSessionRegistry 过期回调或前端显式关闭时触发。
    detach 将 scene 下所有非终态 task 标记为 failed 结束，并把相关 instance 恢复为 ready。
    """

    # 项目目录绝对路径。用于路由 SceneGateway。
    workspace_path: str


@dataclass(frozen=True, slots=True)
class DetachSceneResponse:
    """detach scene 的响应。"""

    # 被归入失败结束的 task 数量。
    cancelled_task_count: int

    # 被恢复为 ready 的 agent instance 数量。
    cancelled_instance_count: int


@dataclass(frozen=True, slots=True)
class CreateChildAgentInstanceRequest:
    """创建子 Agent 实例请求。

    子实例必须挂到同一 task 内已有父实例下，形成任务内运行时 Agent 父子关系树。
    """

    # 所属任务 ID。
    task_id: str

    # 父 Agent 实例 ID。父实例必须属于同一任务。
    parent_instance_id: str

    # 子实例所属线程 ID。
    thread_id: str

    current_working_directory: str

    # 子实例引用的静态 Agent 定义 ID。
    agent_definition_id: str

    # 子实例执行时使用的 LLM 服务商 ID。
    llm_provider_id: str

    # 子实例执行时使用的服务商模型接入项 ID。
    llm_model_binding_id: str

    llm_policy_group_id: str | None = None

    llm_policy_snapshot_json: dict[str, Any] | None = None

    # 可选子实例 ID。不传时由领域服务生成。
    child_instance_id: str | None = None

    # 可选历史会话 ID。完整 Conversation 对象由 conversation_message 仓储维护。
    conversation_id: str | None = None

    # 可选执行日志 trace ID。不传时由领域服务生成。
    trace_id: str | None = None

    # 可选父子关系 ID。不传时由领域服务生成。
    relation_id: str | None = None


class AgentRuntimeGateway(Protocol):
    """Agent 运行时任务与实例管理北向网关。"""

    def create_task_with_root_agent_instance(
        self,
        request: CreateTaskWithRootAgentInstanceRequest,
    ) -> tuple[AgentTask, AgentInstance]:
        """创建任务并同时创建唯一根 Agent 实例。"""

    def create_child_agent_instance(
        self,
        request: CreateChildAgentInstanceRequest,
    ) -> tuple[AgentTask, AgentInstance, AgentParentChildRelation]:
        """在任务父子关系树中创建一个子 Agent 实例。"""

    def list_tasks(self) -> list[AgentTask]:
        """列出当前运行时内所有任务。"""

    def list_scene_tasks(self, scene_id: str) -> list[AgentTask]:
        """列出指定 scene 下的全部任务，按 created_at 倒序。"""

    def get_task(self, task_id: str) -> AgentTask:
        """按 task_id 获取任务。"""

    def get_agent_instance(self, instance_id: str) -> AgentInstance:
        """按 instance_id 获取 Agent 实例。"""

    def list_task_agent_instances(self, task_id: str) -> list[AgentInstance]:
        """列出任务内全部 Agent 实例。"""

    def list_thread_agent_instances(self, thread_id: str) -> list[AgentInstance]:
        """列出线程下全部 Agent 实例。"""

    def list_task_parent_child_relations(
        self,
        task_id: str,
    ) -> tuple[AgentParentChildRelation, ...]:
        """列出任务内 Agent 父子关系树边集合。"""

    def list_child_agent_instances(self, parent_instance_id: str) -> list[AgentInstance]:
        """列出指定 Agent 实例的直接子实例。"""

    def get_parent_agent_instance(self, child_instance_id: str) -> AgentInstance | None:
        """查询指定 Agent 实例的父实例；根实例返回 None。"""

    def start_task(self, task_id: str) -> AgentTask:
        """确认任务仍处于未完成生命周期状态。"""

    def pause_task(self, task_id: str) -> AgentTask:
        """暂停不改变 Task 生命周期状态，仅保持未完成。"""


    def attach_scene(
        self,
        scene_id: str,
        request: AttachSceneRequest,
    ) -> AttachSceneResponse:
        """连接（attach）到 scene，自动恢复或首次创建 root agent instance。

        本方法是前端进入 scene 后的唯一入口，编排了：
        - SceneGateway：读取 bound_agent_definition_id 与 project.workspace_path；
        - AgentRuntimeRepository：按 scene 查活跃和最近 root instance；
        - 历史 root instance 存在时恢复同一个 instance 的待命态，不重建主 Agent；
        - conversation_id 按 task 维度创建和清理，不再通过 scene 跨 task 复用。

        Raises:
            KeyError: scene 不存在。
            ValueError: scene 已 archived 或首次创建缺少 fallback LLM 信息。
            RuntimeError: 网关装配未注入 SceneGateway。
        """
        ...

    def detach_scene(
        self,
        scene_id: str,
        request: "DetachSceneRequest",
    ) -> "DetachSceneResponse":
        """断开（detach）scene，将其下未结束 task 标记为失败结束并让 instance 回到 ready。

        条件：scene 下没有正在执行的 agent（由调用方保证或本方法兜底跳过 running instance）。
        典型触发时机：SceneSessionRegistry 过期回调、前端显式关闭。

        本方法是幂等的：已结束的 task 不受影响。
        """
        ...

    def resume_task(self, task_id: str) -> AgentTask:
        """恢复不改变 Task 生命周期状态，仅保持未完成。"""

    def complete_task(self, task_id: str) -> AgentTask:
        """显式标记任务完成。"""

    def complete_scene_task_and_reset_root_agent_instance(
        self,
        task_id: str,
        root_instance_id: str,
    ) -> tuple[AgentTask, AgentInstance]:
        """完成 Scene 当前 task，并把长期复用的 root agent instance 恢复为可接收新 task 的待命状态。"""

    def reset_scene_root_agent_instance_for_next_task(
        self,
        task_id: str,
        root_instance_id: str,
    ) -> AgentInstance:
        """兼容恢复已终态 task 上的 Scene root agent instance，清空 conversation 并回到待命状态。"""

    def prepare_scene_root_agent_instance_for_new_task(
        self,
        root_instance_id: str,
    ) -> tuple[AgentTask, AgentInstance]:
        """当 Scene root agent instance 当前 task 已完成时，创建新 task 并复用同一个 root instance。"""

    def fail_task(self, task_id: str, failure_message: str) -> AgentTask:
        """标记任务失败。"""

    def cancel_task(self, task_id: str) -> AgentTask:
        """取消任务。"""

    def start_agent_instance(self, instance_id: str) -> AgentInstance:
        """将 Agent 实例执行载体标记为 running。"""

    def pause_agent_instance(self, instance_id: str) -> AgentInstance:
        """将 Agent 实例执行载体恢复为 ready。"""

    def resume_agent_instance(self, instance_id: str) -> AgentInstance:
        """将 Agent 实例执行载体重新标记为 running。"""

    def complete_agent_instance(self, instance_id: str) -> AgentInstance:
        """任务结束后将 Agent 实例执行载体恢复为 ready。"""

    def fail_agent_instance(
        self,
        instance_id: str,
        failure_message: str,
    ) -> AgentInstance:
        """记录最近失败原因并将 Agent 实例执行载体恢复为 ready。"""

    def interrupt_agent_instance(
        self,
        instance_id: str,
        interruption_message: str,
    ) -> AgentInstance:
        """记录技术性执行中断并将 Agent 实例执行载体恢复为 ready，不改变 Task 生命周期。"""

    def cancel_agent_instance(self, instance_id: str) -> AgentInstance:
        """取消 Agent 实例。"""
        ...

    def bind_agent_instance_conversation(
        self,
        instance_id: str,
        conversation_id: str,
    ) -> AgentInstance:
        """为 Agent 实例绑定历史会话 ID。"""
        ...

    def enqueue_agent_instance_user_message(
        self,
        instance_id: str,
        *,
        user_text: str,
        attachments: tuple[PendingUserMessageAttachment, ...] = (),
        client_message_id: str | None = None,
        message_id: str | None = None,
    ) -> tuple[AgentInstance, PendingAgentUserMessage]:
        """把用户消息追加到 Agent 实例待处理队列。"""
        ...

    def drain_agent_instance_user_messages(
        self,
        instance_id: str,
    ) -> tuple[AgentInstance, tuple[PendingAgentUserMessage, ...]]:
        """按 FIFO 顺序取出并清空 Agent 实例待处理用户消息队列。"""
        ...

    def switch_agent_instance_llm_policy(
        self,
        instance_id: str,
        *,
        llm_policy_group_id: str,
        llm_policy_snapshot_json: dict[str, Any],
        llm_provider_id: str,
        llm_model_binding_id: str,
    ) -> AgentInstance:
        ...
# -*- coding: utf-8 -*-
"""Agent 运行时任务与实例管理北向网关契约。

该模块定义程序内部动态运行态管理能力。后续 Agent 执行引擎应通过该契约
创建任务、创建 Agent 实例、维护父子关系树和更新状态，而不是直接操作仓储。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from agent_runtime.agent.agent_instance import AgentInstance
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
    - 若 scene 下当前有活跃 root agent instance → 返回 mode="resumed"，复用 instance；
    - 否则若 scene 下有上一条 root agent instance → 继承其 LLM 策略与 conversation_id 重建，返回 mode="recreated"；
    - 否则首次创建 → 使用本请求中的 fallback LLM 信息创建首个 root instance，返回 mode="created"。

    Fallback LLM 字段仅在「mode=created」时实际生效；其余路径忽略本请求中的 LLM 字段，
    以保证 RuntimePolicySwitcher 在运行中调整过的 LLM 策略不会被前端 attach 调用回滚。
    LLM 字段应在 REST 层解析为已绑定 provider/model 后传入；不在网关层做 policy_group 解析。
    """

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
    - "resumed"：复用已有活跃 root instance，未创建新实例；
    - "recreated"：旧实例已终态，按 scene 绑定 + 上一条 instance 的 LLM 重建新实例；
    - "created"：scene 下首次创建 root instance。
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
        """将任务状态流转为 running。"""

    def pause_task(self, task_id: str) -> AgentTask:
        """将任务状态流转为 paused。"""


    def attach_scene(
        self,
        scene_id: str,
        request: AttachSceneRequest,
    ) -> AttachSceneResponse:
        """连接（attach）到 scene，自动恢复 / 重建 / 首次创建 root agent instance。

        本方法是前端进入 scene 后的唯一入口，编排了：
        - SceneGateway：读取 bound_agent_definition_id、bound_root_conversation_id、project.workspace_path；
        - AgentRuntimeRepository：按 scene 查活跃和最近 root instance；
        - 必要时回写 scene.bound_root_conversation_id。

        Raises:
            KeyError: scene 不存在。
            ValueError: scene 已 archived 或首次创建缺少 fallback LLM 信息。
            RuntimeError: 网关装配未注入 SceneGateway。
        """
        ...
    def resume_task(self, task_id: str) -> AgentTask:
        """将任务状态从 paused 恢复为 running。"""

    def complete_task(self, task_id: str) -> AgentTask:
        """显式标记任务完成。"""

    def fail_task(self, task_id: str, failure_message: str) -> AgentTask:
        """标记任务失败。"""

    def cancel_task(self, task_id: str) -> AgentTask:
        """取消任务。"""

    def start_agent_instance(self, instance_id: str) -> AgentInstance:
        """将 Agent 实例状态流转为 running。"""

    def pause_agent_instance(self, instance_id: str) -> AgentInstance:
        """将 Agent 实例状态流转为 paused。"""

    def resume_agent_instance(self, instance_id: str) -> AgentInstance:
        """将暂停 Agent 实例恢复为 running。"""

    def complete_agent_instance(self, instance_id: str) -> AgentInstance:
        """显式标记 Agent 实例完成。"""

    def fail_agent_instance(
        self,
        instance_id: str,
        failure_message: str,
    ) -> AgentInstance:
        """标记 Agent 实例失败。"""

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
# -*- coding: utf-8 -*-
"""Agent 运行时实例领域模型。

该文件只描述一次 Agent 运行实例的运行态事实，不包含真正的执行循环。
Agent 静态模板数据由 agent_definition 模块维护，本文件中的 AgentInstance
通过 agent_definition_id 引用静态定义。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Final, TypeAlias


AgentInstanceStatus: TypeAlias = str

# Agent 实例已创建但尚未进入执行循环。
AGENT_INSTANCE_STATUS_CREATED: Final[str] = "created"

# Agent 实例正在执行。一个 task 内允许多个实例同时处于 running。
AGENT_INSTANCE_STATUS_RUNNING: Final[str] = "running"

# Agent 实例被执行编排层暂停，后续可恢复到 running。
AGENT_INSTANCE_STATUS_PAUSED: Final[str] = "paused"

# Agent 实例正常完成。
AGENT_INSTANCE_STATUS_COMPLETED: Final[str] = "completed"

# Agent 实例执行失败，failure_message 保存失败原因摘要。
AGENT_INSTANCE_STATUS_FAILED: Final[str] = "failed"

# Agent 实例被取消，不再继续执行。
AGENT_INSTANCE_STATUS_CANCELLED: Final[str] = "cancelled"

AGENT_INSTANCE_STATUSES: Final[frozenset[str]] = frozenset({
    AGENT_INSTANCE_STATUS_CREATED,
    AGENT_INSTANCE_STATUS_RUNNING,
    AGENT_INSTANCE_STATUS_PAUSED,
    AGENT_INSTANCE_STATUS_COMPLETED,
    AGENT_INSTANCE_STATUS_FAILED,
    AGENT_INSTANCE_STATUS_CANCELLED,
})

_TERMINAL_AGENT_INSTANCE_STATUSES: Final[frozenset[str]] = frozenset({
    AGENT_INSTANCE_STATUS_COMPLETED,
    AGENT_INSTANCE_STATUS_FAILED,
    AGENT_INSTANCE_STATUS_CANCELLED,
})

_AGENT_INSTANCE_STATUS_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    AGENT_INSTANCE_STATUS_CREATED: frozenset({
        AGENT_INSTANCE_STATUS_RUNNING,
        AGENT_INSTANCE_STATUS_FAILED,
        AGENT_INSTANCE_STATUS_CANCELLED,
    }),
    AGENT_INSTANCE_STATUS_RUNNING: frozenset({
        AGENT_INSTANCE_STATUS_PAUSED,
        AGENT_INSTANCE_STATUS_COMPLETED,
        AGENT_INSTANCE_STATUS_FAILED,
        AGENT_INSTANCE_STATUS_CANCELLED,
    }),
    AGENT_INSTANCE_STATUS_PAUSED: frozenset({
        AGENT_INSTANCE_STATUS_RUNNING,
        AGENT_INSTANCE_STATUS_FAILED,
        AGENT_INSTANCE_STATUS_CANCELLED,
    }),
    AGENT_INSTANCE_STATUS_COMPLETED: frozenset(),
    AGENT_INSTANCE_STATUS_FAILED: frozenset(),
    AGENT_INSTANCE_STATUS_CANCELLED: frozenset(),
}


def _utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(timezone.utc)


def _require_non_blank(value: str, field_name: str) -> None:
    """校验运行态标识字段不能为空。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空。")
    if value != value.strip():
        raise ValueError(f"{field_name} 不能包含首尾空白字符。")


@dataclass(frozen=True, slots=True)
class AgentInstance:
    """Agent 运行时实例。

    一个 AgentInstance 表示某个 task 内一次真实创建出来的 Agent 执行单元。
    它引用静态 Agent 定义、LLM 服务商模型接入项和历史会话对象，
    并独立维护自身状态。一个 task 内可以有任意多个 AgentInstance 并发执行。
    """

    # Agent 运行时实例唯一标识。每次创建实例都应生成新的 instance_id。
    instance_id: str

    # 所属任务 ID。一个任务内可以同时存在多个 Agent 实例。
    task_id: str

    # 所属线程 ID。线程用于区分同一任务内不同对话执行上下文。
    thread_id: str

    # 静态 Agent 定义 ID，对应 agent_definition 模块中的 AgentDefinition.agent_id。
    agent_definition_id: str

    # 本实例执行时选择的 LLM 服务商 ID。
    llm_provider_id: str

    # 本实例执行时选择的服务商模型接入项 ID。
    llm_model_binding_id: str

    # 历史会话 ID。完整 Conversation 对象由 conversation_message 仓储维护，本实例只保存引用。
    conversation_id: str | None

    # 执行日志 trace 标识。日志系统可用该字段把实例运行过程与日志链路关联起来。
    trace_id: str

    # 当前实例状态。状态流转由领域服务显式触发。
    status: AgentInstanceStatus = AGENT_INSTANCE_STATUS_CREATED

    # 实例创建时间。
    created_at: datetime = None  # type: ignore[assignment]

    # 最近一次状态或元数据更新时间。
    updated_at: datetime = None  # type: ignore[assignment]

    # 首次进入 running 的时间。
    started_at: datetime | None = None

    # 最近一次进入 paused 的时间。
    paused_at: datetime | None = None

    # 正常完成或取消的时间。
    finished_at: datetime | None = None

    # 进入 failed 的时间。
    failed_at: datetime | None = None

    # 失败原因摘要。仅 failed 状态要求有值。
    failure_message: str | None = None

    def __post_init__(self) -> None:
        """校验 Agent 实例运行态事实的完整性。"""
        _require_non_blank(self.instance_id, "instance_id")
        _require_non_blank(self.task_id, "task_id")
        _require_non_blank(self.thread_id, "thread_id")
        _require_non_blank(self.agent_definition_id, "agent_definition_id")
        _require_non_blank(self.llm_provider_id, "llm_provider_id")
        _require_non_blank(self.llm_model_binding_id, "llm_model_binding_id")
        _require_non_blank(self.trace_id, "trace_id")
        if self.conversation_id is not None:
            _require_non_blank(self.conversation_id, "conversation_id")
        if self.status not in AGENT_INSTANCE_STATUSES:
            raise ValueError(f"未知 Agent 实例状态: {self.status}")
        if self.status == AGENT_INSTANCE_STATUS_FAILED:
            _require_non_blank(self.failure_message or "", "failure_message")
        if self.failure_message is not None and not self.failure_message.strip():
            raise ValueError("failure_message 不能是空白字符串。")
        if self.created_at is None:
            object.__setattr__(self, "created_at", _utc_now())
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", self.created_at)

    @property
    def is_terminal(self) -> bool:
        """实例是否已经进入终态。"""
        return self.status in _TERMINAL_AGENT_INSTANCE_STATUSES

    def bind_conversation(
        self,
        conversation_id: str,
        *,
        at: datetime | None = None,
    ) -> "AgentInstance":
        """绑定历史会话 ID。

        Agent 实例创建时可能还没有 Conversation；执行循环创建会话后调用该方法写回引用。
        如果已经绑定同一个 conversation_id，则保持幂等；如果已绑定不同会话，则拒绝覆盖，
        避免一个运行实例的历史对话引用被意外切换。
        """
        _require_non_blank(conversation_id, "conversation_id")
        if self.conversation_id == conversation_id:
            return self
        if self.conversation_id is not None:
            raise ValueError("Agent 实例已经绑定其他 conversation_id，不能覆盖。")
        return replace(self, conversation_id=conversation_id, updated_at=at or _utc_now())

    def transition_to(
        self,
        target_status: AgentInstanceStatus,
        *,
        failure_message: str | None = None,
        at: datetime | None = None,
    ) -> AgentInstance:
        """按领域允许的状态机流转到目标状态。"""
        if target_status not in AGENT_INSTANCE_STATUSES:
            raise ValueError(f"未知 Agent 实例状态: {target_status}")
        if target_status == self.status:
            return self
        allowed_targets = _AGENT_INSTANCE_STATUS_TRANSITIONS[self.status]
        if target_status not in allowed_targets:
            raise ValueError(
                f"Agent 实例状态不能从 {self.status} 流转到 {target_status}。"
            )
        now = at or _utc_now()
        values: dict[str, object] = {
            "status": target_status,
            "updated_at": now,
        }
        if target_status == AGENT_INSTANCE_STATUS_RUNNING:
            values["started_at"] = self.started_at or now
        elif target_status == AGENT_INSTANCE_STATUS_PAUSED:
            values["paused_at"] = now
        elif target_status == AGENT_INSTANCE_STATUS_COMPLETED:
            values["finished_at"] = now
            values["failure_message"] = None
        elif target_status == AGENT_INSTANCE_STATUS_FAILED:
            _require_non_blank(failure_message or "", "failure_message")
            values["failed_at"] = now
            values["failure_message"] = failure_message
        elif target_status == AGENT_INSTANCE_STATUS_CANCELLED:
            values["finished_at"] = now
        return replace(self, **values)

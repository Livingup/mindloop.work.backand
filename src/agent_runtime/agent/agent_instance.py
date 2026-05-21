# -*- coding: utf-8 -*-
"""Agent 运行时实例领域模型。

该文件只描述一次 Agent 运行实例的运行态事实，不包含真正的执行循环。
Agent 静态模板数据由 agent_definition 模块维护，本文件中的 AgentInstance
通过 agent_definition_id 引用静态定义。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Final, TypeAlias

from agent_runtime.input_modality import INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF

AgentInstanceStatus: TypeAlias = str

# Agent 实例当前没有执行循环在跑，可以接收用户消息并按需启动执行。
AGENT_INSTANCE_STATUS_READY: Final[str] = "ready"

# Agent 实例当前正在执行循环。此时仍接收用户消息，但只进入待处理消息队列。
AGENT_INSTANCE_STATUS_RUNNING: Final[str] = "running"

AGENT_INSTANCE_STATUSES: Final[frozenset[str]] = frozenset({
    AGENT_INSTANCE_STATUS_READY,
    AGENT_INSTANCE_STATUS_RUNNING,
})

_LEGACY_READY_INSTANCE_STATUSES: Final[frozenset[str]] = frozenset({
    "created",
    "paused",
    "completed",
    "failed",
    "cancelled",
})


def _normalize_instance_status(status: str) -> str:
    """把历史实例生命周期态归一化为当前执行载体两态。"""
    if status in AGENT_INSTANCE_STATUSES:
        return status
    if status in _LEGACY_READY_INSTANCE_STATUSES:
        return AGENT_INSTANCE_STATUS_READY
    raise ValueError(f"未知 Agent 实例状态: {status}")


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
class PendingUserMessageAttachment:
    kind: str
    resource_id: str
    name: str | None = None
    mime_type: str | None = None
    size_in_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    page_count: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in {INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF}:
            raise ValueError("attachment.kind 必须是 image 或 pdf。")
        _require_non_blank(self.resource_id, "attachment.resource_id")
        if self.name is not None and not self.name.strip():
            raise ValueError("attachment.name 不能是空白字符串。")
        if self.mime_type is not None and not self.mime_type.strip():
            raise ValueError("attachment.mime_type 不能是空白字符串。")
        for field_name in ("size_in_bytes", "width", "height", "page_count"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"attachment.{field_name} 不能小于 0。")


@dataclass(frozen=True, slots=True)
class PendingAgentUserMessage:
    """Agent 实例待处理用户消息。

    用户发给 AgentInstance 的消息统一先进入该队列；执行循环只在完整轮次边界消费，
    避免用户输入插入 LLM 请求或工具执行的中途。
    """

    # 队列消息唯一标识，用于前端提交幂等追踪与运行时事件关联。
    message_id: str

    # 用户输入文本。当前 REST 消息入口只支持文本输入。
    user_text: str

    # 前端可选消息 ID，便于 UI 对齐本地乐观消息与后端入队事实。
    client_message_id: str | None = None

    attachments: tuple[PendingUserMessageAttachment, ...] = ()

    # 后端接收消息的时间，用于审计与 FIFO 排序辅助。
    received_at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        """校验待处理用户消息的业务字段。"""
        _require_non_blank(self.message_id, "message_id")
        if not isinstance(self.user_text, str):
            raise ValueError("user_text 必须是字符串。")
        if self.user_text and self.user_text != self.user_text.strip():
            raise ValueError("user_text 不能包含首尾空白字符。")
        if not self.user_text.strip() and not self.attachments:
            raise ValueError("user_text 和 attachments 必须至少提供一个。")
        if self.client_message_id is not None:
            _require_non_blank(self.client_message_id, "client_message_id")
        for attachment in self.attachments:
            if not isinstance(attachment, PendingUserMessageAttachment):
                raise ValueError("attachments 必须由 PendingUserMessageAttachment 组成。")
        object.__setattr__(self, "attachments", tuple(self.attachments))
        if self.received_at is None:
            object.__setattr__(self, "received_at", _utc_now())


@dataclass(frozen=True, slots=True)
class AgentInstance:
    """Agent 运行时实例。

    AgentInstance 表示可执行的 Agent 运行主体。它的主状态只表达执行载体是否正在运行：
    ready 表示当前可接收输入并可启动执行；running 表示执行循环正在进行。普通子实例仍然
    隶属于单个 task；Scene 绑定的 root agent instance 是长期复用的主 Agent 运行主体，
    可以在同一个 Scene 下依次承载多个 task。
    """

    # Agent 运行时实例唯一标识。每次创建实例都应生成新的 instance_id。
    instance_id: str

    # 当前绑定的任务 ID。Scene root agent instance 会长期复用；它在同一时间只绑定一个当前 task，
    # 当前 task 结束后可切换到下一轮新 task，历史 task 通过 root_agent_instance_id 保留审计关系。
    task_id: str

    # 所属线程 ID。线程用于区分同一任务内不同对话执行上下文。
    thread_id: str

    current_working_directory: str

    # 静态 Agent 定义 ID，对应 agent_definition 模块中的 AgentDefinition.agent_id。
    agent_definition_id: str

    # 本实例执行时选择的 LLM 服务商 ID。
    llm_provider_id: str

    # 本实例执行时选择的服务商模型接入项 ID。
    llm_model_binding_id: str

    # 当前 task 的历史会话 ID。Scene root agent instance 在 end_task 后必须清空该字段，
    # 下一轮新 task 会创建新的 conversation_id，避免不同用户任务的上下文互相污染。
    conversation_id: str | None

    # 执行日志 trace 标识。日志系统可用该字段把实例运行过程与日志链路关联起来。
    trace_id: str

    llm_policy_group_id: str | None = None

    llm_policy_snapshot_json: dict[str, Any] = field(default_factory=dict)

    # 当前实例执行载体状态。ready 可接收输入，running 表示执行循环正在进行。
    status: AgentInstanceStatus = AGENT_INSTANCE_STATUS_READY

    # 用户发给当前 AgentInstance 但尚未被对话消费的消息队列。
    # 队列只能由执行循环在完整轮次边界按 FIFO 顺序 drain 到 conversation。
    pending_user_messages: tuple[PendingAgentUserMessage, ...] = ()

    # 实例创建时间。
    created_at: datetime = None  # type: ignore[assignment]

    # 最近一次状态或元数据更新时间。
    updated_at: datetime = None  # type: ignore[assignment]

    # 首次进入 running 的时间。
    started_at: datetime | None = None

    # 最近一次进入 ready 的时间。
    paused_at: datetime | None = None

    # 保留字段：实例作为执行载体不再进入 completed/cancelled 主状态，历史数据和审计仍可读取该时间。
    finished_at: datetime | None = None

    # 最近一次执行失败或中断的时间，仅作审计快照，不代表实例进入 failed 主状态。
    failed_at: datetime | None = None

    # 最近一次执行失败或中断原因摘要，仅作审计快照，不代表实例进入 failed 主状态。
    failure_message: str | None = None

    def __post_init__(self) -> None:
        """校验 Agent 实例运行态事实的完整性。"""
        _require_non_blank(self.instance_id, "instance_id")
        _require_non_blank(self.task_id, "task_id")
        _require_non_blank(self.thread_id, "thread_id")
        _require_non_blank(self.current_working_directory, "current_working_directory")
        _require_non_blank(self.agent_definition_id, "agent_definition_id")
        _require_non_blank(self.llm_provider_id, "llm_provider_id")
        _require_non_blank(self.llm_model_binding_id, "llm_model_binding_id")
        if self.llm_policy_group_id is not None:
            _require_non_blank(self.llm_policy_group_id, "llm_policy_group_id")
        if not isinstance(self.llm_policy_snapshot_json, dict):
            raise ValueError("llm_policy_snapshot_json 必须是对象。")
        _require_non_blank(self.trace_id, "trace_id")
        if self.conversation_id is not None:
            _require_non_blank(self.conversation_id, "conversation_id")
        normalized_status = _normalize_instance_status(self.status)
        if normalized_status != self.status:
            object.__setattr__(self, "status", normalized_status)
        for pending_message in self.pending_user_messages:
            if not isinstance(pending_message, PendingAgentUserMessage):
                raise ValueError("pending_user_messages 必须由 PendingAgentUserMessage 组成。")
        if self.failure_message is not None and not self.failure_message.strip():
            raise ValueError("failure_message 不能是空白字符串。")
        if self.created_at is None:
            object.__setattr__(self, "created_at", _utc_now())
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", self.created_at)

    @property
    def is_terminal(self) -> bool:
        """实例是否已经进入终态。当前 AgentInstance 两态模型下实例没有终态。"""
        return False

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

    def switch_conversation(
        self,
        conversation_id: str,
        *,
        at: datetime | None = None,
    ) -> "AgentInstance":
        _require_non_blank(conversation_id, "conversation_id")
        if self.conversation_id == conversation_id:
            return self
        return replace(self, conversation_id=conversation_id, updated_at=at or _utc_now())

    def reset_for_next_scene_task(
        self,
        *,
        at: datetime | None = None,
    ) -> "AgentInstance":
        """将 Scene root agent instance 恢复为等待下一轮 task 的待命状态。

        end_task 的业务语义是完成当前 task，而不是销毁 Scene 的主 Agent。
        因此 root instance 需要保留 instance_id、Agent 定义、LLM 策略和工作目录，
        但必须清空本轮 task 的 conversation_id 与运行时间戳，避免下一轮任务复用旧会话上下文。
        """
        now = at or _utc_now()
        return replace(
            self,
            conversation_id=None,
            status=AGENT_INSTANCE_STATUS_READY,
            updated_at=now,
            started_at=None,
            paused_at=now,
            finished_at=None,
            failed_at=None,
            failure_message=None,
        )

    def assign_to_new_scene_task(
        self,
        *,
        task_id: str,
        thread_id: str,
        trace_id: str,
        at: datetime | None = None,
    ) -> "AgentInstance":
        """把长期复用的 Scene root agent instance 切换到新的当前 task。

        每个用户任务必须拥有独立 task_id、thread_id、trace_id 和 conversation_id；
        该方法只复用 root instance 的稳定身份、Agent 定义、工作目录和 LLM 策略。
        调用方应先创建以本 instance_id 为 root_agent_instance_id 的新 AgentTask。
        """
        _require_non_blank(task_id, "task_id")
        _require_non_blank(thread_id, "thread_id")
        _require_non_blank(trace_id, "trace_id")
        if self.status == AGENT_INSTANCE_STATUS_RUNNING:
            raise ValueError("Agent 实例正在执行，不能切换到新的 Scene task。")
        now = at or _utc_now()
        return replace(
            self,
            task_id=task_id,
            thread_id=thread_id,
            conversation_id=None,
            trace_id=trace_id,
            status=AGENT_INSTANCE_STATUS_READY,
            updated_at=now,
            started_at=None,
            paused_at=now,
            finished_at=None,
            failed_at=None,
            failure_message=None,
        )

    def switch_llm_policy(
        self,
        *,
        llm_policy_group_id: str,
        llm_policy_snapshot_json: dict[str, Any],
        llm_provider_id: str,
        llm_model_binding_id: str,
        at: datetime | None = None,
    ) -> "AgentInstance":
        _require_non_blank(llm_policy_group_id, "llm_policy_group_id")
        _require_non_blank(llm_provider_id, "llm_provider_id")
        _require_non_blank(llm_model_binding_id, "llm_model_binding_id")
        if not isinstance(llm_policy_snapshot_json, dict):
            raise ValueError("llm_policy_snapshot_json 必须是对象。")
        return replace(
            self,
            llm_policy_group_id=llm_policy_group_id,
            llm_policy_snapshot_json=dict(llm_policy_snapshot_json),
            llm_provider_id=llm_provider_id,
            llm_model_binding_id=llm_model_binding_id,
            updated_at=at or _utc_now(),
        )

    def enqueue_user_message(
        self,
        pending_message: PendingAgentUserMessage,
        *,
        at: datetime | None = None,
    ) -> "AgentInstance":
        """把用户发来的消息追加到待处理队列末尾。

        所有用户消息统一先进入队列；running 时不打断当前执行循环，ready 时由执行引擎
        启动执行后在循环边界消费。
        """
        if not isinstance(pending_message, PendingAgentUserMessage):
            raise ValueError("pending_message 必须是 PendingAgentUserMessage。")
        return replace(
            self,
            pending_user_messages=(*self.pending_user_messages, pending_message),
            updated_at=at or _utc_now(),
        )

    def drain_pending_user_messages(
        self,
        *,
        at: datetime | None = None,
    ) -> tuple["AgentInstance", tuple[PendingAgentUserMessage, ...]]:
        """按 FIFO 顺序取出并清空待处理用户消息队列。"""
        if not self.pending_user_messages:
            return self, ()
        drained = self.pending_user_messages
        return replace(
            self,
            pending_user_messages=(),
            updated_at=at or _utc_now(),
        ), drained

    def transition_to(
        self,
        target_status: AgentInstanceStatus,
        *,
        failure_message: str | None = None,
        at: datetime | None = None,
    ) -> "AgentInstance":
        """按 ready/running 两态切换执行载体状态。

        AgentInstance 不再表达 completed/failed/cancelled 终态；任务是否结束由 AgentTask 表达。
        执行异常或 fail_task 只会把实例恢复到 ready，并把 failure_message 作为最近错误审计。
        """
        target_status = _normalize_instance_status(target_status)
        now = at or _utc_now()
        values: dict[str, object] = {
            "status": target_status,
            "updated_at": now,
        }
        if target_status == AGENT_INSTANCE_STATUS_RUNNING:
            values["started_at"] = self.started_at or now
        else:
            values["paused_at"] = now
            if failure_message is not None:
                _require_non_blank(failure_message, "failure_message")
                values["failed_at"] = now
                values["failure_message"] = failure_message
        if target_status == self.status and failure_message is None:
            return self
        return replace(self, **values)
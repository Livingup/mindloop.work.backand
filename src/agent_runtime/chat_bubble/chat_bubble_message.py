# -*- coding: utf-8 -*-
"""前端 Chat 消息气泡领域模型。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Final, TypeAlias


ChatBubbleRole: TypeAlias = str
ChatBubbleStatus: TypeAlias = str
ChatBubbleTextFormat: TypeAlias = str

CHAT_BUBBLE_ROLE_USER: Final[str] = "user"
CHAT_BUBBLE_ROLE_AGENT: Final[str] = "agent"
CHAT_BUBBLE_ROLE_LOCAL_AGENT: Final[str] = "local_agent"
CHAT_BUBBLE_ROLE_REMOTE_AGENT: Final[str] = "remote_agent"
CHAT_BUBBLE_ROLE_SYSTEM: Final[str] = "system"
# 兼容历史已持久化的 assistant 气泡；新 UI 气泡统一使用 agent。
CHAT_BUBBLE_ROLE_ASSISTANT: Final[str] = "assistant"
CHAT_BUBBLE_ROLES: Final[frozenset[str]] = frozenset({
    CHAT_BUBBLE_ROLE_USER,
    CHAT_BUBBLE_ROLE_AGENT,
    CHAT_BUBBLE_ROLE_LOCAL_AGENT,
    CHAT_BUBBLE_ROLE_REMOTE_AGENT,
    CHAT_BUBBLE_ROLE_SYSTEM,
    CHAT_BUBBLE_ROLE_ASSISTANT,
})

CHAT_BUBBLE_STATUS_SENDING: Final[str] = "sending"
CHAT_BUBBLE_STATUS_ACCEPTED: Final[str] = "accepted"
CHAT_BUBBLE_STATUS_COMPLETED: Final[str] = "completed"
CHAT_BUBBLE_STATUS_STREAMING: Final[str] = "streaming"
CHAT_BUBBLE_STATUS_FAILED: Final[str] = "failed"
CHAT_BUBBLE_STATUSES: Final[frozenset[str]] = frozenset({
    CHAT_BUBBLE_STATUS_SENDING,
    CHAT_BUBBLE_STATUS_ACCEPTED,
    CHAT_BUBBLE_STATUS_COMPLETED,
    CHAT_BUBBLE_STATUS_STREAMING,
    CHAT_BUBBLE_STATUS_FAILED,
})

CHAT_BUBBLE_TEXT_FORMATS: Final[frozenset[str]] = frozenset({
    "plain_text",
    "markdown",
    "json",
    "html",
    "diff",
})

ChatBubbleInteractionState: TypeAlias = str

CHAT_BUBBLE_INTERACTION_STATE_NONE: Final[str] = "none"
CHAT_BUBBLE_INTERACTION_STATE_PENDING: Final[str] = "pending"
CHAT_BUBBLE_INTERACTION_STATE_ANSWERED: Final[str] = "answered"
CHAT_BUBBLE_INTERACTION_STATES: Final[frozenset[str]] = frozenset({
    CHAT_BUBBLE_INTERACTION_STATE_NONE,
    CHAT_BUBBLE_INTERACTION_STATE_PENDING,
    CHAT_BUBBLE_INTERACTION_STATE_ANSWERED,
})


def _utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(timezone.utc)


def _require_non_blank(value: str, field_name: str) -> None:
    """校验前端消息气泡的业务标识和文本字段不能为空。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空。")
    if value != value.strip():
        raise ValueError(f"{field_name} 不能包含首尾空白字符。")


@dataclass(frozen=True, slots=True)
class ChatBubbleInteractionOption:
    """前端消息气泡中的用户交互选项。"""

    # 选项唯一标识，来自 message_user request_response 的 options。
    option_id: str

    # 展示给用户的选项文案。
    label: str

    # 可选说明，用于在按钮下方展示更详细的选择含义。
    description: str | None = None

    def __post_init__(self) -> None:
        """校验交互选项字段，避免持久化不可展示的空选项。"""
        _require_non_blank(self.option_id, "option_id")
        _require_non_blank(self.label, "label")
        if self.description is not None and not self.description.strip():
            raise ValueError("description 不能是空白字符串。")

    def to_json(self) -> dict[str, Any]:
        """转换为前端 API 可直接消费的 JSON 对象。"""
        data: dict[str, Any] = {
            "id": self.option_id,
            "label": self.label,
        }
        if self.description is not None:
            data["description"] = self.description
        return data

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "ChatBubbleInteractionOption":
        """从前端请求或数据库 JSON 还原交互选项。"""
        return cls(
            option_id=str(value.get("id") or value.get("option_id") or ""),
            label=str(value.get("label") or ""),
            description=(str(value["description"]) if value.get("description") is not None else None),
        )


@dataclass(frozen=True, slots=True)
class ChatBubbleMessage:
    """面向前端 Chat UI 的消息气泡持久化模型。

    ChatBubbleMessage 不是运行时事件日志，也不是 LLM conversation 原始上下文；它只表达
    前端聊天列表最终要展示的一条气泡，以及这条气泡和后端消息提交/SSE 事件之间的关联键。
    """

    # 气泡唯一标识。前端可传入稳定 ID，后端也可生成。
    bubble_id: str

    # 所属 Scene。前端打开 Scene 时按 scene_id 查询历史气泡列表。
    scene_id: str

    # 当前展示气泡关联的 root/child AgentInstance，允许系统消息为空。
    agent_instance_id: str | None

    # 可选 task 归属，用于审计 Scene 中跨 task 的历史气泡。
    task_id: str | None

    # 可选 conversation 归属，用于关联 LLM 上下文会话。
    conversation_id: str | None

    # 气泡角色：user / assistant / system。
    role: ChatBubbleRole

    # 前端展示状态：sending / accepted / completed / streaming / failed。
    status: ChatBubbleStatus

    # 气泡正文文本。
    text: str

    # 文本格式：plain_text / markdown / json / html / diff。
    text_format: ChatBubbleTextFormat = "markdown"

    # 前端展示渠道，当前 Web Chat 默认 web。
    channel: str | None = "web"

    # 前端生成的客户端消息 ID，用于 HTTP 提交和 SSE accepted 事件对齐。
    client_message_id: str | None = None

    # 后端 conversation 或 SSE 事件中的消息 ID。
    backend_message_id: str | None = None

    # 后端 AgentInstance pending 队列中的消息 ID。
    pending_message_id: str | None = None

    # request_response 类型消息的交互选项；只表达按钮展示数据，不承担是否等待用户回馈的判断。
    interaction_options: tuple[ChatBubbleInteractionOption, ...] = ()

    # 后端明确声明的用户交互状态：none / pending / answered。
    interaction_state: ChatBubbleInteractionState = CHAT_BUBBLE_INTERACTION_STATE_NONE

    # 用户已经选择的选项 ID，用于恢复历史按钮状态。
    selected_option_id: str | None = None

    # Scene 内稳定排序号，由仓储按 scene 递增分配。
    sort_order: int = 0

    # 创建时间。
    created_at: datetime = None  # type: ignore[assignment]

    # 最近更新时间。
    updated_at: datetime = None  # type: ignore[assignment]

    # 扩展元数据，保留前端未来展示需求。
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验消息气泡业务不变量。"""
        _require_non_blank(self.bubble_id, "bubble_id")
        _require_non_blank(self.scene_id, "scene_id")
        if self.agent_instance_id is not None:
            _require_non_blank(self.agent_instance_id, "agent_instance_id")
        if self.task_id is not None:
            _require_non_blank(self.task_id, "task_id")
        if self.conversation_id is not None:
            _require_non_blank(self.conversation_id, "conversation_id")
        if self.role not in CHAT_BUBBLE_ROLES:
            raise ValueError(f"未知 Chat 气泡角色: {self.role}")
        if self.status not in CHAT_BUBBLE_STATUSES:
            raise ValueError(f"未知 Chat 气泡状态: {self.status}")
        if not isinstance(self.text, str):
            raise ValueError("text 必须是字符串。")
        if self.text_format not in CHAT_BUBBLE_TEXT_FORMATS:
            raise ValueError(f"未知 Chat 气泡文本格式: {self.text_format}")
        if self.channel is not None:
            _require_non_blank(self.channel, "channel")
        if self.client_message_id is not None:
            _require_non_blank(self.client_message_id, "client_message_id")
        if self.backend_message_id is not None:
            _require_non_blank(self.backend_message_id, "backend_message_id")
        if self.pending_message_id is not None:
            _require_non_blank(self.pending_message_id, "pending_message_id")
        for option in self.interaction_options:
            if not isinstance(option, ChatBubbleInteractionOption):
                raise ValueError("interaction_options 必须由 ChatBubbleInteractionOption 组成。")
        if self.interaction_state not in CHAT_BUBBLE_INTERACTION_STATES:
            raise ValueError(f"未知 Chat 气泡交互状态: {self.interaction_state}")
        if self.interaction_state == CHAT_BUBBLE_INTERACTION_STATE_PENDING and self.role not in {CHAT_BUBBLE_ROLE_AGENT, CHAT_BUBBLE_ROLE_ASSISTANT}:
            raise ValueError("只有 agent 气泡可以处于 pending 交互状态。")
        if self.selected_option_id is not None:
            _require_non_blank(self.selected_option_id, "selected_option_id")
            if self.interaction_state == CHAT_BUBBLE_INTERACTION_STATE_PENDING:
                raise ValueError("已选择选项的气泡不能继续处于 pending 交互状态。")
        if self.sort_order < 0:
            raise ValueError("sort_order 不能小于 0。")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata 必须是对象。")
        if self.created_at is None:
            object.__setattr__(self, "created_at", _utc_now())
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", self.created_at)

    def with_patch(
        self,
        *,
        status: ChatBubbleStatus | None = None,
        text: str | None = None,
        text_format: ChatBubbleTextFormat | None = None,
        channel: str | None = None,
        backend_message_id: str | None = None,
        pending_message_id: str | None = None,
        interaction_options: tuple[ChatBubbleInteractionOption, ...] | None = None,
        interaction_state: ChatBubbleInteractionState | None = None,
        selected_option_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> "ChatBubbleMessage":
        """按前端 PATCH 请求更新气泡展示状态。"""
        values: dict[str, Any] = {"updated_at": at or _utc_now()}
        if status is not None:
            values["status"] = status
        if text is not None:
            values["text"] = text
        if text_format is not None:
            values["text_format"] = text_format
        if channel is not None:
            values["channel"] = channel
        if backend_message_id is not None:
            values["backend_message_id"] = backend_message_id
        if pending_message_id is not None:
            values["pending_message_id"] = pending_message_id
        if interaction_options is not None:
            values["interaction_options"] = interaction_options
        if interaction_state is not None:
            values["interaction_state"] = interaction_state
        if selected_option_id is not None:
            values["selected_option_id"] = selected_option_id
        if metadata is not None:
            values["metadata"] = metadata
        return replace(self, **values)

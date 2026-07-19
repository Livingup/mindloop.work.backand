# -*- coding: utf-8 -*-
"""Communication message ownership policy.

Agent-instance runtime events share one scene transport, but each business event must
carry an explicit ``metadata.agent_instance_id`` so consumers can route it without
guessing from the stream, task, selected Agent, or actor.
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class CommunicationMessageOwnerPolicy(str, Enum):
    """Whether a communication message may omit an Agent instance owner."""

    AGENT_INSTANCE_REQUIRED = "agent_instance_required"
    UNOWNED_ALLOWED = "unowned_allowed"


_AGENT_INSTANCE_REQUIRED_EVENT_TYPES = frozenset({
    "user.message.accepted",
})

_AGENT_INSTANCE_REQUIRED_EVENT_PREFIXES = (
    "agent.",
    "user.message.",
    "llm.",
    "tool.",
    "context_compression.",
    "terminal.",
    "frontend.debug.",
)

# These families are intentionally transport/global/resource scoped. They may omit an
# Agent owner. Unknown/custom message types also remain legal because the communication
# module is a generic transport and must not reinterpret every external business event.
_UNOWNED_ALLOWED_EVENT_PREFIXES = (
    "communication.",
    "system.",
    "network.",
    "file_resource.",
    "custom.",
)


def classify_message_owner_policy(message_type: str) -> CommunicationMessageOwnerPolicy:
    """Classify the owner requirement for one communication business message type."""

    normalized = message_type.strip() if isinstance(message_type, str) else ""
    if normalized in _AGENT_INSTANCE_REQUIRED_EVENT_TYPES or normalized.startswith(
        _AGENT_INSTANCE_REQUIRED_EVENT_PREFIXES
    ):
        return CommunicationMessageOwnerPolicy.AGENT_INSTANCE_REQUIRED
    if normalized.startswith(_UNOWNED_ALLOWED_EVENT_PREFIXES):
        return CommunicationMessageOwnerPolicy.UNOWNED_ALLOWED
    return CommunicationMessageOwnerPolicy.UNOWNED_ALLOWED


def agent_instance_id_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    """Return a non-blank explicit Agent owner from message metadata."""

    if not metadata:
        return None
    value = metadata.get("agent_instance_id")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def require_message_agent_instance_id(
    message_type: str,
    metadata: dict[str, Any] | None,
) -> None:
    """Reject Agent-scoped messages that do not carry an explicit metadata owner."""

    if classify_message_owner_policy(message_type) is CommunicationMessageOwnerPolicy.UNOWNED_ALLOWED:
        return
    agent_instance_id = agent_instance_id_from_metadata(metadata)
    if agent_instance_id is None:
        raise ValueError(
            "Agent 实例级通信事件必须提供非空 metadata.agent_instance_id: "
            f"message_type={message_type}"
        )




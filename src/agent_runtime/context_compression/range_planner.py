from __future__ import annotations

from agent_runtime.context_compression.config import ContextCompressionConfig
from agent_runtime.context_compression.models import CompressionRange
from agent_runtime.conversation_message.message.models import Conversation, ROLE_ASSISTANT, ROLE_USER, ToolResultPart, ToolUsePart


def plan_compression_range(
    conversation: Conversation,
    *,
    config: ContextCompressionConfig,
    compression_index: int,
    reason: str,
) -> CompressionRange | None:
    messages = conversation.messages
    if len(messages) <= config.retained_tail_min_messages:
        return None
    retained_start = _find_retained_tail_start(conversation, config.retained_tail_min_messages)
    if retained_start <= 0:
        return None
    retained_start = _adjust_tail_start_for_tool_protocol(conversation, retained_start)
    compressed_end = retained_start - 1
    compressed_count = compressed_end + 1
    if compressed_count < config.min_compressible_messages:
        return None
    if _has_unmatched_tool_protocol(messages[:retained_start]):
        return None
    return CompressionRange(
        compressed_start_index=0,
        compressed_end_index=compressed_end,
        retained_tail_start_index=retained_start,
        retained_tail_end_index=len(messages) - 1,
        retained_tail_anchor=_anchor_for_message(conversation, retained_start),
        reason=reason,
        compression_index=compression_index,
    )


def _find_retained_tail_start(conversation: Conversation, min_tail_messages: int) -> int:
    messages = conversation.messages
    minimum_start = max(0, len(messages) - min_tail_messages)
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == ROLE_ASSISTANT:
            return min(index, minimum_start)
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == ROLE_USER:
            return min(index, minimum_start)
    return minimum_start


def _adjust_tail_start_for_tool_protocol(conversation: Conversation, retained_start: int) -> int:
    messages = conversation.messages
    for index in range(retained_start, len(messages)):
        message = messages[index]
        if any(isinstance(part, ToolResultPart) for part in message.content):
            for prior_index in range(index - 1, -1, -1):
                prior = messages[prior_index]
                if any(isinstance(part, ToolUsePart) for part in prior.content):
                    retained_start = min(retained_start, prior_index)
                    break
    return retained_start


def _has_unmatched_tool_protocol(messages: list[object]) -> bool:
    pending = 0
    for message in messages:
        content = getattr(message, "content", [])
        pending += sum(1 for part in content if isinstance(part, ToolUsePart))
        for part in content:
            if isinstance(part, ToolResultPart):
                pending -= 1
                if pending < 0:
                    return True
    return pending != 0


def _anchor_for_message(conversation: Conversation, index: int) -> str:
    message = conversation.messages[index]
    if message.role == ROLE_ASSISTANT:
        return "latest_assistant_message"
    if message.role == ROLE_USER:
        return "latest_user_message"
    return "retained_tail_min_messages"

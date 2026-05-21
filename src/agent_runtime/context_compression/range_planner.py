from __future__ import annotations

from agent_runtime.context_compression.models import CompressionRange
from agent_runtime.conversation_message.message.models import Conversation, ROLE_ASSISTANT


def plan_compression_range(
    conversation: Conversation,
    *,
    compression_index: int,
    reason: str,
) -> CompressionRange | None:
    messages = conversation.messages
    retained_start = _find_last_assistant_message_index(conversation)
    if retained_start <= 0:
        return None
    compressed_end = retained_start - 1
    return CompressionRange(
        compressed_start_index=0,
        compressed_end_index=compressed_end,
        retained_tail_start_index=retained_start,
        retained_tail_end_index=len(messages) - 1,
        retained_tail_anchor="latest_assistant_message",
        reason=reason,
        compression_index=compression_index,
    )


def _find_last_assistant_message_index(conversation: Conversation) -> int:
    messages = conversation.messages
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == ROLE_ASSISTANT:
            return index
    return 0

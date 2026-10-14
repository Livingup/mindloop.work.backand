from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

from agent_runtime.context_compression.config import ContextCompressionConfig
from agent_runtime.context_compression.models import CompressionDecision
from agent_runtime.conversation_message.message.models import (
    ROLE_ASSISTANT,
    Conversation,
    ImagePart,
    PdfPart,
    TextPart,
    ThinkingPart,
    ToolResultPart,
    ToolUsePart,
    UniMessage,
)
from agent_runtime.conversation_message.message.tool_result_output import normalize_tool_result_output
from agent_runtime.llm_model_registry.llm_model_registry_service import ResolvedLlmProviderModel


_CHARS_PER_TOKEN = 4
_MESSAGE_OVERHEAD_TOKENS = 4
_PART_OVERHEAD_TOKENS = 2
_RESOURCE_REFERENCE_TOKENS = 16


def decide_context_compression(
    conversation: Conversation,
    resolved_model: ResolvedLlmProviderModel,
    config: ContextCompressionConfig,
) -> CompressionDecision:
    if not config.enabled:
        return CompressionDecision(False, "config_disabled")
    usage = conversation.state.last_context_usage or {}
    base_used_tokens = _int_or_none(usage.get("context_window_used_tokens"))
    if base_used_tokens is None:
        return CompressionDecision(False, "missing_last_context_usage")
    threshold = _effective_threshold(resolved_model, config)
    if threshold is None:
        return CompressionDecision(
            False,
            "missing_context_window_threshold",
            used_tokens=base_used_tokens,
            base_used_tokens=base_used_tokens,
            projected_used_tokens=base_used_tokens,
        )

    incremental_tokens, incremental_message_count, recorded_message_count = _estimate_incremental_context_tokens(
        conversation,
        usage,
    )
    projected_used_tokens = base_used_tokens + incremental_tokens
    decision_fields = {
        "threshold": threshold,
        "used_tokens": projected_used_tokens,
        "base_used_tokens": base_used_tokens,
        "estimated_incremental_tokens": incremental_tokens,
        "projected_used_tokens": projected_used_tokens,
        "estimated_incremental_message_count": incremental_message_count,
        "recorded_message_count": recorded_message_count,
    }
    if projected_used_tokens < threshold:
        return CompressionDecision(False, "below_threshold", **decision_fields)
    reason = (
        "context_usage_exceeded_threshold"
        if base_used_tokens >= threshold
        else "projected_context_usage_exceeded_threshold"
    )
    return CompressionDecision(True, reason, **decision_fields)


def _effective_threshold(
    resolved_model: ResolvedLlmProviderModel,
    config: ContextCompressionConfig,
) -> int | None:
    threshold = resolved_model.compression_threshold
    if threshold is None and resolved_model.context_window:
        threshold = int(resolved_model.context_window * config.default_threshold_ratio)
    if threshold is not None and threshold > 0:
        return threshold
    return None


def _estimate_incremental_context_tokens(
    conversation: Conversation,
    usage: dict[str, Any],
) -> tuple[int, int, int | None]:
    recorded_message_count = _recorded_message_count(conversation, usage)
    if recorded_message_count is None:
        return 0, 0, None
    messages = conversation.messages
    safe_recorded_message_count = max(0, min(recorded_message_count, len(messages)))
    incremental_messages = messages[safe_recorded_message_count:]
    return (
        sum(_estimate_message_tokens(message) for message in incremental_messages),
        len(incremental_messages),
        safe_recorded_message_count,
    )


def _recorded_message_count(conversation: Conversation, usage: dict[str, Any]) -> int | None:
    recorded_message_count = _int_or_none(usage.get("recorded_message_count"))
    if recorded_message_count is not None:
        return recorded_message_count
    recorded_message_index = _int_or_none(usage.get("recorded_message_index"))
    if recorded_message_index is not None:
        return recorded_message_index + 1
    return _latest_assistant_recorded_message_count(conversation)


def _latest_assistant_recorded_message_count(conversation: Conversation) -> int:
    for index in range(len(conversation.messages) - 1, -1, -1):
        if conversation.messages[index].role == ROLE_ASSISTANT:
            return index + 1
    return len(conversation.messages)


def _estimate_message_tokens(message: UniMessage) -> int:
    total = _MESSAGE_OVERHEAD_TOKENS + _estimate_text_tokens(str(message.role))
    for part in message.content:
        total += _estimate_part_tokens(part)
    return max(1, total)


def _estimate_part_tokens(part: object) -> int:
    if isinstance(part, TextPart):
        return _PART_OVERHEAD_TOKENS + _estimate_text_tokens(part.text)
    if isinstance(part, ImagePart | PdfPart):
        return _PART_OVERHEAD_TOKENS + _RESOURCE_REFERENCE_TOKENS + _estimate_text_tokens(part.resource_id)
    if isinstance(part, ToolUsePart):
        return (
            _PART_OVERHEAD_TOKENS
            + _estimate_text_tokens(part.name)
            + _estimate_value_tokens(part.input)
            + _estimate_text_tokens(part.tool_use_id or "")
        )
    if isinstance(part, ToolResultPart):
        return (
            _PART_OVERHEAD_TOKENS
            + _estimate_text_tokens(part.tool_use_ref)
            + (1 if part.is_error else 0)
            + _estimate_tool_result_output_tokens(part.output)
        )
    if isinstance(part, ThinkingPart):
        return _PART_OVERHEAD_TOKENS + _estimate_value_tokens(part)
    return _PART_OVERHEAD_TOKENS + _estimate_value_tokens(part)


def _estimate_tool_result_output_tokens(output: Any) -> int:
    total = 0
    for item in normalize_tool_result_output(output):
        total += _estimate_tool_result_item_tokens(item)
    return total


def _estimate_tool_result_item_tokens(item: dict[str, Any]) -> int:
    kind = item.get("kind")
    if kind == "text":
        return _PART_OVERHEAD_TOKENS + _estimate_text_tokens(str(item.get("text") or ""))
    if kind in {"image", "pdf"}:
        return (
            _PART_OVERHEAD_TOKENS
            + _RESOURCE_REFERENCE_TOKENS
            + _estimate_text_tokens(str(item.get("resource_id") or ""))
            + _estimate_text_tokens(str(item.get("url") or ""))
            + _estimate_text_tokens(str(item.get("path") or ""))
        )
    return _PART_OVERHEAD_TOKENS + _estimate_value_tokens(item)


def _estimate_value_tokens(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return _estimate_text_tokens(value)
    if isinstance(value, bool | int | float):
        return _estimate_text_tokens(str(value))
    if isinstance(value, list | tuple):
        return sum(_estimate_value_tokens(item) for item in value)
    return _estimate_text_tokens(_stable_json_text(value))


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def _stable_json_text(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=_json_default,
        )
    except (TypeError, ValueError):
        return str(value)


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return str(value)


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None

from __future__ import annotations

from agent_runtime.context_compression.config import ContextCompressionConfig
from agent_runtime.context_compression.models import CompressionDecision
from agent_runtime.conversation_message.message.models import Conversation
from agent_runtime.llm_model_registry.llm_model_registry_service import ResolvedLlmProviderModel


def decide_context_compression(
    conversation: Conversation,
    resolved_model: ResolvedLlmProviderModel,
    config: ContextCompressionConfig,
) -> CompressionDecision:
    if not config.enabled:
        return CompressionDecision(False, "config_disabled")
    usage = conversation.state.last_context_usage or {}
    used_tokens = _int_or_none(usage.get("context_window_used_tokens"))
    if used_tokens is None:
        return CompressionDecision(False, "missing_last_context_usage")
    threshold = _effective_threshold(resolved_model, config)
    if threshold is None:
        return CompressionDecision(False, "missing_context_window_threshold", used_tokens=used_tokens)
    if used_tokens < threshold:
        return CompressionDecision(False, "below_threshold", threshold=threshold, used_tokens=used_tokens)
    return CompressionDecision(True, "context_usage_exceeded_threshold", threshold=threshold, used_tokens=used_tokens)


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


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None

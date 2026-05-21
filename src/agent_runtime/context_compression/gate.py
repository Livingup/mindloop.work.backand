from __future__ import annotations

from agent_runtime.context_compression.config import ContextCompressionConfig
from agent_runtime.context_compression.models import CompressionDecision
from agent_runtime.conversation_message.message.models import Conversation
from agent_runtime.llm_model_registry.llm_model_registry_service import ResolvedLlmProviderModel


def decide_context_compression(
    conversation: Conversation,
    resolved_candidates: tuple[ResolvedLlmProviderModel, ...],
    config: ContextCompressionConfig,
) -> CompressionDecision:
    if not config.enabled:
        return CompressionDecision(False, "config_disabled")
    usage = conversation.state.last_context_usage or {}
    used_tokens = _int_or_none(usage.get("context_window_used_tokens"))
    if used_tokens is None:
        return CompressionDecision(False, "missing_last_context_usage")
    threshold = _min_effective_threshold(resolved_candidates, config)
    if threshold is None:
        return CompressionDecision(False, "missing_context_window_threshold", used_tokens=used_tokens)
    if used_tokens < threshold:
        return CompressionDecision(False, "below_threshold", threshold=threshold, used_tokens=used_tokens)
    return CompressionDecision(True, "context_usage_exceeded_threshold", threshold=threshold, used_tokens=used_tokens)


def _min_effective_threshold(
    resolved_candidates: tuple[ResolvedLlmProviderModel, ...],
    config: ContextCompressionConfig,
) -> int | None:
    values: list[int] = []
    for candidate in resolved_candidates:
        threshold = candidate.compression_threshold
        if threshold is None and candidate.context_window:
            threshold = int(candidate.context_window * config.default_threshold_ratio)
        if threshold is not None and threshold > 0:
            values.append(threshold)
    return min(values) if values else None


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None

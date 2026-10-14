from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from agent_runtime.llm_model_registry.llm_registry_validation import (
    validate_registry_id,
    validate_unique_registry_ids,
)


WebToolOperation = Literal["web_search", "read_url_context"]


@dataclass(frozen=True, slots=True)
class WebToolPolicyCandidate:
    id: str
    provider_id: str
    model_binding_id: str
    enabled: bool = True

    def __post_init__(self) -> None:
        validate_registry_id(self.id, "candidate.id")
        validate_registry_id(self.provider_id, "candidate.provider_id")
        validate_registry_id(self.model_binding_id, "candidate.model_binding_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider_id": self.provider_id,
            "model_binding_id": self.model_binding_id,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WebToolPolicyCandidate:
        return cls(
            id=str(data.get("id", "")),
            provider_id=str(data.get("provider_id", "")),
            model_binding_id=str(data.get("model_binding_id", "")),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True, slots=True)
class WebToolsRuntimeSettings:
    candidates: tuple[WebToolPolicyCandidate, ...] = field(default_factory=tuple)
    updated_at: str | None = None

    def __post_init__(self) -> None:
        validate_unique_registry_ids((item.id for item in self.candidates), "candidate.id")
        object.__setattr__(self, "candidates", tuple(self.candidates))

    def enabled_candidates(self) -> tuple[WebToolPolicyCandidate, ...]:
        return tuple(item for item in self.candidates if item.enabled)


@dataclass(frozen=True, slots=True)
class WebSearchRequest:
    query: str
    additional_queries: tuple[str, ...] = field(default_factory=tuple)
    max_results: int = 8
    allowed_domains: tuple[str, ...] = field(default_factory=tuple)
    blocked_domains: tuple[str, ...] = field(default_factory=tuple)
    recency: str | None = None


@dataclass(frozen=True, slots=True)
class ReadUrlContextRequest:
    url: str
    additional_urls: tuple[str, ...] = field(default_factory=tuple)
    question: str | None = None
    max_summary_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class WebToolResult:
    operation: WebToolOperation
    content: str
    data: dict[str, Any]
    provider_id: str
    binding_id: str
    model_id: str
    model_alias: str
    api_format: str
    candidate_index: int
    attempt_count: int
    api_key_index: int | None

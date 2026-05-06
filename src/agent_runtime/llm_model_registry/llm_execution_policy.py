# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from agent_runtime.llm_model_registry.llm_registry_validation import (
    validate_non_negative_int,
    validate_optional_text,
    validate_registry_id,
    validate_unique_registry_ids,
)

LLM_EXECUTION_STRATEGY_ORDERED_FAILOVER: Final[str] = "ordered_failover"
LLM_EXECUTION_STRATEGIES: Final[frozenset[str]] = frozenset({
    LLM_EXECUTION_STRATEGY_ORDERED_FAILOVER,
})


@dataclass(frozen=True, slots=True)
class LlmExecutionPolicyCandidate:
    id: str
    provider_id: str
    model_binding_id: str
    enabled: bool = True
    priority: int = 0

    def __post_init__(self) -> None:
        validate_registry_id(self.id, "candidate.id")
        validate_registry_id(self.provider_id, "candidate.provider_id")
        validate_registry_id(self.model_binding_id, "candidate.model_binding_id")
        validate_non_negative_int(self.priority, "candidate.priority")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider_id": self.provider_id,
            "model_binding_id": self.model_binding_id,
            "enabled": self.enabled,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LlmExecutionPolicyCandidate:
        return cls(
            id=str(data.get("id", "")),
            provider_id=str(data.get("provider_id", "")),
            model_binding_id=str(data.get("model_binding_id", "")),
            enabled=bool(data.get("enabled", True)),
            priority=int(data.get("priority", 0)),
        )


@dataclass(frozen=True, slots=True)
class LlmExecutionFailurePolicy:
    failover_on_status_codes: tuple[int, ...] = (408, 429, 500, 502, 503, 504)
    failover_on_error_types: tuple[str, ...] = ("TimeoutError", "LlmHttpRequestError")
    failover_on_auth_error: bool = True
    failover_on_context_length_error: bool = True
    max_candidates_per_request: int = 0

    def __post_init__(self) -> None:
        validate_non_negative_int(self.max_candidates_per_request, "failure_policy.max_candidates_per_request")
        status_codes: list[int] = []
        seen_status_codes: set[int] = set()
        for code in self.failover_on_status_codes:
            code_int = int(code)
            if code_int < 100 or code_int > 599:
                raise ValueError(f"failure_policy.failover_on_status_codes 包含非法 HTTP 状态码: {code_int}")
            if code_int in seen_status_codes:
                raise ValueError(f"failure_policy.failover_on_status_codes 存在重复状态码: {code_int}")
            seen_status_codes.add(code_int)
            status_codes.append(code_int)
        error_types: list[str] = []
        seen_error_types: set[str] = set()
        for error_type in self.failover_on_error_types:
            item = str(error_type).strip()
            if not item:
                raise ValueError("failure_policy.failover_on_error_types 不能包含空值。")
            if item in seen_error_types:
                raise ValueError(f"failure_policy.failover_on_error_types 存在重复值: {item}")
            seen_error_types.add(item)
            error_types.append(item)
        object.__setattr__(self, "failover_on_status_codes", tuple(status_codes))
        object.__setattr__(self, "failover_on_error_types", tuple(error_types))

    def to_dict(self) -> dict[str, Any]:
        return {
            "failover_on_status_codes": list(self.failover_on_status_codes),
            "failover_on_error_types": list(self.failover_on_error_types),
            "failover_on_auth_error": self.failover_on_auth_error,
            "failover_on_context_length_error": self.failover_on_context_length_error,
            "max_candidates_per_request": self.max_candidates_per_request,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> LlmExecutionFailurePolicy:
        data = data or {}
        return cls(
            failover_on_status_codes=tuple(int(item) for item in data.get("failover_on_status_codes", [408, 429, 500, 502, 503, 504])),
            failover_on_error_types=tuple(str(item) for item in data.get("failover_on_error_types", ["TimeoutError", "LlmHttpRequestError"])),
            failover_on_auth_error=bool(data.get("failover_on_auth_error", True)),
            failover_on_context_length_error=bool(data.get("failover_on_context_length_error", True)),
            max_candidates_per_request=int(data.get("max_candidates_per_request", 0)),
        )


@dataclass(frozen=True, slots=True)
class LlmExecutionPolicy:
    policy_group_id: str
    name: str | None = None
    description: str | None = None
    group: str = "default"
    enabled: bool = True
    strategy: str = LLM_EXECUTION_STRATEGY_ORDERED_FAILOVER
    failure_policy: LlmExecutionFailurePolicy = field(default_factory=LlmExecutionFailurePolicy)
    candidates: tuple[LlmExecutionPolicyCandidate, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        validate_registry_id(self.policy_group_id, "policy_group_id")
        validate_optional_text(self.name, "execution_policy.name")
        validate_optional_text(self.description, "execution_policy.description")
        validate_optional_text(self.group, "execution_policy.group")
        if self.strategy not in LLM_EXECUTION_STRATEGIES:
            raise ValueError(f"未知 LLM 执行策略类型: {self.strategy}")
        if not self.candidates:
            raise ValueError("execution_policy.candidates 不能为空。")
        validate_unique_registry_ids((item.id for item in self.candidates), "candidate.id")
        object.__setattr__(self, "group", self.group.strip())
        object.__setattr__(self, "candidates", tuple(self.candidates))

    def enabled_candidates(self) -> tuple[LlmExecutionPolicyCandidate, ...]:
        return tuple(sorted((item for item in self.candidates if item.enabled), key=lambda item: item.priority))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "policy_group_id": self.policy_group_id,
            "group": self.group,
            "enabled": self.enabled,
            "strategy": self.strategy,
            "failure_policy": self.failure_policy.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
        }
        if self.name is not None:
            data["name"] = self.name
        if self.description is not None:
            data["description"] = self.description
        return data

    @classmethod
    def from_dict(cls, policy_group_id: str, data: dict[str, Any]) -> LlmExecutionPolicy:
        yaml_policy_group_id = data.get("policy_group_id", policy_group_id)
        if yaml_policy_group_id != policy_group_id:
            raise ValueError(f"执行策略组文件名 ID '{policy_group_id}' 与 YAML 内 policy_group_id '{yaml_policy_group_id}' 不一致。")
        candidate_items = data.get("candidates", []) or []
        if not isinstance(candidate_items, list):
            raise ValueError("execution_policy.candidates 必须是数组。")
        return cls(
            policy_group_id=policy_group_id,
            name=data.get("name"),
            description=data.get("description"),
            group=str(data.get("group") or "default"),
            enabled=bool(data.get("enabled", True)),
            strategy=str(data.get("strategy") or LLM_EXECUTION_STRATEGY_ORDERED_FAILOVER),
            failure_policy=LlmExecutionFailurePolicy.from_dict(data.get("failure_policy")),
            candidates=tuple(
                LlmExecutionPolicyCandidate.from_dict(_require_dict(item, "execution_policy.candidates 元素"))
                for item in candidate_items
            ),
        )


def _require_dict(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} 必须是对象。")
    return value

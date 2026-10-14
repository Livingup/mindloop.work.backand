# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_runtime.llm_model_registry.llm_registry_validation import (
    validate_optional_text,
    validate_registry_id,
    validate_unique_registry_ids,
)


UNSUPPORTED_INPUT_MODALITY_STRATEGY_DEGRADE = "degrade"
UNSUPPORTED_INPUT_MODALITY_STRATEGY_SKIP_CANDIDATE = "skip_candidate"
SUPPORTED_UNSUPPORTED_INPUT_MODALITY_STRATEGIES = frozenset({
    UNSUPPORTED_INPUT_MODALITY_STRATEGY_DEGRADE,
    UNSUPPORTED_INPUT_MODALITY_STRATEGY_SKIP_CANDIDATE,
})


@dataclass(frozen=True, slots=True)
class LlmExecutionPolicyCandidate:
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
    def from_dict(cls, data: dict[str, Any]) -> LlmExecutionPolicyCandidate:
        return cls(
            id=str(data.get("id", "")),
            provider_id=str(data.get("provider_id", "")),
            model_binding_id=str(data.get("model_binding_id", "")),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True, slots=True)
class LlmExecutionPolicy:
    policy_group_id: str
    name: str | None = None
    description: str | None = None
    group: str = "default"
    enabled: bool = True
    unsupported_input_modality_strategy: str = UNSUPPORTED_INPUT_MODALITY_STRATEGY_DEGRADE
    candidates: tuple[LlmExecutionPolicyCandidate, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        validate_registry_id(self.policy_group_id, "policy_group_id")
        validate_optional_text(self.name, "execution_policy.name")
        validate_optional_text(self.description, "execution_policy.description")
        validate_optional_text(self.group, "execution_policy.group")
        strategy = self.unsupported_input_modality_strategy.strip()
        if strategy not in SUPPORTED_UNSUPPORTED_INPUT_MODALITY_STRATEGIES:
            raise ValueError(
                "execution_policy.unsupported_input_modality_strategy 必须是 "
                + ", ".join(sorted(SUPPORTED_UNSUPPORTED_INPUT_MODALITY_STRATEGIES))
            )
        if not self.candidates:
            raise ValueError("execution_policy.candidates 不能为空。")
        validate_unique_registry_ids((item.id for item in self.candidates), "candidate.id")
        object.__setattr__(self, "group", self.group.strip())
        object.__setattr__(self, "unsupported_input_modality_strategy", strategy)
        object.__setattr__(self, "candidates", tuple(self.candidates))

    def enabled_candidates(self) -> tuple[LlmExecutionPolicyCandidate, ...]:
        return tuple(item for item in self.candidates if item.enabled)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "policy_group_id": self.policy_group_id,
            "group": self.group,
            "enabled": self.enabled,
            "unsupported_input_modality_strategy": self.unsupported_input_modality_strategy,
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
            unsupported_input_modality_strategy=str(
                data.get("unsupported_input_modality_strategy")
                or UNSUPPORTED_INPUT_MODALITY_STRATEGY_DEGRADE
            ),
            candidates=tuple(
                LlmExecutionPolicyCandidate.from_dict(_require_dict(item, "execution_policy.candidates 元素"))
                for item in candidate_items
            ),
        )


def _require_dict(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} 必须是对象。")
    return value

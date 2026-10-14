# -*- coding: utf-8 -*-
"""LLM Registry PostgreSQL 仓储实现。"""
from __future__ import annotations

from typing import Callable

from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore
from agent_runtime.llm_model_registry.llm_execution_policy import LlmExecutionPolicy
from agent_runtime.llm_model_registry.llm_model_metadata import LlmModelMetadata
from agent_runtime.llm_model_registry.llm_model_registry_repository import (
    LlmModelRegistryRepository,
 )
from agent_runtime.llm_model_registry.llm_provider_definition import LlmProviderDefinition
from agent_runtime.llm_model_registry.llm_registry_validation import validate_registry_id

_MODEL_NAMESPACE = "llm_model_metadata"
_PROVIDER_NAMESPACE = "llm_provider_definition"
_POLICY_NAMESPACE = "llm_execution_policy"


class PostgresLlmModelRegistryRepository(LlmModelRegistryRepository):
    """基于云端 PostgreSQL 的 LLM Registry 仓储。

    LLM 模型元数据、服务商定义、执行策略组按聚合根 JSONB 保存；API Key 随服务商定义保存到
    用户私有 PostgreSQL 中。仓储不做本地缓存，每次读写都访问云端数据库。
    """

    def __init__(
        self,
        store_accessor: Callable[[], PostgresUserConfigStore],
        *,
        namespace_prefix: str = "",
    ) -> None:
        self._store_accessor = store_accessor
        self._model_namespace = f"{namespace_prefix}{_MODEL_NAMESPACE}"
        self._provider_namespace = f"{namespace_prefix}{_PROVIDER_NAMESPACE}"
        self._policy_namespace = f"{namespace_prefix}{_POLICY_NAMESPACE}"

    @property
    def _store(self) -> PostgresUserConfigStore:
        return self._store_accessor()

    def find_all_model_metadata(self) -> list[LlmModelMetadata]:
        return [
            _payload_to_model_metadata(payload)
            for payload in self._store.list_payloads(self._model_namespace)
        ]

    def find_model_metadata_by_id(self, model_id: str) -> LlmModelMetadata | None:
        validate_registry_id(model_id, "model_id")
        payload = self._store.get_payload(self._model_namespace, model_id)
        if payload is None:
            return None
        return _payload_to_model_metadata(payload)

    def save_model_metadata(self, metadata: LlmModelMetadata) -> None:
        self._store.save_payload(
            self._model_namespace,
            metadata.model_id,
            metadata.to_dict(),
        )

    def delete_model_metadata(self, model_id: str) -> bool:
        validate_registry_id(model_id, "model_id")
        return self._store.delete_payload(self._model_namespace, model_id)

    def contains_model_metadata(self, model_id: str) -> bool:
        validate_registry_id(model_id, "model_id")
        return self._store.contains_payload(self._model_namespace, model_id)

    def find_all_providers(self) -> list[LlmProviderDefinition]:
        return [
            _payload_to_provider(payload)
            for payload in self._store.list_payloads(self._provider_namespace)
        ]

    def find_provider_by_id(self, provider_id: str) -> LlmProviderDefinition | None:
        validate_registry_id(provider_id, "provider_id")
        payload = self._store.get_payload(self._provider_namespace, provider_id)
        if payload is None:
            return None
        return _payload_to_provider(payload)

    def save_provider(self, provider: LlmProviderDefinition) -> None:
        self._store.save_payload(
            self._provider_namespace,
            provider.provider_id,
            provider.to_dict(),
        )

    def delete_provider(self, provider_id: str) -> bool:
        validate_registry_id(provider_id, "provider_id")
        return self._store.delete_payload(self._provider_namespace, provider_id)

    def contains_provider(self, provider_id: str) -> bool:
        validate_registry_id(provider_id, "provider_id")
        return self._store.contains_payload(self._provider_namespace, provider_id)

    def find_all_execution_policies(self) -> list[LlmExecutionPolicy]:
        return [
            _payload_to_execution_policy(payload)
            for payload in self._store.list_payloads(self._policy_namespace)
        ]

    def find_execution_policy_by_id(self, policy_group_id: str) -> LlmExecutionPolicy | None:
        validate_registry_id(policy_group_id, "policy_group_id")
        payload = self._store.get_payload(self._policy_namespace, policy_group_id)
        if payload is None:
            return None
        return _payload_to_execution_policy(payload)

    def save_execution_policy(self, policy: LlmExecutionPolicy) -> None:
        self._store.save_payload(
            self._policy_namespace,
            policy.policy_group_id,
            policy.to_dict(),
        )

    def delete_execution_policy(self, policy_group_id: str) -> bool:
        validate_registry_id(policy_group_id, "policy_group_id")
        return self._store.delete_payload(self._policy_namespace, policy_group_id)

    def contains_execution_policy(self, policy_group_id: str) -> bool:
        validate_registry_id(policy_group_id, "policy_group_id")
        return self._store.contains_payload(self._policy_namespace, policy_group_id)


def _payload_to_model_metadata(payload: dict[str, object]) -> LlmModelMetadata:
    model_id = str(payload.get("model_id", ""))
    return LlmModelMetadata.from_dict(model_id, payload)


def _payload_to_provider(payload: dict[str, object]) -> LlmProviderDefinition:
    provider_id = str(payload.get("provider_id", ""))
    return LlmProviderDefinition.from_dict(provider_id, payload)


def _payload_to_execution_policy(payload: dict[str, object]) -> LlmExecutionPolicy:
    policy_group_id = str(payload.get("policy_group_id", ""))
    return LlmExecutionPolicy.from_dict(policy_group_id, payload)

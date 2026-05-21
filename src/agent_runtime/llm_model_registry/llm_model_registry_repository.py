# -*- coding: utf-8 -*-
"""LLM 服务商与模型注册仓储接口（领域层）。

领域服务只依赖该接口，不关心用户自定义注册数据最终存储在 YAML 文件、数据库还是其它介质中。
"""
from __future__ import annotations

from typing import Protocol

from agent_runtime.llm_model_registry.llm_model_metadata import LlmModelMetadata
from agent_runtime.llm_model_registry.llm_execution_policy import LlmExecutionPolicy
from agent_runtime.llm_model_registry.llm_provider_definition import LlmProviderDefinition


class LlmModelRegistryRepository(Protocol):
    """LLM 服务商与模型注册仓储接口。

    仓储以集合语义管理三类聚合根：
    - LlmModelMetadata：模型型号元数据，model_id 全局唯一；
    - LlmProviderDefinition：服务商定义，provider_id 全局唯一；
    - LlmExecutionPolicy：执行策略组，policy_group_id 全局唯一。
    """

    # ── 模型元数据集合 ──────────────────────────────────────

    def find_all_model_metadata(self) -> list[LlmModelMetadata]:
        """返回所有已持久化的模型型号元数据。"""
        ...

    def find_model_metadata_by_id(self, model_id: str) -> LlmModelMetadata | None:
        """按 model_id 查找模型元数据。未找到时返回 None。"""
        ...

    def save_model_metadata(self, metadata: LlmModelMetadata) -> None:
        """保存模型元数据。model_id 已存在时覆盖，不存在时新增。"""
        ...

    def delete_model_metadata(self, model_id: str) -> bool:
        """删除模型元数据。返回是否实际删除了文件。"""
        ...

    def contains_model_metadata(self, model_id: str) -> bool:
        """判断指定 model_id 的模型元数据是否存在。"""
        ...

    # ── 服务商定义集合 ──────────────────────────────────────

    def find_all_providers(self) -> list[LlmProviderDefinition]:
        """返回所有已持久化的 LLM 服务商定义。"""
        ...

    def find_provider_by_id(self, provider_id: str) -> LlmProviderDefinition | None:
        """按 provider_id 查找服务商定义。未找到时返回 None。"""
        ...

    def save_provider(self, provider: LlmProviderDefinition) -> None:
        """保存服务商定义。provider_id 已存在时覆盖，不存在时新增。"""
        ...

    def delete_provider(self, provider_id: str) -> bool:
        """删除服务商定义。返回是否实际删除了文件。"""
        ...

    def contains_provider(self, provider_id: str) -> bool:
        """判断指定 provider_id 的服务商定义是否存在。"""
        ...

    # ── 执行策略组集合 ──────────────────────────────────────

    def find_all_execution_policies(self) -> list[LlmExecutionPolicy]:
        """返回所有已持久化的 LLM 执行策略组。"""
        ...

    def find_execution_policy_by_id(self, policy_group_id: str) -> LlmExecutionPolicy | None:
        """按 policy_group_id 查找执行策略组。未找到时返回 None。"""
        ...

    def save_execution_policy(self, policy: LlmExecutionPolicy) -> None:
        """保存执行策略组。policy_group_id 已存在时覆盖，不存在时新增。"""
        ...

    def delete_execution_policy(self, policy_group_id: str) -> bool:
        """删除执行策略组。返回是否实际删除了文件。"""
        ...

    def contains_execution_policy(self, policy_group_id: str) -> bool:
        """判断指定 policy_group_id 的执行策略组是否存在。"""
        ...

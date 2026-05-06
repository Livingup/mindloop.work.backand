# -*- coding: utf-8 -*-
"""LLM 服务商与模型注册北向网关实现。"""
from __future__ import annotations

from agent_runtime.llm_model_registry.llm_model_metadata import LlmModelMetadata
from agent_runtime.llm_model_registry.llm_execution_policy import LlmExecutionPolicy
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    LlmModelRegistryService,
    ResolvedLlmExecutionPolicy,
    ResolvedLlmProviderModel,
)
from agent_runtime.llm_model_registry.llm_provider_definition import (
    LlmProviderDefinition,
    LlmProviderModelBinding,
)
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmExecutionPolicySaveRequest,
    LlmModelMetadataSaveRequest,
    LlmProviderSaveRequest,
)


class LlmModelRegistryGatewayImpl:
    """LLM 服务商与模型注册北向网关。

    网关是 LLM 注册模块的唯一对外入口。CLI、REST、后续 Agent 执行引擎和 LLM 统一调用客户端
    统一通过本网关访问模型元数据、服务商定义和解析后的服务商模型调用配置，避免外部调用方直接依赖
    领域服务或 YAML 仓储实现。
    """

    def __init__(self, service: LlmModelRegistryService) -> None:
        # LLM 注册领域服务。网关只负责北向入口转发和请求对象转换，具体业务规则由领域服务与聚合根承担。
        self._service = service

    # ── 模型元数据管理 ──────────────────────────────────────

    def list_model_metadata(self) -> list[LlmModelMetadata]:
        """列出所有模型型号元数据。"""
        return self._service.list_model_metadata()

    def get_model_metadata(self, model_id: str) -> LlmModelMetadata:
        """根据 model_id 获取模型元数据。未找到时抛出 KeyError。"""
        return self._service.get_model_metadata(model_id)

    def save_model_metadata(
        self,
        request: LlmModelMetadataSaveRequest,
    ) -> LlmModelMetadata:
        """新增或覆盖保存模型元数据。"""
        return self._service.save_model_metadata(request.to_metadata())

    def delete_model_metadata(self, model_id: str) -> None:
        """删除模型元数据。仍被服务商引用时抛出 ValueError。"""
        self._service.delete_model_metadata(model_id)

    # ── 服务商定义管理 ──────────────────────────────────────

    def list_providers(self) -> list[LlmProviderDefinition]:
        """列出所有 LLM 服务商定义。"""
        return self._service.list_providers()

    def get_provider(self, provider_id: str) -> LlmProviderDefinition:
        """根据 provider_id 获取服务商定义。未找到时抛出 KeyError。"""
        return self._service.get_provider(provider_id)

    def save_provider(self, request: LlmProviderSaveRequest) -> LlmProviderDefinition:
        """新增或覆盖保存 LLM 服务商定义。"""
        return self._service.save_provider(request.to_provider())

    def delete_provider(self, provider_id: str) -> None:
        """删除服务商定义。不存在时静默跳过。"""
        self._service.delete_provider(provider_id)

    # ── 执行策略组管理 ──────────────────────────────────────

    def list_execution_policies(self) -> list[LlmExecutionPolicy]:
        return self._service.list_execution_policies()

    def get_execution_policy(self, policy_group_id: str) -> LlmExecutionPolicy:
        return self._service.get_execution_policy(policy_group_id)

    def save_execution_policy(
        self,
        request: LlmExecutionPolicySaveRequest,
    ) -> LlmExecutionPolicy:
        return self._service.save_execution_policy(request.to_policy())

    def delete_execution_policy(self, policy_group_id: str) -> None:
        self._service.delete_execution_policy(policy_group_id)

    def resolve_execution_policy(
        self,
        policy_group_id: str,
    ) -> ResolvedLlmExecutionPolicy:
        return self._service.resolve_execution_policy(policy_group_id)

    # ── 服务商模型接入项查询与解析 ────────────────────────────

    def list_provider_model_bindings(
        self,
        provider_id: str,
    ) -> list[LlmProviderModelBinding]:
        """列出指定服务商下所有模型接入项。"""
        return self._service.list_provider_model_bindings(provider_id)

    def resolve_provider_model(
        self,
        provider_id: str,
        binding_id: str,
    ) -> ResolvedLlmProviderModel:
        """解析指定服务商模型接入项，返回合并后的调用配置。"""
        return self._service.resolve_provider_model(provider_id, binding_id)

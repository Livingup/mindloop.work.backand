# -*- coding: utf-8 -*-
"""LLM 服务商与模型注册 REST API 接入适配。

提供 FastAPI 路由，支持模型元数据、服务商定义和服务商模型接入项解析查询。
路由挂载到主应用时建议使用 prefix="/api/llm-registry"。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_runtime.codex_oauth.models import CODEX_DISPLAY_API_BASE_URL, CODEX_PROVIDER_ID
from agent_runtime.input_modality import INPUT_MODALITY_TEXT, SUPPORTED_INPUT_MODALITIES
from agent_runtime.llm_api_protocol import API_FORMAT_CODEX_RESPONSES, LlmApiFormat, SUPPORTED_LLM_API_FORMATS
from agent_runtime.llm_model_registry.llm_execution_policy import (
    LlmExecutionPolicy,
    LlmExecutionPolicyCandidate,
    SUPPORTED_UNSUPPORTED_INPUT_MODALITY_STRATEGIES,
    UNSUPPORTED_INPUT_MODALITY_STRATEGY_DEGRADE,
)
from agent_runtime.llm_model_registry.llm_model_metadata import LlmModelMetadata
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    ResolvedLlmExecutionPolicy,
    ResolvedLlmProviderModel,
)
from agent_runtime.llm_model_registry.llm_provider_definition import (
    LlmApiKey,
    LlmProviderDefinition,
    LlmProviderModelBinding,
    LlmRetryPolicy,
)
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmExecutionPolicySaveRequest,
    LlmModelMetadataSaveRequest,
    LlmModelRegistryGateway,
    LlmProviderSaveRequest,
)

_MANAGED_CODEX_PROVIDER_ID = CODEX_PROVIDER_ID
_MANAGED_CODEX_DISPLAY_API_BASE_URL = CODEX_DISPLAY_API_BASE_URL
_BUILTIN_PROVIDER_IDS = {_MANAGED_CODEX_PROVIDER_ID}


# ── 请求 / 响应 Pydantic 模型 ───────────────────────────────────


class LlmModelMetadataBody(BaseModel):
    """模型元数据保存请求体。

    model_id 来自 URL 路径，body 只提交模型能力字段。
    """

    context_window: int
    max_output_tokens: int
    group: str = "default"
    compression_threshold: int | None = None
    supports_tool_calling: bool = False
    supports_structured_output: bool = False
    supported_input_modalities: list[str] = Field(
        default_factory=lambda: [INPUT_MODALITY_TEXT]
    )


class LlmModelMetadataPatchBody(BaseModel):
    """模型元数据 PATCH 请求体；只更新显式提交字段。"""

    context_window: int | None = None
    max_output_tokens: int | None = None
    group: str | None = None
    compression_threshold: int | None = None
    supports_tool_calling: bool | None = None
    supports_structured_output: bool | None = None
    supported_input_modalities: list[str] | None = None


class LlmModelMetadataResponse(BaseModel):
    """模型元数据响应体。"""

    model_id: str
    context_window: int
    max_output_tokens: int
    group: str = "default"
    source: str = "custom"
    compression_threshold: int | None = None
    supports_tool_calling: bool = False
    supports_structured_output: bool = False
    supported_input_modalities: list[str] = Field(default_factory=list)


class LlmApiKeyBody(BaseModel):
    """API Key 请求体。"""

    key: str
    name: str | None = None


class LlmRetryPolicyBody(BaseModel):
    """服务商重试策略请求体。"""

    max_retries_per_key: int = 3
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    retryable_status_codes: list[int] = Field(default_factory=lambda: [429, 500, 502, 503, 504])


class LlmProviderModelBindingBody(BaseModel):
    """服务商模型接入项请求体。"""

    id: str
    model_id: str
    model_alias: str
    api_format: str
    tool_choice: str | None = None
    thinking: str | None = None
    custom_request_params: str | None = None
    api_base_url: str | None = None
    custom_headers: dict[str, str] = Field(default_factory=dict)
    api_keys: list[LlmApiKeyBody] = Field(default_factory=list)


class LlmProviderBody(BaseModel):
    """服务商定义保存请求体。

    provider_id 来自 URL 路径，body 只提交服务商调用配置和模型接入项。
    """

    group: str = "default"
    api_base_url: str
    custom_headers: dict[str, str] = Field(default_factory=dict)
    api_keys: list[LlmApiKeyBody] = Field(default_factory=list)
    retry: LlmRetryPolicyBody = Field(default_factory=LlmRetryPolicyBody)
    models: list[LlmProviderModelBindingBody] = Field(default_factory=list)


class LlmProviderPatchBody(BaseModel):
    """服务商定义 PATCH 请求体；models 需走模型接入项子资源接口。"""

    group: str | None = None
    api_base_url: str | None = None
    custom_headers: dict[str, str] | None = None
    api_keys: list[LlmApiKeyBody] | None = None
    retry: LlmRetryPolicyBody | None = None


class LlmProviderModelBindingPatchBody(BaseModel):
    """服务商模型接入项 PATCH 请求体；只更新显式提交字段。"""

    model_id: str | None = None
    model_alias: str | None = None
    api_format: str | None = None
    tool_choice: str | None = None
    thinking: str | None = None
    custom_request_params: str | None = None
    api_base_url: str | None = None
    custom_headers: dict[str, str] | None = None
    api_keys: list[LlmApiKeyBody] | None = None


class ReorderIdsBody(BaseModel):
    """子资源重排序请求体。ids 必须完整覆盖当前子资源 ID 集合。"""

    ids: list[str] = Field(default_factory=list)


class LlmProviderResponse(BaseModel):
    """服务商定义响应体。"""

    provider_id: str
    group: str = "default"
    provider_type: str = "custom"
    auth_type: str = "api_key"
    builtin: bool = False
    managed: bool = False
    api_base_url: str
    custom_headers: dict[str, str] = Field(default_factory=dict)
    api_keys: list[dict[str, Any]] = Field(default_factory=list)
    retry: dict[str, Any]
    models: list[dict[str, Any]] = Field(default_factory=list)


class LlmExecutionPolicyCandidateBody(BaseModel):
    id: str
    provider_id: str
    model_binding_id: str
    enabled: bool = True


class LlmExecutionPolicyBody(BaseModel):
    name: str | None = None
    description: str | None = None
    group: str = "default"
    enabled: bool = True
    unsupported_input_modality_strategy: str = UNSUPPORTED_INPUT_MODALITY_STRATEGY_DEGRADE
    candidates: list[LlmExecutionPolicyCandidateBody] = Field(default_factory=list)


class LlmExecutionPolicyPatchBody(BaseModel):
    """执行策略组 PATCH 请求体；候选项列表需走 candidates 子资源接口。"""

    name: str | None = None
    description: str | None = None
    group: str | None = None
    enabled: bool | None = None
    unsupported_input_modality_strategy: str | None = None


class LlmExecutionPolicyCandidatePatchBody(BaseModel):
    """执行策略候选项 PATCH 请求体。"""

    provider_id: str | None = None
    model_binding_id: str | None = None
    enabled: bool | None = None


class LlmExecutionPolicyResponse(BaseModel):
    policy_group_id: str
    name: str | None = None
    description: str | None = None
    group: str = "default"
    enabled: bool = True
    unsupported_input_modality_strategy: str = UNSUPPORTED_INPUT_MODALITY_STRATEGY_DEGRADE
    candidates: list[dict[str, Any]] = Field(default_factory=list)


class ResolvedLlmExecutionPolicyResponse(BaseModel):
    policy_group_id: str
    name: str | None = None
    description: str | None = None
    unsupported_input_modality_strategy: str = UNSUPPORTED_INPUT_MODALITY_STRATEGY_DEGRADE
    candidates: list[dict[str, Any]] = Field(default_factory=list)


class ResolvedLlmProviderModelResponse(BaseModel):
    """解析后的服务商模型调用配置响应体。"""

    provider_id: str
    binding_id: str
    model_id: str
    model_alias: str
    api_format: str
    sdk_platform: str
    api_base_url: str
    api_keys: list[dict[str, Any]] = Field(default_factory=list)
    custom_headers: dict[str, str] = Field(default_factory=dict)
    retry: dict[str, Any]
    tool_choice: str | None = None
    thinking: str | None = None
    custom_request_params: str | None = None
    context_window: int
    max_output_tokens: int
    compression_threshold: int | None = None
    supports_tool_calling: bool
    supports_structured_output: bool
    supported_input_modalities: list[str] = Field(default_factory=list)


class LlmRegistryMetadataResponse(BaseModel):
    api_formats: list[str] = Field(default_factory=list)
    input_modalities: list[str] = Field(default_factory=list)
    unsupported_input_modality_strategies: list[str] = Field(default_factory=list)


# ── 路由工厂 ─────────────────────────────────────────────────────


def create_llm_model_registry_router(
    gateway: LlmModelRegistryGateway,
    builtin_provider_ids: set[str] | None = None,
    api_formats: tuple[LlmApiFormat, ...] | None = None,
    unsupported_input_modality_strategies: tuple[str, ...] | None = None,
    readonly_models: bool = False,
) -> APIRouter:
    """根据网关实例创建 LLM 注册 REST API 路由。

    Args:
        gateway: LLM 注册模块北向网关实例。

    Returns:
        FastAPI APIRouter，挂载时建议使用 prefix="/api/llm-registry"。
    """
    router = APIRouter()
    readonly_provider_ids = _BUILTIN_PROVIDER_IDS if builtin_provider_ids is None else builtin_provider_ids
    supported_api_formats = SUPPORTED_LLM_API_FORMATS if api_formats is None else api_formats
    supported_unsupported_input_modality_strategies = (
        SUPPORTED_UNSUPPORTED_INPUT_MODALITY_STRATEGIES
        if unsupported_input_modality_strategies is None
        else frozenset(unsupported_input_modality_strategies)
    )

    @router.get("/metadata", response_model=LlmRegistryMetadataResponse)
    def get_registry_metadata() -> LlmRegistryMetadataResponse:
        return LlmRegistryMetadataResponse(
            api_formats=list(supported_api_formats),
            input_modalities=sorted(SUPPORTED_INPUT_MODALITIES),
            unsupported_input_modality_strategies=sorted(
                supported_unsupported_input_modality_strategies
            ),
        )

    @router.get("/models", response_model=list[dict[str, Any]])
    def list_model_metadata(summary: bool = False) -> list[dict[str, Any]]:
        """列出所有模型型号元数据；summary=true 时返回低噪声摘要。"""
        metadata_items = gateway.list_model_metadata()
        if summary:
            return [
                {
                    "model_id": item.model_id,
                    "group": item.group,
                    "source": item.source,
                    "context_window": item.context_window,
                    "max_output_tokens": item.max_output_tokens,
                    "supported_input_modalities": list(item.supported_input_modalities),
                }
                for item in metadata_items
            ]
        return [_metadata_to_response(item).model_dump() for item in metadata_items]

    @router.get("/models/{model_id}", response_model=LlmModelMetadataResponse)
    def get_model_metadata(model_id: str) -> LlmModelMetadataResponse:
        """根据 model_id 获取模型元数据。"""
        try:
            metadata = gateway.get_model_metadata(model_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"LLM 模型元数据不存在: {model_id}")
        return _metadata_to_response(metadata)

    @router.put("/models/{model_id}", response_model=LlmModelMetadataResponse)
    def save_model_metadata(
        model_id: str,
        body: LlmModelMetadataBody,
    ) -> LlmModelMetadataResponse:
        """新增或覆盖保存模型元数据。"""
        if readonly_models:
            raise HTTPException(status_code=409, detail="当前 Registry 的模型定义由程序内置维护，不能新增或修改。")
        try:
            metadata = gateway.save_model_metadata(_body_to_model_request(model_id, body))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _metadata_to_response(metadata)

    @router.patch("/models/{model_id}", response_model=LlmModelMetadataResponse)
    def patch_model_metadata(
        model_id: str,
        body: LlmModelMetadataPatchBody,
    ) -> LlmModelMetadataResponse:
        """部分更新模型元数据。"""
        if readonly_models:
            raise HTTPException(status_code=409, detail="当前 Registry 的模型定义由程序内置维护，不能新增或修改。")
        try:
            current = gateway.get_model_metadata(model_id)
            metadata = gateway.save_model_metadata(_patch_model_request(current, body))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"LLM 模型元数据不存在: {model_id}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _metadata_to_response(metadata)

    @router.delete("/models/{model_id}", status_code=204)
    def delete_model_metadata(model_id: str) -> None:
        """删除模型元数据。仍被服务商引用时返回 409。"""
        if readonly_models:
            raise HTTPException(status_code=409, detail="当前 Registry 的模型定义由程序内置维护，不能删除。")
        try:
            gateway.delete_model_metadata(model_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

    @router.get("/providers", response_model=list[dict[str, Any]])
    def list_providers(
        summary: bool = False,
        include_secrets: bool = False,
    ) -> list[dict[str, Any]]:
        """列出 LLM 服务商；默认脱敏 API Key，summary=true 时仅返回摘要。"""
        providers = gateway.list_providers()
        if summary:
            return [
                {
                    "provider_id": item.provider_id,
                    "group": item.group,
                    **_provider_runtime_metadata(item, readonly_provider_ids),
                    "model_count": len(item.models),
                }
                for item in providers
            ]
        return [
            _provider_to_response(
                item, readonly_provider_ids, include_secrets=include_secrets,
            ).model_dump()
            for item in providers
        ]

    @router.get("/providers/{provider_id}", response_model=LlmProviderResponse)
    def get_provider(provider_id: str, include_secrets: bool = False) -> LlmProviderResponse:
        """根据 provider_id 获取服务商定义；默认脱敏 API Key。"""
        try:
            provider = gateway.get_provider(provider_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"LLM 服务商不存在: {provider_id}")
        return _provider_to_response(provider, readonly_provider_ids, include_secrets=include_secrets)

    @router.put("/providers/{provider_id}", response_model=LlmProviderResponse)
    def save_provider(
        provider_id: str,
        body: LlmProviderBody,
    ) -> LlmProviderResponse:
        """新增或覆盖保存 LLM 服务商定义。"""
        managed_codex = _is_managed_codex_provider(provider_id, readonly_provider_ids)
        if not managed_codex and any(item.api_format == API_FORMAT_CODEX_RESPONSES for item in body.models):
            raise HTTPException(
                status_code=400,
                detail=f"{API_FORMAT_CODEX_RESPONSES} 仅保留给托管服务商 {_MANAGED_CODEX_PROVIDER_ID}。",
            )
        invalid_api_formats = sorted({
            item.api_format
            for item in body.models
            if not managed_codex and item.api_format not in supported_api_formats
        })
        if invalid_api_formats:
            raise HTTPException(
                status_code=400,
                detail="服务商模型接入项包含当前 Registry 不支持的 API 协议: "
                + ", ".join(invalid_api_formats),
            )
        try:
            try:
                current = gateway.get_provider(provider_id)
            except KeyError:
                current = None
            body = _restore_provider_body(body, current)
            request = _body_to_provider_request(provider_id, body)
            provider = gateway.save_provider(
                _normalize_managed_codex_save_request(request, readonly_provider_ids)
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _provider_to_response(provider, readonly_provider_ids)

    @router.patch("/providers/{provider_id}", response_model=LlmProviderResponse)
    def patch_provider(
        provider_id: str,
        body: LlmProviderPatchBody,
    ) -> LlmProviderResponse:
        """部分更新服务商顶层配置；模型接入项通过 /models 子资源维护。"""
        try:
            current = gateway.get_provider(provider_id)
            body = _restore_provider_patch_body(body, current)
            request = _patch_provider_request(current, body)
            provider = gateway.save_provider(
                _normalize_managed_codex_save_request(request, readonly_provider_ids)
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"LLM 服务商不存在: {provider_id}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _provider_to_response(provider, readonly_provider_ids)

    @router.delete("/providers/{provider_id}", status_code=204)
    def delete_provider(provider_id: str) -> None:
        """删除 LLM 服务商定义。不存在时静默返回 204。"""
        if _is_builtin_provider_id(provider_id, readonly_provider_ids):
            raise HTTPException(status_code=409, detail=f"内置服务商不能删除: {provider_id}")
        try:
            gateway.delete_provider(provider_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

    @router.get("/execution-policies", response_model=list[dict[str, Any]])
    def list_execution_policies(summary: bool = False) -> list[dict[str, Any]]:
        policies = gateway.list_execution_policies()
        if summary:
            return [
                {
                    "policy_group_id": item.policy_group_id,
                    "name": item.name,
                    "description": item.description,
                    "group": item.group,
                    "enabled": item.enabled,
                    "candidate_count": len(item.candidates),
                }
                for item in policies
            ]
        return [_execution_policy_to_response(item).model_dump() for item in policies]

    @router.get("/execution-policies/{policy_group_id}", response_model=LlmExecutionPolicyResponse)
    def get_execution_policy(policy_group_id: str) -> LlmExecutionPolicyResponse:
        try:
            policy = gateway.get_execution_policy(policy_group_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"LLM 执行策略组不存在: {policy_group_id}")
        return _execution_policy_to_response(policy)

    @router.put("/execution-policies/{policy_group_id}", response_model=LlmExecutionPolicyResponse)
    def save_execution_policy(
        policy_group_id: str,
        body: LlmExecutionPolicyBody,
    ) -> LlmExecutionPolicyResponse:
        if body.unsupported_input_modality_strategy not in supported_unsupported_input_modality_strategies:
            raise HTTPException(
                status_code=400,
                detail="执行策略包含当前 Registry 不支持的输入模态不兼容处理策略: "
                + body.unsupported_input_modality_strategy,
            )
        try:
            policy = gateway.save_execution_policy(
                _body_to_execution_policy_request(policy_group_id, body)
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _execution_policy_to_response(policy)

    @router.patch("/execution-policies/{policy_group_id}", response_model=LlmExecutionPolicyResponse)
    def patch_execution_policy(
        policy_group_id: str,
        body: LlmExecutionPolicyPatchBody,
    ) -> LlmExecutionPolicyResponse:
        """部分更新执行策略组顶层配置；候选项通过 candidates 子资源维护。"""
        strategy = body.unsupported_input_modality_strategy
        if strategy is not None and strategy not in supported_unsupported_input_modality_strategies:
            raise HTTPException(
                status_code=400,
                detail="执行策略包含当前 Registry 不支持的输入模态不兼容处理策略: " + strategy,
            )
        try:
            current = gateway.get_execution_policy(policy_group_id)
            policy = gateway.save_execution_policy(_patch_execution_policy_request(current, body))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"LLM 执行策略组不存在: {policy_group_id}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _execution_policy_to_response(policy)

    @router.delete("/execution-policies/{policy_group_id}", status_code=204)
    def delete_execution_policy(policy_group_id: str) -> None:
        gateway.delete_execution_policy(policy_group_id)

    @router.get(
        "/execution-policies/{policy_group_id}/resolved",
        response_model=ResolvedLlmExecutionPolicyResponse,
    )
    def resolve_execution_policy(policy_group_id: str) -> ResolvedLlmExecutionPolicyResponse:
        try:
            resolved = gateway.resolve_execution_policy(policy_group_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"LLM 执行策略组不存在: {policy_group_id}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _resolved_execution_policy_to_response(resolved)

    @router.get("/execution-policies/{policy_group_id}/candidates", response_model=list[dict[str, Any]])
    def list_execution_policy_candidates(policy_group_id: str) -> list[dict[str, Any]]:
        """列出执行策略组候选项。"""
        try:
            policy = gateway.get_execution_policy(policy_group_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"LLM 执行策略组不存在: {policy_group_id}")
        return [item.to_dict() for item in policy.candidates]

    @router.post("/execution-policies/{policy_group_id}/candidates", response_model=dict[str, Any], status_code=201)
    def create_execution_policy_candidate(
        policy_group_id: str,
        body: LlmExecutionPolicyCandidateBody,
    ) -> dict[str, Any]:
        """新增执行策略候选项。"""
        try:
            policy = gateway.get_execution_policy(policy_group_id)
            if any(item.id == body.id for item in policy.candidates):
                raise HTTPException(status_code=409, detail=f"LLM 执行策略候选项已存在: {policy_group_id}/{body.id}")
            candidate = _body_to_execution_policy_candidate(body)
            updated = _policy_with_candidates(policy, policy.candidates + (candidate,))
            policy = gateway.save_execution_policy(LlmExecutionPolicySaveRequest.from_policy(updated))
            return _get_policy_candidate(policy, candidate.id).to_dict()
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.put("/execution-policies/{policy_group_id}/candidates/{candidate_id}", response_model=dict[str, Any])
    def replace_execution_policy_candidate(
        policy_group_id: str,
        candidate_id: str,
        body: LlmExecutionPolicyCandidateBody,
    ) -> dict[str, Any]:
        """整体替换执行策略候选项，路径 candidate_id 为被替换项 ID。"""
        try:
            policy = gateway.get_execution_policy(policy_group_id)
            _get_policy_candidate(policy, candidate_id)
            replacement = _body_to_execution_policy_candidate(body)
            if replacement.id != candidate_id and any(item.id == replacement.id for item in policy.candidates):
                raise HTTPException(status_code=409, detail=f"LLM 执行策略候选项已存在: {policy_group_id}/{replacement.id}")
            candidates = tuple(replacement if item.id == candidate_id else item for item in policy.candidates)
            policy = gateway.save_execution_policy(LlmExecutionPolicySaveRequest.from_policy(_policy_with_candidates(policy, candidates)))
            return _get_policy_candidate(policy, replacement.id).to_dict()
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.patch("/execution-policies/{policy_group_id}/candidates/{candidate_id}", response_model=dict[str, Any])
    def patch_execution_policy_candidate(
        policy_group_id: str,
        candidate_id: str,
        body: LlmExecutionPolicyCandidatePatchBody,
    ) -> dict[str, Any]:
        """部分更新执行策略候选项。"""
        try:
            policy = gateway.get_execution_policy(policy_group_id)
            current = _get_policy_candidate(policy, candidate_id)
            replacement = _patch_policy_candidate(current, body)
            candidates = tuple(replacement if item.id == candidate_id else item for item in policy.candidates)
            policy = gateway.save_execution_policy(LlmExecutionPolicySaveRequest.from_policy(_policy_with_candidates(policy, candidates)))
            return _get_policy_candidate(policy, candidate_id).to_dict()
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.delete("/execution-policies/{policy_group_id}/candidates/{candidate_id}", status_code=204)
    def delete_execution_policy_candidate(policy_group_id: str, candidate_id: str) -> None:
        """删除执行策略候选项。"""
        try:
            policy = gateway.get_execution_policy(policy_group_id)
            _get_policy_candidate(policy, candidate_id)
            candidates = tuple(item for item in policy.candidates if item.id != candidate_id)
            gateway.save_execution_policy(LlmExecutionPolicySaveRequest.from_policy(_policy_with_candidates(policy, candidates)))
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.post("/execution-policies/{policy_group_id}/candidates/reorder", response_model=list[dict[str, Any]])
    def reorder_execution_policy_candidates(
        policy_group_id: str,
        body: ReorderIdsBody,
    ) -> list[dict[str, Any]]:
        """按完整 ID 列表重排执行策略候选项。"""
        try:
            policy = gateway.get_execution_policy(policy_group_id)
            candidates = _reorder_by_ids(policy.candidates, body.ids, "candidate.id")
            policy = gateway.save_execution_policy(LlmExecutionPolicySaveRequest.from_policy(_policy_with_candidates(policy, candidates)))
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return [item.to_dict() for item in policy.candidates]

    @router.get("/providers/{provider_id}/models", response_model=list[dict[str, Any]])
    def list_provider_model_bindings(
        provider_id: str,
        include_secrets: bool = False,
    ) -> list[dict[str, Any]]:
        """列出指定服务商下所有模型接入项；默认脱敏 API Key。"""
        try:
            bindings = gateway.list_provider_model_bindings(provider_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"LLM 服务商不存在: {provider_id}")
        items = [item.to_dict() for item in bindings]
        if include_secrets:
            return items
        return [_mask_binding_dict(item) for item in items]

    @router.get("/providers/{provider_id}/models/{binding_id}", response_model=dict[str, Any])
    def get_provider_model_binding(
        provider_id: str,
        binding_id: str,
        include_secrets: bool = False,
    ) -> dict[str, Any]:
        """获取单个服务商模型接入项；默认脱敏 API Key。"""
        try:
            binding = _get_provider_binding(gateway.get_provider(provider_id), binding_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        data = binding.to_dict()
        return data if include_secrets else _mask_binding_dict(data)

    @router.post("/providers/{provider_id}/models", response_model=dict[str, Any], status_code=201)
    def create_provider_model_binding(
        provider_id: str,
        body: LlmProviderModelBindingBody,
        include_secrets: bool = False,
    ) -> dict[str, Any]:
        """为服务商新增模型接入项。"""
        managed_codex = _is_managed_codex_provider(provider_id, readonly_provider_ids)
        if not managed_codex and body.api_format == API_FORMAT_CODEX_RESPONSES:
            raise HTTPException(
                status_code=400,
                detail=f"{API_FORMAT_CODEX_RESPONSES} 仅保留给托管服务商 {_MANAGED_CODEX_PROVIDER_ID}。",
            )
        if not managed_codex and body.api_format not in supported_api_formats:
            raise HTTPException(status_code=400, detail="服务商模型接入项包含当前 Registry 不支持的 API 协议: " + body.api_format)
        try:
            provider = gateway.get_provider(provider_id)
            if any(item.id == body.id for item in provider.models):
                raise HTTPException(status_code=409, detail=f"LLM 服务商模型接入项已存在: {provider_id}/{body.id}")
            binding = _body_to_binding(body)
            updated = _provider_with_models(provider, provider.models + (binding,))
            updated = _normalize_managed_codex_provider(updated, readonly_provider_ids)
            provider = gateway.save_provider(LlmProviderSaveRequest.from_provider(updated))
            created = _get_provider_binding(provider, binding.id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"LLM 服务商不存在: {provider_id}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        data = created.to_dict()
        return data if include_secrets else _mask_binding_dict(data)

    @router.put("/providers/{provider_id}/models/{binding_id}", response_model=dict[str, Any])
    def replace_provider_model_binding(
        provider_id: str,
        binding_id: str,
        body: LlmProviderModelBindingBody,
        include_secrets: bool = False,
    ) -> dict[str, Any]:
        """整体替换服务商模型接入项，路径 binding_id 为被替换项 ID。"""
        managed_codex = _is_managed_codex_provider(provider_id, readonly_provider_ids)
        if not managed_codex and body.api_format == API_FORMAT_CODEX_RESPONSES:
            raise HTTPException(
                status_code=400,
                detail=f"{API_FORMAT_CODEX_RESPONSES} 仅保留给托管服务商 {_MANAGED_CODEX_PROVIDER_ID}。",
            )
        if not managed_codex and body.api_format not in supported_api_formats:
            raise HTTPException(status_code=400, detail="服务商模型接入项包含当前 Registry 不支持的 API 协议: " + body.api_format)
        try:
            provider = gateway.get_provider(provider_id)
            current_binding = next((item for item in provider.models if item.id == binding_id), None)
            body = _restore_binding_body(body, current_binding)
            _get_provider_binding(provider, binding_id)
            replacement = _body_to_binding(body)
            if replacement.id != binding_id and any(item.id == replacement.id for item in provider.models):
                raise HTTPException(status_code=409, detail=f"LLM 服务商模型接入项已存在: {provider_id}/{replacement.id}")
            models = tuple(replacement if item.id == binding_id else item for item in provider.models)
            updated = _normalize_managed_codex_provider(
                _provider_with_models(provider, models), readonly_provider_ids
            )
            provider = gateway.save_provider(LlmProviderSaveRequest.from_provider(updated))
            binding = _get_provider_binding(provider, replacement.id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        data = binding.to_dict()
        return data if include_secrets else _mask_binding_dict(data)

    @router.patch("/providers/{provider_id}/models/{binding_id}", response_model=dict[str, Any])
    def patch_provider_model_binding(
        provider_id: str,
        binding_id: str,
        body: LlmProviderModelBindingPatchBody,
        include_secrets: bool = False,
    ) -> dict[str, Any]:
        """部分更新服务商模型接入项。"""
        managed_codex = _is_managed_codex_provider(provider_id, readonly_provider_ids)
        if not managed_codex and body.api_format == API_FORMAT_CODEX_RESPONSES:
            raise HTTPException(
                status_code=400,
                detail=f"{API_FORMAT_CODEX_RESPONSES} 仅保留给托管服务商 {_MANAGED_CODEX_PROVIDER_ID}。",
            )
        if not managed_codex and body.api_format is not None and body.api_format not in supported_api_formats:
            raise HTTPException(status_code=400, detail="服务商模型接入项包含当前 Registry 不支持的 API 协议: " + body.api_format)
        try:
            provider = gateway.get_provider(provider_id)
            current = _get_provider_binding(provider, binding_id)
            body = _restore_binding_patch_body(body, current)
            replacement = _patch_provider_binding(current, body)
            models = tuple(replacement if item.id == binding_id else item for item in provider.models)
            updated = _normalize_managed_codex_provider(
                _provider_with_models(provider, models), readonly_provider_ids
            )
            provider = gateway.save_provider(LlmProviderSaveRequest.from_provider(updated))
            binding = _get_provider_binding(provider, binding_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        data = binding.to_dict()
        return data if include_secrets else _mask_binding_dict(data)

    @router.delete("/providers/{provider_id}/models/{binding_id}", status_code=204)
    def delete_provider_model_binding(provider_id: str, binding_id: str) -> None:
        """删除服务商模型接入项；被执行策略引用时返回 409。"""
        try:
            provider = gateway.get_provider(provider_id)
            _get_provider_binding(provider, binding_id)
            models = tuple(item for item in provider.models if item.id != binding_id)
            updated = _normalize_managed_codex_provider(
                _provider_with_models(provider, models), readonly_provider_ids
            )
            gateway.save_provider(LlmProviderSaveRequest.from_provider(updated))
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

    @router.post("/providers/{provider_id}/models/reorder", response_model=list[dict[str, Any]])
    def reorder_provider_model_bindings(
        provider_id: str,
        body: ReorderIdsBody,
        include_secrets: bool = False,
    ) -> list[dict[str, Any]]:
        """按完整 ID 列表重排服务商模型接入项。"""
        try:
            provider = gateway.get_provider(provider_id)
            models = _reorder_by_ids(provider.models, body.ids, "provider_model.id")
            updated = _normalize_managed_codex_provider(
                _provider_with_models(provider, models), readonly_provider_ids
            )
            provider = gateway.save_provider(LlmProviderSaveRequest.from_provider(updated))
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        items = [item.to_dict() for item in provider.models]
        return items if include_secrets else [_mask_binding_dict(item) for item in items]

    @router.get(
        "/providers/{provider_id}/models/{binding_id}/resolved",
        response_model=ResolvedLlmProviderModelResponse,
    )
    def resolve_provider_model(
        provider_id: str,
        binding_id: str,
        include_secrets: bool = False,
    ) -> ResolvedLlmProviderModelResponse:
        """解析指定服务商模型接入项，返回合并后的调用配置；默认脱敏 API Key。"""
        try:
            resolved = gateway.resolve_provider_model(provider_id, binding_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return _resolved_to_response(resolved, include_secrets=include_secrets)

    return router


# ── DTO 转换辅助函数 ─────────────────────────────────────────────


def _metadata_to_response(metadata: LlmModelMetadata) -> LlmModelMetadataResponse:
    """将模型元数据领域实体转换为 REST 响应模型。"""
    return LlmModelMetadataResponse(**metadata.to_dict())


def _provider_to_response(
    provider: LlmProviderDefinition,
    builtin_provider_ids: set[str] | None = None,
    *,
    include_secrets: bool = False,
) -> LlmProviderResponse:
    """将服务商定义领域实体转换为 REST 响应模型；默认脱敏 API Key。"""
    data = provider.to_dict()
    if not include_secrets:
        data = _mask_provider_dict(data)
    return LlmProviderResponse(**data, **_provider_runtime_metadata(provider, builtin_provider_ids))


def _provider_runtime_metadata(provider: LlmProviderDefinition, builtin_provider_ids: set[str] | None = None) -> dict[str, Any]:
    auth_type = "oauth" if any(item.api_format == API_FORMAT_CODEX_RESPONSES for item in provider.models) else "api_key"
    readonly_provider_ids = _BUILTIN_PROVIDER_IDS if builtin_provider_ids is None else builtin_provider_ids
    builtin = _is_builtin_provider_id(provider.provider_id, readonly_provider_ids)
    return {
        "provider_type": "builtin" if builtin else "custom",
        "auth_type": auth_type,
        "builtin": builtin,
        "managed": builtin,
    }


def _is_builtin_provider_id(provider_id: str, builtin_provider_ids: set[str] | None = None) -> bool:
    return provider_id in (_BUILTIN_PROVIDER_IDS if builtin_provider_ids is None else builtin_provider_ids)


def _is_managed_codex_provider(
    provider_id: str,
    builtin_provider_ids: set[str] | None = None,
 ) -> bool:
    return (
        provider_id == _MANAGED_CODEX_PROVIDER_ID
        and _is_builtin_provider_id(provider_id, builtin_provider_ids)
    )


def _managed_codex_retry_policy() -> LlmRetryPolicy:
    return LlmRetryPolicy(
        max_retries_per_key=0,
        initial_delay_seconds=0,
        max_delay_seconds=0,
        retryable_status_codes=(),
    )


def _managed_codex_binding(binding: LlmProviderModelBinding) -> LlmProviderModelBinding:
    return LlmProviderModelBinding(
        id=binding.id,
        model_id=binding.model_id,
        model_alias=binding.model_alias,
        api_format=API_FORMAT_CODEX_RESPONSES,
        tool_choice=binding.tool_choice,
        thinking=binding.thinking,
        custom_request_params=binding.custom_request_params,
        api_base_url=None,
        custom_headers={},
        api_keys=(),
    )


def _normalize_managed_codex_provider(
    provider: LlmProviderDefinition,
    builtin_provider_ids: set[str] | None = None,
 ) -> LlmProviderDefinition:
    if not _is_managed_codex_provider(provider.provider_id, builtin_provider_ids):
        return provider
    return LlmProviderDefinition(
        provider_id=provider.provider_id,
        group=provider.group,
        api_base_url=_MANAGED_CODEX_DISPLAY_API_BASE_URL,
        custom_headers={},
        api_keys=(),
        retry=_managed_codex_retry_policy(),
        models=tuple(_managed_codex_binding(binding) for binding in provider.models),
    )


def _normalize_managed_codex_save_request(
    request: LlmProviderSaveRequest,
    builtin_provider_ids: set[str] | None = None,
 ) -> LlmProviderSaveRequest:
    if not _is_managed_codex_provider(request.provider_id, builtin_provider_ids):
        return request
    return LlmProviderSaveRequest(
        provider_id=request.provider_id,
        group=request.group,
        api_base_url=_MANAGED_CODEX_DISPLAY_API_BASE_URL,
        custom_headers={},
        api_keys=(),
        retry=_managed_codex_retry_policy(),
        models=tuple(_managed_codex_binding(binding) for binding in request.models),
    )


def _body_fields_set(body: BaseModel) -> set[str]:
    """Return fields explicitly submitted to a Pydantic v1/v2 model."""
    return set(getattr(body, "model_fields_set", getattr(body, "__fields_set__", set())))


def _patch_model_request(
    current: LlmModelMetadata,
    body: LlmModelMetadataPatchBody,
) -> LlmModelMetadataSaveRequest:
    fields = _body_fields_set(body)
    return LlmModelMetadataSaveRequest(
        model_id=current.model_id,
        context_window=body.context_window if "context_window" in fields else current.context_window,
        max_output_tokens=body.max_output_tokens if "max_output_tokens" in fields else current.max_output_tokens,
        group=body.group if "group" in fields else current.group,
        source=current.source,
        compression_threshold=(
            body.compression_threshold
            if "compression_threshold" in fields else current.compression_threshold
        ),
        supports_tool_calling=(
            body.supports_tool_calling
            if "supports_tool_calling" in fields else current.supports_tool_calling
        ),
        supports_structured_output=(
            body.supports_structured_output
            if "supports_structured_output" in fields else current.supports_structured_output
        ),
        supported_input_modalities=(
            tuple(body.supported_input_modalities)
            if "supported_input_modalities" in fields and body.supported_input_modalities is not None
            else current.supported_input_modalities
        ),
    )


def _patch_provider_request(
    current: LlmProviderDefinition,
    body: LlmProviderPatchBody,
) -> LlmProviderSaveRequest:
    fields = _body_fields_set(body)
    return LlmProviderSaveRequest(
        provider_id=current.provider_id,
        group=body.group if "group" in fields else current.group,
        api_base_url=body.api_base_url if "api_base_url" in fields else current.api_base_url,
        custom_headers=(
            dict(body.custom_headers or {})
            if "custom_headers" in fields else dict(current.custom_headers)
        ),
        api_keys=(
            tuple(_body_to_api_key(item) for item in (body.api_keys or []))
            if "api_keys" in fields else current.api_keys
        ),
        retry=(
            _body_to_retry_policy(body.retry)
            if "retry" in fields and body.retry is not None else current.retry
        ),
        models=current.models,
    )


def _provider_with_models(
    provider: LlmProviderDefinition,
    models: tuple[LlmProviderModelBinding, ...],
) -> LlmProviderDefinition:
    return LlmProviderDefinition(
        provider_id=provider.provider_id,
        group=provider.group,
        api_base_url=provider.api_base_url,
        custom_headers=dict(provider.custom_headers),
        api_keys=provider.api_keys,
        retry=provider.retry,
        models=models,
    )


def _get_provider_binding(
    provider: LlmProviderDefinition,
    binding_id: str,
) -> LlmProviderModelBinding:
    binding = next((item for item in provider.models if item.id == binding_id), None)
    if binding is None:
        raise KeyError(f"LLM 服务商模型接入项不存在: {provider.provider_id}/{binding_id}")
    return binding


def _patch_provider_binding(
    current: LlmProviderModelBinding,
    body: LlmProviderModelBindingPatchBody,
) -> LlmProviderModelBinding:
    fields = _body_fields_set(body)
    return LlmProviderModelBinding(
        id=current.id,
        model_id=body.model_id if "model_id" in fields else current.model_id,
        model_alias=body.model_alias if "model_alias" in fields else current.model_alias,
        api_format=body.api_format if "api_format" in fields else current.api_format,
        tool_choice=body.tool_choice if "tool_choice" in fields else current.tool_choice,
        thinking=body.thinking if "thinking" in fields else current.thinking,
        custom_request_params=(
            body.custom_request_params
            if "custom_request_params" in fields else current.custom_request_params
        ),
        api_base_url=body.api_base_url if "api_base_url" in fields else current.api_base_url,
        custom_headers=(
            dict(body.custom_headers or {})
            if "custom_headers" in fields else dict(current.custom_headers)
        ),
        api_keys=(
            tuple(_body_to_api_key(item) for item in (body.api_keys or []))
            if "api_keys" in fields else current.api_keys
        ),
    )


def _patch_execution_policy_request(
    current: LlmExecutionPolicy,
    body: LlmExecutionPolicyPatchBody,
) -> LlmExecutionPolicySaveRequest:
    fields = _body_fields_set(body)
    return LlmExecutionPolicySaveRequest(
        policy_group_id=current.policy_group_id,
        name=body.name if "name" in fields else current.name,
        description=body.description if "description" in fields else current.description,
        group=body.group if "group" in fields else current.group,
        enabled=body.enabled if "enabled" in fields else current.enabled,
        unsupported_input_modality_strategy=(
            body.unsupported_input_modality_strategy
            if "unsupported_input_modality_strategy" in fields
            else current.unsupported_input_modality_strategy
        ),
        candidates=current.candidates,
    )


def _policy_with_candidates(
    policy: LlmExecutionPolicy,
    candidates: tuple[LlmExecutionPolicyCandidate, ...],
) -> LlmExecutionPolicy:
    return LlmExecutionPolicy(
        policy_group_id=policy.policy_group_id,
        name=policy.name,
        description=policy.description,
        group=policy.group,
        enabled=policy.enabled,
        unsupported_input_modality_strategy=policy.unsupported_input_modality_strategy,
        candidates=candidates,
    )


def _get_policy_candidate(
    policy: LlmExecutionPolicy,
    candidate_id: str,
) -> LlmExecutionPolicyCandidate:
    candidate = next((item for item in policy.candidates if item.id == candidate_id), None)
    if candidate is None:
        raise KeyError(f"LLM 执行策略候选项不存在: {policy.policy_group_id}/{candidate_id}")
    return candidate


def _patch_policy_candidate(
    current: LlmExecutionPolicyCandidate,
    body: LlmExecutionPolicyCandidatePatchBody,
) -> LlmExecutionPolicyCandidate:
    fields = _body_fields_set(body)
    return LlmExecutionPolicyCandidate(
        id=current.id,
        provider_id=body.provider_id if "provider_id" in fields else current.provider_id,
        model_binding_id=(
            body.model_binding_id if "model_binding_id" in fields else current.model_binding_id
        ),
        enabled=body.enabled if "enabled" in fields else current.enabled,
    )


def _reorder_by_ids(items: tuple[Any, ...], ids: list[str], field_name: str) -> tuple[Any, ...]:
    if len(ids) != len(set(ids)):
        raise ValueError(f"{field_name} 重排序列表不能包含重复 ID。")
    by_id = {item.id: item for item in items}
    current_ids = set(by_id)
    requested_ids = set(ids)
    if requested_ids != current_ids:
        missing = sorted(current_ids - requested_ids)
        unknown = sorted(requested_ids - current_ids)
        details: list[str] = []
        if missing:
            details.append("缺少: " + ", ".join(missing))
        if unknown:
            details.append("未知: " + ", ".join(unknown))
        raise ValueError(f"{field_name} 重排序列表必须完整覆盖当前 ID。" + "；".join(details))
    return tuple(by_id[item_id] for item_id in ids)


def _execution_policy_to_response(policy: LlmExecutionPolicy) -> LlmExecutionPolicyResponse:
    return LlmExecutionPolicyResponse(**policy.to_dict())


def _resolved_to_response(
    resolved: ResolvedLlmProviderModel,
    *,
    include_secrets: bool = False,
) -> ResolvedLlmProviderModelResponse:
    """将解析后的服务商模型调用配置转换为 REST 响应模型；默认脱敏 API Key。"""
    data = resolved.to_dict()
    if not include_secrets:
        data = {
            **data,
            "api_keys": _mask_api_keys(data.get("api_keys", [])),
            "custom_headers": _mask_custom_headers(data.get("custom_headers", {})),
        }
    return ResolvedLlmProviderModelResponse(**data)


def _resolved_execution_policy_to_response(
    resolved: ResolvedLlmExecutionPolicy,
) -> ResolvedLlmExecutionPolicyResponse:
    return ResolvedLlmExecutionPolicyResponse(**resolved.to_dict())


def _mask_provider_dict(data: dict[str, Any]) -> dict[str, Any]:
    masked = dict(data)
    masked["api_keys"] = _mask_api_keys(masked.get("api_keys", []))
    masked["custom_headers"] = _mask_custom_headers(masked.get("custom_headers", {}))
    masked["models"] = [
        _mask_binding_dict(item)
        for item in masked.get("models", [])
        if isinstance(item, dict)
    ]
    return masked


def _mask_binding_dict(data: dict[str, Any]) -> dict[str, Any]:
    masked = dict(data)
    masked["api_keys"] = _mask_api_keys(masked.get("api_keys", []))
    masked["custom_headers"] = _mask_custom_headers(masked.get("custom_headers", {}))
    return masked


def _mask_custom_headers(headers: object) -> dict[str, str]:
    if not isinstance(headers, dict):
        return {}
    masked: dict[str, str] = {}
    sensitive_markers = ("authorization", "api-key", "apikey", "x-api-key", "token", "secret", "password", "key")
    for key, value in headers.items():
        header_name = str(key)
        if any(marker in header_name.lower() for marker in sensitive_markers):
            masked[header_name] = "********"
        else:
            masked[header_name] = str(value)
    return masked


def _mask_api_keys(api_keys: object) -> list[dict[str, Any]]:
    if not isinstance(api_keys, list):
        return []
    masked: list[dict[str, Any]] = []
    for item in api_keys:
        if not isinstance(item, dict):
            continue
        original_key = str(item.get("key", ""))
        masked_item = {key: value for key, value in item.items() if key != "key"}
        if len(original_key) > 8:
            masked_item["key"] = f"{original_key[:4]}********{original_key[-4:]}"
        else:
            masked_item["key"] = "********"
        masked_item["masked"] = True
        masked.append(masked_item)
    return masked


def _body_to_model_request(
    model_id: str,
    body: LlmModelMetadataBody,
) -> LlmModelMetadataSaveRequest:
    """将 REST 模型元数据保存请求体转换为北向保存请求。"""
    return LlmModelMetadataSaveRequest(
        model_id=model_id,
        context_window=body.context_window,
        max_output_tokens=body.max_output_tokens,
        group=body.group,
        source="custom",
        compression_threshold=body.compression_threshold,
        supports_tool_calling=body.supports_tool_calling,
        supports_structured_output=body.supports_structured_output,
        supported_input_modalities=tuple(body.supported_input_modalities),
    )


def _body_to_provider_request(
    provider_id: str,
    body: LlmProviderBody,
) -> LlmProviderSaveRequest:
    """将 REST 服务商保存请求体转换为北向保存请求。"""
    return LlmProviderSaveRequest(
        provider_id=provider_id,
        group=body.group,
        api_base_url=body.api_base_url,
        custom_headers=dict(body.custom_headers),
        api_keys=tuple(_body_to_api_key(item) for item in body.api_keys),
        retry=_body_to_retry_policy(body.retry),
        models=tuple(_body_to_binding(item) for item in body.models),
    )


def _restore_api_keys_from_current(
    body_keys: list[LlmApiKeyBody] | None,
    current_keys: tuple[LlmApiKey, ...]
) -> list[LlmApiKeyBody] | None:
    if body_keys is None:
        return None
    restored: list[LlmApiKeyBody] = []
    unnamed_current_pool = [k for k in current_keys if not k.name]
    named_current_map = {k.name: k for k in current_keys if k.name}

    for bk in body_keys:
        if "********" in bk.key:
            if bk.name and bk.name in named_current_map:
                restored.append(LlmApiKeyBody(key=named_current_map[bk.name].key, name=bk.name))
            elif not bk.name and unnamed_current_pool:
                original = unnamed_current_pool.pop(0)
                restored.append(LlmApiKeyBody(key=original.key, name=bk.name))
            else:
                restored.append(bk)
        else:
            restored.append(bk)
    return restored


def _restore_custom_headers_from_current(
    body_headers: dict[str, str] | None,
    current_headers: dict[str, str]
) -> dict[str, str] | None:
    if body_headers is None:
        return None
    restored: dict[str, str] = {}
    for k, v in body_headers.items():
        if "********" in v and k in current_headers:
            restored[k] = current_headers[k]
        else:
            restored[k] = v
    return restored


def _restore_provider_body(
    body: LlmProviderBody,
    current: LlmProviderDefinition | None
) -> LlmProviderBody:
    if current is None:
        return body
    if body.custom_headers is not None:
        body.custom_headers = _restore_custom_headers_from_current(body.custom_headers, current.custom_headers) or {}
    if body.api_keys is not None:
        body.api_keys = _restore_api_keys_from_current(body.api_keys, current.api_keys) or []
    for mb in body.models:
        current_mb = next((m for m in current.models if m.id == mb.id), None)
        if current_mb:
            if mb.custom_headers is not None:
                mb.custom_headers = _restore_custom_headers_from_current(mb.custom_headers, current_mb.custom_headers) or {}
            if mb.api_keys is not None:
                mb.api_keys = _restore_api_keys_from_current(mb.api_keys, current_mb.api_keys) or []
    return body


def _restore_provider_patch_body(
    body: LlmProviderPatchBody,
    current: LlmProviderDefinition
) -> LlmProviderPatchBody:
    if body.custom_headers is not None:
        body.custom_headers = _restore_custom_headers_from_current(body.custom_headers, current.custom_headers)
    if body.api_keys is not None:
        body.api_keys = _restore_api_keys_from_current(body.api_keys, current.api_keys)
    return body


def _restore_binding_body(
    body: LlmProviderModelBindingBody,
    current: LlmProviderModelBinding | None
) -> LlmProviderModelBindingBody:
    if current is None:
        return body
    if body.custom_headers is not None:
        body.custom_headers = _restore_custom_headers_from_current(body.custom_headers, current.custom_headers) or {}
    if body.api_keys is not None:
        body.api_keys = _restore_api_keys_from_current(body.api_keys, current.api_keys) or []
    return body


def _restore_binding_patch_body(
    body: LlmProviderModelBindingPatchBody,
    current: LlmProviderModelBinding
) -> LlmProviderModelBindingPatchBody:
    if body.custom_headers is not None:
        body.custom_headers = _restore_custom_headers_from_current(body.custom_headers, current.custom_headers)
    if body.api_keys is not None:
        body.api_keys = _restore_api_keys_from_current(body.api_keys, current.api_keys)
    return body


def _body_to_execution_policy_request(
    policy_group_id: str,
    body: LlmExecutionPolicyBody,
) -> LlmExecutionPolicySaveRequest:
    return LlmExecutionPolicySaveRequest(
        policy_group_id=policy_group_id,
        name=body.name,
        description=body.description,
        group=body.group,
        enabled=body.enabled,
        unsupported_input_modality_strategy=body.unsupported_input_modality_strategy,
        candidates=tuple(_body_to_execution_policy_candidate(item) for item in body.candidates),
    )


def _body_to_api_key(body: LlmApiKeyBody) -> LlmApiKey:
    """将 API Key 请求体转换为领域值对象。"""
    return LlmApiKey(key=body.key, name=body.name)


def _body_to_retry_policy(body: LlmRetryPolicyBody) -> LlmRetryPolicy:
    """将重试策略请求体转换为领域值对象。"""
    return LlmRetryPolicy(
        max_retries_per_key=body.max_retries_per_key,
        initial_delay_seconds=body.initial_delay_seconds,
        max_delay_seconds=body.max_delay_seconds,
        retryable_status_codes=tuple(body.retryable_status_codes),
    )


def _body_to_execution_policy_candidate(
    body: LlmExecutionPolicyCandidateBody,
) -> LlmExecutionPolicyCandidate:
    return LlmExecutionPolicyCandidate(
        id=body.id,
        provider_id=body.provider_id,
        model_binding_id=body.model_binding_id,
        enabled=body.enabled,
    )


def _body_to_binding(body: LlmProviderModelBindingBody) -> LlmProviderModelBinding:
    """将服务商模型接入项请求体转换为领域实体。"""
    return LlmProviderModelBinding(
        id=body.id,
        model_id=body.model_id,
        model_alias=body.model_alias,
        api_format=body.api_format,
        tool_choice=body.tool_choice,
        thinking=body.thinking,
        custom_request_params=body.custom_request_params,
        api_base_url=body.api_base_url,
        custom_headers=dict(body.custom_headers),
        api_keys=tuple(_body_to_api_key(item) for item in body.api_keys),
    )

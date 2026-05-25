# -*- coding: utf-8 -*-
"""LLM 服务商与模型注册 REST API 接入适配。

提供 FastAPI 路由，支持模型元数据、服务商定义和服务商模型接入项解析查询。
路由挂载到主应用时建议使用 prefix="/api/llm-registry"。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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

_BUILTIN_PROVIDER_IDS = {"openai_codex"}


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

    @router.get("/metadata", response_model=LlmRegistryMetadataResponse)
    def get_registry_metadata() -> LlmRegistryMetadataResponse:
        return LlmRegistryMetadataResponse(
            api_formats=list(supported_api_formats),
            input_modalities=sorted(SUPPORTED_INPUT_MODALITIES),
            unsupported_input_modality_strategies=sorted(
                SUPPORTED_UNSUPPORTED_INPUT_MODALITY_STRATEGIES
            ),
        )

    @router.get("/models", response_model=list[LlmModelMetadataResponse])
    def list_model_metadata() -> list[LlmModelMetadataResponse]:
        """列出所有模型型号元数据。"""
        return [_metadata_to_response(item) for item in gateway.list_model_metadata()]

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
        try:
            metadata = gateway.save_model_metadata(_body_to_model_request(model_id, body))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _metadata_to_response(metadata)

    @router.delete("/models/{model_id}", status_code=204)
    def delete_model_metadata(model_id: str) -> None:
        """删除模型元数据。仍被服务商引用时返回 409。"""
        try:
            gateway.delete_model_metadata(model_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

    @router.get("/providers", response_model=list[LlmProviderResponse])
    def list_providers() -> list[LlmProviderResponse]:
        """列出所有 LLM 服务商定义。"""
        return [_provider_to_response(item, readonly_provider_ids) for item in gateway.list_providers()]

    @router.get("/providers/{provider_id}", response_model=LlmProviderResponse)
    def get_provider(provider_id: str) -> LlmProviderResponse:
        """根据 provider_id 获取服务商定义。"""
        try:
            provider = gateway.get_provider(provider_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"LLM 服务商不存在: {provider_id}")
        return _provider_to_response(provider, readonly_provider_ids)

    @router.put("/providers/{provider_id}", response_model=LlmProviderResponse)
    def save_provider(
        provider_id: str,
        body: LlmProviderBody,
    ) -> LlmProviderResponse:
        """新增或覆盖保存 LLM 服务商定义。"""
        invalid_api_formats = sorted({
            item.api_format
            for item in body.models
            if item.api_format not in supported_api_formats
        })
        if invalid_api_formats:
            raise HTTPException(
                status_code=400,
                detail="服务商模型接入项包含当前 Registry 不支持的 API 协议: "
                + ", ".join(invalid_api_formats),
            )
        try:
            provider = gateway.save_provider(_body_to_provider_request(provider_id, body))
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

    @router.get("/execution-policies", response_model=list[LlmExecutionPolicyResponse])
    def list_execution_policies() -> list[LlmExecutionPolicyResponse]:
        return [_execution_policy_to_response(item) for item in gateway.list_execution_policies()]

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
        try:
            policy = gateway.save_execution_policy(
                _body_to_execution_policy_request(policy_group_id, body)
            )
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

    @router.get("/providers/{provider_id}/models", response_model=list[dict[str, Any]])
    def list_provider_model_bindings(provider_id: str) -> list[dict[str, Any]]:
        """列出指定服务商下所有模型接入项。"""
        try:
            bindings = gateway.list_provider_model_bindings(provider_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"LLM 服务商不存在: {provider_id}")
        return [item.to_dict() for item in bindings]

    @router.get(
        "/providers/{provider_id}/models/{binding_id}/resolved",
        response_model=ResolvedLlmProviderModelResponse,
    )
    def resolve_provider_model(
        provider_id: str,
        binding_id: str,
    ) -> ResolvedLlmProviderModelResponse:
        """解析指定服务商模型接入项，返回合并后的调用配置。"""
        try:
            resolved = gateway.resolve_provider_model(provider_id, binding_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return _resolved_to_response(resolved)

    return router


# ── DTO 转换辅助函数 ─────────────────────────────────────────────


def _metadata_to_response(metadata: LlmModelMetadata) -> LlmModelMetadataResponse:
    """将模型元数据领域实体转换为 REST 响应模型。"""
    return LlmModelMetadataResponse(**metadata.to_dict())


def _provider_to_response(provider: LlmProviderDefinition, builtin_provider_ids: set[str] | None = None) -> LlmProviderResponse:
    """将服务商定义领域实体转换为 REST 响应模型。"""
    return LlmProviderResponse(**provider.to_dict(), **_provider_runtime_metadata(provider, builtin_provider_ids))


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


def _execution_policy_to_response(policy: LlmExecutionPolicy) -> LlmExecutionPolicyResponse:
    return LlmExecutionPolicyResponse(**policy.to_dict())


def _resolved_to_response(
    resolved: ResolvedLlmProviderModel,
) -> ResolvedLlmProviderModelResponse:
    """将解析后的服务商模型调用配置转换为 REST 响应模型。"""
    return ResolvedLlmProviderModelResponse(**resolved.to_dict())


def _resolved_execution_policy_to_response(
    resolved: ResolvedLlmExecutionPolicy,
) -> ResolvedLlmExecutionPolicyResponse:
    return ResolvedLlmExecutionPolicyResponse(**resolved.to_dict())


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

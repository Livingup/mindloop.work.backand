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
from agent_runtime.llm_api_protocol import SUPPORTED_LLM_API_FORMATS
from agent_runtime.llm_model_registry.llm_model_metadata import LlmModelMetadata
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    ResolvedLlmProviderModel,
)
from agent_runtime.llm_model_registry.llm_provider_definition import (
    LlmApiKey,
    LlmProviderDefinition,
    LlmProviderModelBinding,
    LlmRetryPolicy,
)
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmModelMetadataSaveRequest,
    LlmModelRegistryGateway,
    LlmProviderSaveRequest,
)


# ── 请求 / 响应 Pydantic 模型 ───────────────────────────────────


class LlmModelMetadataBody(BaseModel):
    """模型元数据保存请求体。

    model_id 来自 URL 路径，body 只提交模型能力字段。
    """

    name: str
    context_window: int
    max_output_tokens: int
    compression_threshold: int | None = None
    supports_tool_calling: bool = False
    supports_structured_output: bool = False
    supported_input_modalities: list[str] = Field(
        default_factory=lambda: [INPUT_MODALITY_TEXT]
    )


class LlmModelMetadataResponse(BaseModel):
    """模型元数据响应体。"""

    model_id: str
    name: str
    context_window: int
    max_output_tokens: int
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

    name: str | None = None
    api_base_url: str
    custom_headers: dict[str, str] = Field(default_factory=dict)
    api_keys: list[LlmApiKeyBody] = Field(default_factory=list)
    retry: LlmRetryPolicyBody = Field(default_factory=LlmRetryPolicyBody)
    models: list[LlmProviderModelBindingBody] = Field(default_factory=list)


class LlmProviderResponse(BaseModel):
    """服务商定义响应体。"""

    provider_id: str
    name: str | None = None
    api_base_url: str
    custom_headers: dict[str, str] = Field(default_factory=dict)
    api_keys: list[dict[str, Any]] = Field(default_factory=list)
    retry: dict[str, Any]
    models: list[dict[str, Any]] = Field(default_factory=list)


class ResolvedLlmProviderModelResponse(BaseModel):
    """解析后的服务商模型调用配置响应体。"""

    provider_id: str
    binding_id: str
    model_id: str
    model_name: str
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


# ── 路由工厂 ─────────────────────────────────────────────────────


def create_llm_model_registry_router(
    gateway: LlmModelRegistryGateway,
) -> APIRouter:
    """根据网关实例创建 LLM 注册 REST API 路由。

    Args:
        gateway: LLM 注册模块北向网关实例。

    Returns:
        FastAPI APIRouter，挂载时建议使用 prefix="/api/llm-registry"。
    """
    router = APIRouter()

    @router.get("/metadata", response_model=LlmRegistryMetadataResponse)
    def get_registry_metadata() -> LlmRegistryMetadataResponse:
        return LlmRegistryMetadataResponse(
            api_formats=list(SUPPORTED_LLM_API_FORMATS),
            input_modalities=sorted(SUPPORTED_INPUT_MODALITIES),
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
        return [_provider_to_response(item) for item in gateway.list_providers()]

    @router.get("/providers/{provider_id}", response_model=LlmProviderResponse)
    def get_provider(provider_id: str) -> LlmProviderResponse:
        """根据 provider_id 获取服务商定义。"""
        try:
            provider = gateway.get_provider(provider_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"LLM 服务商不存在: {provider_id}")
        return _provider_to_response(provider)

    @router.put("/providers/{provider_id}", response_model=LlmProviderResponse)
    def save_provider(
        provider_id: str,
        body: LlmProviderBody,
    ) -> LlmProviderResponse:
        """新增或覆盖保存 LLM 服务商定义。"""
        try:
            provider = gateway.save_provider(_body_to_provider_request(provider_id, body))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _provider_to_response(provider)

    @router.delete("/providers/{provider_id}", status_code=204)
    def delete_provider(provider_id: str) -> None:
        """删除 LLM 服务商定义。不存在时静默返回 204。"""
        gateway.delete_provider(provider_id)

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


def _provider_to_response(provider: LlmProviderDefinition) -> LlmProviderResponse:
    """将服务商定义领域实体转换为 REST 响应模型。"""
    return LlmProviderResponse(**provider.to_dict())


def _resolved_to_response(
    resolved: ResolvedLlmProviderModel,
) -> ResolvedLlmProviderModelResponse:
    """将解析后的服务商模型调用配置转换为 REST 响应模型。"""
    return ResolvedLlmProviderModelResponse(**resolved.to_dict())


def _body_to_model_request(
    model_id: str,
    body: LlmModelMetadataBody,
) -> LlmModelMetadataSaveRequest:
    """将 REST 模型元数据保存请求体转换为北向保存请求。"""
    return LlmModelMetadataSaveRequest(
        model_id=model_id,
        name=body.name,
        context_window=body.context_window,
        max_output_tokens=body.max_output_tokens,
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
        name=body.name,
        api_base_url=body.api_base_url,
        custom_headers=dict(body.custom_headers),
        api_keys=tuple(_body_to_api_key(item) for item in body.api_keys),
        retry=_body_to_retry_policy(body.retry),
        models=tuple(_body_to_binding(item) for item in body.models),
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

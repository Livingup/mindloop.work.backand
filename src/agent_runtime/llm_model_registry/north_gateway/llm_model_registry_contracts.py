# -*- coding: utf-8 -*-
"""LLM 服务商与模型注册北向网关契约。

该模块定义 LLM 注册服务对外暴露的请求模型、结果模型和服务接口。
所有调用方（程序内部、REST API、CLI、后续 Agent 执行引擎）均依赖此契约。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

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


@dataclass(frozen=True, slots=True)
class LlmModelMetadataSaveRequest:
    """保存模型元数据请求。

    调用方提交完整模型型号能力数据，由领域实体校验 model_id 文件名安全、token 容量字段和输入模态数组后，
    持久化为用户目录 YAML 文件。
    """

    # 模型型号唯一标识，同时也是 models/{model_id}.yaml 文件名主名。
    model_id: str

    # 模型展示名。
    name: str

    # 模型最大上下文窗口 token 数。
    context_window: int

    # 模型单次响应最大输出 token 数。
    max_output_tokens: int

    # 会话压缩触发阈值。
    compression_threshold: int | None = None

    # 是否支持工具调用。
    supports_tool_calling: bool = False

    # 是否支持结构化输出。
    supports_structured_output: bool = False

    # 支持的输入模态列表。
    supported_input_modalities: tuple[str, ...] = ("text",)

    def to_metadata(self) -> LlmModelMetadata:
        """转换为 LlmModelMetadata 聚合根。"""
        return LlmModelMetadata(
            model_id=self.model_id,
            name=self.name,
            context_window=self.context_window,
            max_output_tokens=self.max_output_tokens,
            compression_threshold=self.compression_threshold,
            supports_tool_calling=self.supports_tool_calling,
            supports_structured_output=self.supports_structured_output,
            supported_input_modalities=self.supported_input_modalities,
        )

    @classmethod
    def from_metadata(cls, metadata: LlmModelMetadata) -> LlmModelMetadataSaveRequest:
        """从 LlmModelMetadata 聚合根创建保存请求。"""
        return cls(
            model_id=metadata.model_id,
            name=metadata.name,
            context_window=metadata.context_window,
            max_output_tokens=metadata.max_output_tokens,
            compression_threshold=metadata.compression_threshold,
            supports_tool_calling=metadata.supports_tool_calling,
            supports_structured_output=metadata.supports_structured_output,
            supported_input_modalities=metadata.supported_input_modalities,
        )


@dataclass(frozen=True, slots=True)
class LlmProviderSaveRequest:
    """保存 LLM 服务商定义请求。

    调用方提交完整服务商配置，由领域实体校验 provider_id、模型接入项 ID、api_format、JSON 自定义请求字段
    和 API Key 等结构后，再由领域服务校验所有 model_id 引用均已存在。
    """

    # 服务商唯一标识，同时也是 providers/{provider_id}.yaml 文件名主名。
    provider_id: str

    # 人类可读服务商名称。
    name: str | None = None

    # 服务商默认 API base URL。
    api_base_url: str = ""

    # 服务商级默认请求头。
    custom_headers: dict[str, str] = field(default_factory=dict)

    # 服务商级默认 API Keys。
    api_keys: tuple[LlmApiKey, ...] = field(default_factory=tuple)

    # 服务商重试策略。
    retry: LlmRetryPolicy = field(default_factory=LlmRetryPolicy)

    # 服务商模型接入项列表。
    models: tuple[LlmProviderModelBinding, ...] = field(default_factory=tuple)

    def to_provider(self) -> LlmProviderDefinition:
        """转换为 LlmProviderDefinition 聚合根。"""
        return LlmProviderDefinition(
            provider_id=self.provider_id,
            name=self.name,
            api_base_url=self.api_base_url,
            custom_headers=self.custom_headers,
            api_keys=self.api_keys,
            retry=self.retry,
            models=self.models,
        )

    @classmethod
    def from_provider(cls, provider: LlmProviderDefinition) -> LlmProviderSaveRequest:
        """从 LlmProviderDefinition 聚合根创建保存请求。"""
        return cls(
            provider_id=provider.provider_id,
            name=provider.name,
            api_base_url=provider.api_base_url,
            custom_headers=dict(provider.custom_headers),
            api_keys=provider.api_keys,
            retry=provider.retry,
            models=provider.models,
        )


class LlmModelRegistryGateway(Protocol):
    """LLM 服务商与模型注册模块北向网关接口。

    CLI、REST、后续 Agent 执行引擎和 LLM 统一调用客户端都应通过该接口访问用户自定义注册数据，
    不直接依赖领域服务或 YAML 仓储实现。
    """

    # ── 模型元数据管理 ──────────────────────────────────────

    def list_model_metadata(self) -> list[LlmModelMetadata]:
        """列出所有模型型号元数据。"""
        ...

    def get_model_metadata(self, model_id: str) -> LlmModelMetadata:
        """根据 model_id 获取模型元数据。未找到时抛出 KeyError。"""
        ...

    def save_model_metadata(
        self,
        request: LlmModelMetadataSaveRequest,
    ) -> LlmModelMetadata:
        """新增或覆盖保存模型元数据。"""
        ...

    def delete_model_metadata(self, model_id: str) -> None:
        """删除模型元数据。仍被服务商引用时抛出 ValueError。"""
        ...

    # ── 服务商定义管理 ──────────────────────────────────────

    def list_providers(self) -> list[LlmProviderDefinition]:
        """列出所有 LLM 服务商定义。"""
        ...

    def get_provider(self, provider_id: str) -> LlmProviderDefinition:
        """根据 provider_id 获取服务商定义。未找到时抛出 KeyError。"""
        ...

    def save_provider(self, request: LlmProviderSaveRequest) -> LlmProviderDefinition:
        """新增或覆盖保存 LLM 服务商定义。"""
        ...

    def delete_provider(self, provider_id: str) -> None:
        """删除服务商定义。不存在时静默跳过。"""
        ...

    # ── 服务商模型接入项查询与解析 ────────────────────────────

    def list_provider_model_bindings(
        self,
        provider_id: str,
    ) -> list[LlmProviderModelBinding]:
        """列出指定服务商下所有模型接入项。"""
        ...

    def resolve_provider_model(
        self,
        provider_id: str,
        binding_id: str,
    ) -> ResolvedLlmProviderModel:
        """解析指定服务商模型接入项，返回合并后的调用配置。"""
        ...

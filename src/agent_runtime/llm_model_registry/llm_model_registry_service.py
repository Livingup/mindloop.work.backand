# -*- coding: utf-8 -*-
"""LLM 服务商与模型注册领域服务。

该服务无状态，协调注册仓储完成模型元数据、服务商定义、服务商模型接入项解析等业务用例。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from agent_runtime.llm_model_registry.llm_model_metadata import LlmModelMetadata
from agent_runtime.llm_model_registry.llm_model_registry_repository import (
    LlmModelRegistryRepository,
)
from agent_runtime.llm_model_registry.llm_provider_definition import (
    LlmApiKey,
    LlmProviderDefinition,
    LlmProviderModelBinding,
    LlmRetryPolicy,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedLlmProviderModel:
    """解析后的服务商模型调用配置。

    该值对象面向后续 Agent 执行引擎和 LLM 统一调用客户端：它把服务商级默认配置、模型接入项覆盖配置
    和模型型号元数据合并成一次 LLM 请求所需的稳定配置快照。
    """

    # 服务商 ID，例如 sub2api_codex。
    provider_id: str

    # 服务商模型接入项 ID，例如 gemini_flash_via_sub2api。
    binding_id: str

    # 关联的模型元数据 ID，例如 gemini-3-flash。
    model_id: str

    # 模型展示名，来自模型元数据。
    model_name: str

    # 服务商请求时实际使用的模型名。
    model_alias: str

    # 服务商模型调用协议格式。
    api_format: str

    # 已应用模型专属覆盖后的 API base URL。
    api_base_url: str

    # 已应用模型专属覆盖后的 API Keys。
    api_keys: tuple[LlmApiKey, ...]

    # 已合并服务商级和模型级后的请求头，模型级同名 header 覆盖服务商级 header。
    custom_headers: dict[str, str]

    # 服务商重试策略。
    retry: LlmRetryPolicy

    # 自定义 tool_choice JSON 字符串。
    tool_choice: str | None

    # 自定义 thinking JSON 字符串。
    thinking: str | None

    # 自定义请求参数 JSON 字符串。
    custom_request_params: str | None

    # 模型最大上下文窗口 token 数。
    context_window: int

    # 模型最大输出 token 数。
    max_output_tokens: int

    # 模型压缩阈值。
    compression_threshold: int | None

    # 是否支持工具调用。
    supports_tool_calling: bool

    # 是否支持结构化输出。
    supports_structured_output: bool

    # 支持的输入模态。
    supported_input_modalities: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """转换为对 CLI/REST 友好的字典结构。"""
        return {
            "provider_id": self.provider_id,
            "binding_id": self.binding_id,
            "model_id": self.model_id,
            "model_name": self.model_name,
            "model_alias": self.model_alias,
            "api_format": self.api_format,
            "api_base_url": self.api_base_url,
            "api_keys": [item.to_dict() for item in self.api_keys],
            "custom_headers": dict(self.custom_headers),
            "retry": self.retry.to_dict(),
            "tool_choice": self.tool_choice,
            "thinking": self.thinking,
            "custom_request_params": self.custom_request_params,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "compression_threshold": self.compression_threshold,
            "supports_tool_calling": self.supports_tool_calling,
            "supports_structured_output": self.supports_structured_output,
            "supported_input_modalities": list(self.supported_input_modalities),
        }


class LlmModelRegistryService:
    """LLM 服务商与模型注册领域服务。

    该服务承担用户自定义 LLM 注册数据的业务编排：
    - 模型元数据独立维护，描述模型型号本身能力；
    - 服务商定义引用模型元数据，不复制模型能力字段；
    - 保存服务商时校验每个 model_id 已存在，防止后续调用解析失败；
    - 删除模型元数据时阻止删除仍被服务商引用的模型；
    - 解析服务商模型接入项时应用 base URL、API Keys、headers 等覆盖规则。
    """

    def __init__(self, repository: LlmModelRegistryRepository) -> None:
        # LLM 注册仓储。该仓储负责用户自定义模型元数据和服务商 YAML 文件的持久化与查询，
        # 是后续 Agent 执行引擎选择 LLM 服务商、模型和协议参数的数据基础。
        self._repository = repository

    # ── 模型元数据管理 ──────────────────────────────────────

    def list_model_metadata(self) -> list[LlmModelMetadata]:
        """列出所有模型型号元数据。"""
        return self._repository.find_all_model_metadata()

    def get_model_metadata(self, model_id: str) -> LlmModelMetadata:
        """按 model_id 获取模型元数据；不存在时抛出 KeyError。"""
        metadata = self._repository.find_model_metadata_by_id(model_id)
        if metadata is None:
            raise KeyError(f"LLM 模型元数据不存在: {model_id}")
        return metadata

    def save_model_metadata(self, metadata: LlmModelMetadata) -> LlmModelMetadata:
        """新增或覆盖保存模型元数据。"""
        self._repository.save_model_metadata(metadata)
        logger.info("保存 LLM 模型元数据: %s", metadata.model_id)
        return metadata

    def delete_model_metadata(self, model_id: str) -> None:
        """删除模型元数据。

        如果已有服务商模型接入项引用该模型，则拒绝删除，避免服务商定义留下悬空 model_id。
        """
        referenced_by = [
            provider.provider_id
            for provider in self._repository.find_all_providers()
            if any(binding.model_id == model_id for binding in provider.models)
        ]
        if referenced_by:
            raise ValueError(
                f"LLM 模型元数据 '{model_id}' 正被服务商引用，不能删除: "
                + ", ".join(referenced_by)
            )
        if self._repository.delete_model_metadata(model_id):
            logger.info("删除 LLM 模型元数据: %s", model_id)

    # ── 服务商定义管理 ──────────────────────────────────────

    def list_providers(self) -> list[LlmProviderDefinition]:
        """列出所有 LLM 服务商定义。"""
        return self._repository.find_all_providers()

    def get_provider(self, provider_id: str) -> LlmProviderDefinition:
        """按 provider_id 获取服务商定义；不存在时抛出 KeyError。"""
        provider = self._repository.find_provider_by_id(provider_id)
        if provider is None:
            raise KeyError(f"LLM 服务商不存在: {provider_id}")
        return provider

    def save_provider(self, provider: LlmProviderDefinition) -> LlmProviderDefinition:
        """新增或覆盖保存 LLM 服务商定义。

        保存前校验服务商下每个模型接入项引用的 model_id 都已存在，保证服务商配置可被解析。
        """
        missing_model_ids = sorted({
            binding.model_id
            for binding in provider.models
            if not self._repository.contains_model_metadata(binding.model_id)
        })
        if missing_model_ids:
            raise ValueError(
                "LLM 服务商引用了不存在的模型元数据: "
                + ", ".join(missing_model_ids)
            )
        self._repository.save_provider(provider)
        logger.info("保存 LLM 服务商定义: %s", provider.provider_id)
        return provider

    def delete_provider(self, provider_id: str) -> None:
        """删除服务商定义；不存在时静默跳过，便于幂等清理。"""
        if self._repository.delete_provider(provider_id):
            logger.info("删除 LLM 服务商定义: %s", provider_id)

    # ── 服务商模型接入项查询与解析 ────────────────────────────

    def list_provider_model_bindings(
        self,
        provider_id: str,
    ) -> list[LlmProviderModelBinding]:
        """列出指定服务商下所有模型接入项。"""
        provider = self.get_provider(provider_id)
        return list(provider.models)

    def resolve_provider_model(
        self,
        provider_id: str,
        binding_id: str,
    ) -> ResolvedLlmProviderModel:
        """解析指定服务商模型接入项。

        覆盖规则：
        - binding.api_base_url 非空时覆盖 provider.api_base_url；
        - binding.api_keys 非空时覆盖 provider.api_keys；
        - custom_headers 先取服务商级，再用模型接入项同名 header 覆盖；
        - 模型能力字段来自 LlmModelMetadata，不从服务商定义复制。
        """
        provider = self.get_provider(provider_id)
        binding = next((item for item in provider.models if item.id == binding_id), None)
        if binding is None:
            raise KeyError(f"LLM 服务商模型接入项不存在: {provider_id}/{binding_id}")
        metadata = self.get_model_metadata(binding.model_id)
        merged_headers = dict(provider.custom_headers)
        merged_headers.update(binding.custom_headers)
        return ResolvedLlmProviderModel(
            provider_id=provider.provider_id,
            binding_id=binding.id,
            model_id=metadata.model_id,
            model_name=metadata.name,
            model_alias=binding.model_alias,
            api_format=binding.api_format,
            api_base_url=binding.api_base_url or provider.api_base_url,
            api_keys=binding.api_keys or provider.api_keys,
            custom_headers=merged_headers,
            retry=provider.retry,
            tool_choice=binding.tool_choice,
            thinking=binding.thinking,
            custom_request_params=binding.custom_request_params,
            context_window=metadata.context_window,
            max_output_tokens=metadata.max_output_tokens,
            compression_threshold=metadata.compression_threshold,
            supports_tool_calling=metadata.supports_tool_calling,
            supports_structured_output=metadata.supports_structured_output,
            supported_input_modalities=metadata.supported_input_modalities,
        )
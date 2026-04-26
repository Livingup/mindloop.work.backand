# -*- coding: utf-8 -*-
"""LLM 模型型号元数据实体。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_runtime.llm_model_registry.llm_registry_validation import (
    validate_modalities,
    validate_optional_text,
    validate_positive_int,
    validate_registry_id,
 )


@dataclass(frozen=True, slots=True)
class LlmModelMetadata:
    """LLM 模型型号元数据聚合根。

    模型元数据描述“模型型号本身具备什么能力”，不绑定任何服务商、API Key 或实际请求地址。
    例如 gemini-3.1-pro 可以声明上下文窗口、最大输出 token、是否支持工具调用、是否支持结构化输出、
    以及支持 text/image 等输入模态。具体请求时的 thinking 参数由服务商模型接入项维护。
    """

    # 模型型号唯一标识，同时作为 ~/.spread-ai/llm-model-registry/models/{model_id}.yaml 的文件名主名。
    model_id: str

    # 模型展示名。默认通常与 model_id 一致，用于管理后台、CLI 和后续 Agent 选择界面展示。
    name: str

    # 模型最大上下文窗口 token 数，用于 Agent 执行引擎判断历史消息是否需要压缩或裁剪。
    context_window: int

    # 模型单次响应最大输出 token 数，用于 LLM 请求参数上限控制。
    max_output_tokens: int

    # 会话压缩触发阈值。为空时表示该模型不声明专属阈值，由调用方使用系统默认策略。
    compression_threshold: int | None = None

    # 是否支持工具调用。后续 LLM 统一调用客户端会据此决定是否允许注入 tools schema。
    supports_tool_calling: bool = False

    # 是否支持结构化输出。用于决定是否可启用 JSON schema / response_format 等结构化响应参数。
    supports_structured_output: bool = False

    # 支持的输入模态列表，例如 text、image、audio、video、pdf。保持开放枚举，便于未来扩展新模态。
    supported_input_modalities: tuple[str, ...] = field(default_factory=lambda: ("text",))

    def __post_init__(self) -> None:
        """实体创建时校验模型元数据的核心业务规则。"""
        validate_registry_id(self.model_id, "model_id")
        validate_optional_text(self.name, "name")
        if not self.name.strip():
            raise ValueError("name 不能为空。")
        validate_positive_int(self.context_window, "context_window")
        validate_positive_int(self.max_output_tokens, "max_output_tokens")
        if self.compression_threshold is not None:
            validate_positive_int(self.compression_threshold, "compression_threshold")
            if self.compression_threshold > self.context_window:
                raise ValueError("compression_threshold 不能大于 context_window。")
        object.__setattr__(
            self,
            "supported_input_modalities",
            validate_modalities(self.supported_input_modalities),
        )

    def to_dict(self) -> dict[str, Any]:
        """将模型元数据转换为 YAML 友好的字典结构。"""
        data: dict[str, Any] = {
            "model_id": self.model_id,
            "name": self.name,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "supports_tool_calling": self.supports_tool_calling,
            "supports_structured_output": self.supports_structured_output,
            "supported_input_modalities": list(self.supported_input_modalities),
        }
        if self.compression_threshold is not None:
            data["compression_threshold"] = self.compression_threshold
        return data

    @classmethod
    def from_dict(cls, model_id: str, data: dict[str, Any]) -> LlmModelMetadata:
        """从 YAML 字典创建模型元数据实体。"""
        return cls(
            model_id=str(data.get("model_id") or model_id),
            name=str(data.get("name") or model_id),
            context_window=int(data.get("context_window", 0)),
            max_output_tokens=int(data.get("max_output_tokens", 0)),
            compression_threshold=(
                int(data["compression_threshold"])
                if data.get("compression_threshold") is not None else None
            ),
            supports_tool_calling=bool(data.get("supports_tool_calling", False)),
            supports_structured_output=bool(data.get("supports_structured_output", False)),
            supported_input_modalities=tuple(
                str(item) for item in data.get("supported_input_modalities", ["text"])
            ),
        )
# -*- coding: utf-8 -*-
"""LLM 服务商定义与模型接入项实体。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_runtime.llm_model_registry.llm_registry_validation import (
    validate_api_format,
    validate_json_string,
    validate_non_negative_int,
    validate_optional_text,
    validate_registry_id,
    validate_unique_registry_ids,
)


@dataclass(frozen=True, slots=True)
class LlmApiKey:
    """LLM API Key 配置值对象。

    API Key 可以出现在服务商默认凭证列表中，也可以出现在某个模型接入项的专属凭证列表中。
    key 字段支持 ConfigRegistry 或调用方在写入前展开环境变量后的真实值，也支持用户在 YAML 中保留
    ${ENV_NAME} 形式，后续 LLM 调用客户端可按自己的运行时策略再次处理。
    """

    # API Key 字符串或环境变量占位符字符串。
    key: str

    # 可选备注，便于用户区分多 key 轮换来源。
    name: str | None = None

    def __post_init__(self) -> None:
        """校验 API Key 配置必须有可用 key 字段。"""
        if not self.key or not self.key.strip():
            raise ValueError("api_keys.key 不能为空。")
        validate_optional_text(self.name, "api_keys.name")

    def to_dict(self) -> dict[str, Any]:
        """转换为 YAML 友好的字典结构。"""
        data: dict[str, Any] = {"key": self.key}
        if self.name is not None:
            data["name"] = self.name
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LlmApiKey:
        """从 YAML 字典创建 API Key 值对象。"""
        return cls(key=str(data.get("key", "")), name=data.get("name"))


@dataclass(frozen=True, slots=True)
class LlmRetryPolicy:
    """LLM 服务商请求重试策略。

    该策略描述后续 LLM 统一调用客户端在同一个服务商下轮换 API Key 和处理临时失败时可使用的默认重试规则。
    本模块只负责保存和校验策略，不执行真实 HTTP 请求。
    """

    # 单个 API Key 最多重试次数。多个 key 轮换时调用方可按此上限逐个尝试。
    max_retries_per_key: int = 3

    # 首次重试等待秒数，允许为 0 表示立即重试。
    initial_delay_seconds: float = 1.0

    # 指数退避最大等待秒数。
    max_delay_seconds: float = 30.0

    # 建议触发重试的 HTTP 状态码列表。
    retryable_status_codes: tuple[int, ...] = (429, 500, 502, 503, 504)

    def __post_init__(self) -> None:
        """校验重试策略不能出现负数和重复状态码。"""
        validate_non_negative_int(self.max_retries_per_key, "retry.max_retries_per_key")
        if self.initial_delay_seconds < 0:
            raise ValueError("retry.initial_delay_seconds 必须是非负数。")
        if self.max_delay_seconds < 0:
            raise ValueError("retry.max_delay_seconds 必须是非负数。")
        if self.max_delay_seconds and self.initial_delay_seconds > self.max_delay_seconds:
            raise ValueError("retry.initial_delay_seconds 不能大于 retry.max_delay_seconds。")
        status_codes: list[int] = []
        seen: set[int] = set()
        for code in self.retryable_status_codes:
            code_int = int(code)
            if code_int < 100 or code_int > 599:
                raise ValueError(f"retry.retryable_status_codes 包含非法 HTTP 状态码: {code_int}")
            if code_int in seen:
                raise ValueError(f"retry.retryable_status_codes 存在重复状态码: {code_int}")
            seen.add(code_int)
            status_codes.append(code_int)
        object.__setattr__(self, "retryable_status_codes", tuple(status_codes))

    def to_dict(self) -> dict[str, Any]:
        """转换为 YAML 友好的字典结构。"""
        return {
            "max_retries_per_key": self.max_retries_per_key,
            "initial_delay_seconds": self.initial_delay_seconds,
            "max_delay_seconds": self.max_delay_seconds,
            "retryable_status_codes": list(self.retryable_status_codes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> LlmRetryPolicy:
        """从 YAML 字典创建重试策略；缺省时使用安全默认值。"""
        data = data or {}
        return cls(
            max_retries_per_key=int(data.get("max_retries_per_key", 3)),
            initial_delay_seconds=float(data.get("initial_delay_seconds", 1.0)),
            max_delay_seconds=float(data.get("max_delay_seconds", 30.0)),
            retryable_status_codes=tuple(
                int(item) for item in data.get(
                    "retryable_status_codes", [429, 500, 502, 503, 504]
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class LlmProviderModelBinding:
    """服务商下的模型接入项。

    该实体描述“某个服务商如何请求某个模型型号”：它通过 model_id 关联模型元数据，使用 model_alias 表示
    服务商请求体中的真实模型名，并保存 api_format、thinking、tool_choice、自定义请求参数、请求头、
    模型专属 base URL 和模型专属 API Keys 等服务商调用细节。
    """

    # 接入项唯一标识，在同一 provider 内唯一；用于 Agent 后续选择具体服务商模型配置。
    id: str

    # 关联的模型元数据 ID，对应 llm-model-registry/models/{model_id}.yaml。
    model_id: str

    # 服务商请求时实际使用的模型名。例如同一个模型元数据在不同中转服务里可能叫不同别名。
    model_alias: str

    # 服务商模型调用协议格式，例如 anthropic_messages、openai_responses、openai_chat_completions、gemini_generate_content、zhipu_chat_completions。
    api_format: str

    # 自定义 tool_choice JSON 字符串。模块只校验 JSON 合法性，不理解协议语义。
    tool_choice: str | None = None

    # 自定义 thinking JSON 字符串。模块只校验 JSON 合法性，不理解协议语义。
    thinking: str | None = None

    # 自定义请求参数 JSON 字符串。后续调用客户端可合并到真实 LLM 请求体中。
    custom_request_params: str | None = None

    # 模型接入项专属 API base URL；为空时使用服务商默认 api_base_url。
    api_base_url: str | None = None

    # 模型接入项专属请求头；会覆盖或补充服务商级请求头。
    custom_headers: dict[str, str] = field(default_factory=dict)

    # 模型接入项专属 API Keys；非空时覆盖服务商默认 api_keys。
    api_keys: tuple[LlmApiKey, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """实体创建时校验服务商模型接入项业务规则。"""
        validate_registry_id(self.id, "provider_model.id")
        validate_registry_id(self.model_id, "provider_model.model_id")
        if not self.model_alias or not self.model_alias.strip():
            raise ValueError("provider_model.model_alias 不能为空。")
        validate_api_format(self.api_format)
        validate_json_string(self.tool_choice, "tool_choice")
        validate_json_string(self.thinking, "thinking")
        validate_json_string(self.custom_request_params, "custom_request_params")
        validate_optional_text(self.api_base_url, "provider_model.api_base_url")
        object.__setattr__(
            self,
            "custom_headers",
            _normalize_headers(self.custom_headers),
        )
        object.__setattr__(self, "api_keys", tuple(self.api_keys))

    def to_dict(self) -> dict[str, Any]:
        """转换为 YAML 友好的字典结构。"""
        data: dict[str, Any] = {
            "id": self.id,
            "model_id": self.model_id,
            "model_alias": self.model_alias,
            "api_format": self.api_format,
        }
        if self.tool_choice is not None:
            data["tool_choice"] = self.tool_choice
        if self.thinking is not None:
            data["thinking"] = self.thinking
        if self.custom_request_params is not None:
            data["custom_request_params"] = self.custom_request_params
        if self.api_base_url is not None:
            data["api_base_url"] = self.api_base_url
        if self.custom_headers:
            data["custom_headers"] = dict(self.custom_headers)
        if self.api_keys:
            data["api_keys"] = [item.to_dict() for item in self.api_keys]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LlmProviderModelBinding:
        """从 YAML 字典创建服务商模型接入项。"""
        return cls(
            id=str(data.get("id", "")),
            model_id=str(data.get("model_id", "")),
            model_alias=str(data.get("model_alias", "")),
            api_format=str(data.get("api_format", "")),
            tool_choice=data.get("tool_choice"),
            thinking=data.get("thinking"),
            custom_request_params=data.get("custom_request_params"),
            api_base_url=data.get("api_base_url"),
            custom_headers={str(k): str(v) for k, v in (data.get("custom_headers") or {}).items()},
            api_keys=tuple(
                LlmApiKey.from_dict(item)
                for item in data.get("api_keys", []) or []
                if isinstance(item, dict)
            ),
        )


@dataclass(frozen=True, slots=True)
class LlmProviderDefinition:
    """LLM 服务商定义聚合根。

    服务商定义描述一个用户可选择的 LLM 供应方或中转服务，例如 sub2api_codex。服务商级配置提供默认
    api_base_url、默认请求头、默认 API Keys 和重试策略；models 列表描述该服务商可调用哪些模型，
    以及每个模型接入项的协议格式、请求参数和覆盖配置。
    """

    # 服务商唯一标识，同时作为 ~/.spread-ai/llm-model-registry/providers/{provider_id}.yaml 的文件名主名。
    provider_id: str

    # 服务商自定义分组，用于管理界面按用户定义的供应方类别聚合展示。
    group: str = "default"

    # 服务商默认 API base URL。单个模型接入项未配置专属 URL 时使用此地址。
    api_base_url: str = ""

    # 服务商级默认请求头。模型接入项 custom_headers 可在解析时覆盖同名 header。
    custom_headers: dict[str, str] = field(default_factory=dict)

    # 服务商级默认 API Keys。模型接入项配置 api_keys 时会覆盖此列表。
    api_keys: tuple[LlmApiKey, ...] = field(default_factory=tuple)

    # 服务商请求重试策略，供后续 LLM 调用客户端执行 key 轮换和重试时使用。
    retry: LlmRetryPolicy = field(default_factory=LlmRetryPolicy)

    # 服务商支持的模型接入项列表。同一 provider 内接入项 id 必须唯一。
    models: tuple[LlmProviderModelBinding, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """实体创建时校验服务商定义核心业务规则。"""
        validate_registry_id(self.provider_id, "provider_id")
        validate_optional_text(self.group, "group")
        object.__setattr__(self, "group", self.group.strip())
        if not self.api_base_url or not self.api_base_url.strip():
            raise ValueError("api_base_url 不能为空。")
        object.__setattr__(self, "custom_headers", _normalize_headers(self.custom_headers))
        object.__setattr__(self, "api_keys", tuple(self.api_keys))
        object.__setattr__(self, "models", tuple(self.models))
        validate_unique_registry_ids((item.id for item in self.models), "provider_model.id")

    def to_dict(self) -> dict[str, Any]:
        """将服务商定义转换为 YAML 友好的字典结构。"""
        data: dict[str, Any] = {
            "provider_id": self.provider_id,
            "group": self.group,
            "api_base_url": self.api_base_url,
            "models": [item.to_dict() for item in self.models],
            "retry": self.retry.to_dict(),
        }
        if self.custom_headers:
            data["custom_headers"] = dict(self.custom_headers)
        if self.api_keys:
            data["api_keys"] = [item.to_dict() for item in self.api_keys]
        return data

    @classmethod
    def from_dict(cls, provider_id: str, data: dict[str, Any]) -> LlmProviderDefinition:
        """从 YAML 字典创建服务商定义。"""
        return cls(
            provider_id=str(data.get("provider_id") or provider_id),
            group=str(data.get("group") or "default"),
            api_base_url=str(data.get("api_base_url", "")),
            custom_headers={str(k): str(v) for k, v in (data.get("custom_headers") or {}).items()},
            api_keys=tuple(
                LlmApiKey.from_dict(item)
                for item in data.get("api_keys", []) or []
                if isinstance(item, dict)
            ),
            retry=LlmRetryPolicy.from_dict(data.get("retry")),
            models=tuple(
                LlmProviderModelBinding.from_dict(item)
                for item in data.get("models", []) or []
                if isinstance(item, dict)
            ),
        )


def _normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    """校验并规范化自定义请求头字典。"""
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        header_name = str(key).strip()
        if not header_name:
            raise ValueError("custom_headers 不能包含空请求头名称。")
        normalized[header_name] = str(value)
    return normalized

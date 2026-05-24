# -*- coding: utf-8 -*-
"""LLM SDK 平台与 API 协议格式全局定义。

本文件是项目内所有 LLM SDK 平台标识和具体 API 协议格式标识的唯一来源。

概念区分：
- SDK 平台（sdk_platform）：表示调用哪家 SDK / 客户端家族，如 openai、anthropic、gemini、zhipu。
- API 协议格式（api_format）：表示该 SDK 平台下具体采用哪种请求/响应协议，
  如 openai_responses、openai_chat_completions。
"""
from __future__ import annotations

from typing import TypeAlias


# SDK 平台标识。Tool Schema 生成、SDK 客户端选择等场景使用该维度。
LlmSdkPlatform: TypeAlias = str

SDK_PLATFORM_ANTHROPIC: LlmSdkPlatform = "anthropic"
SDK_PLATFORM_OPENAI: LlmSdkPlatform = "openai"
SDK_PLATFORM_GEMINI: LlmSdkPlatform = "gemini"
SDK_PLATFORM_ZHIPU: LlmSdkPlatform = "zhipu"

SUPPORTED_LLM_SDK_PLATFORMS: tuple[LlmSdkPlatform, ...] = (
    SDK_PLATFORM_ANTHROPIC,
    SDK_PLATFORM_OPENAI,
    SDK_PLATFORM_GEMINI,
    SDK_PLATFORM_ZHIPU,
)


# API 协议格式标识。conversation_message 的协议适配器、LLM 请求渲染/响应解析、
# llm_model_registry.provider.models[*].api_format 使用该维度。
LlmApiFormat: TypeAlias = str

API_FORMAT_ANTHROPIC_MESSAGES: LlmApiFormat = "anthropic_messages"
API_FORMAT_OPENAI_RESPONSES: LlmApiFormat = "openai_responses"
API_FORMAT_CODEX_RESPONSES: LlmApiFormat = "codex_responses"
API_FORMAT_OPENAI_CHAT_COMPLETIONS: LlmApiFormat = "openai_chat_completions"
API_FORMAT_GEMINI_GENERATE_CONTENT: LlmApiFormat = "gemini_generate_content"
API_FORMAT_ZHIPU_CHAT_COMPLETIONS: LlmApiFormat = "zhipu_chat_completions"

SUPPORTED_LLM_API_FORMATS: tuple[LlmApiFormat, ...] = (
    API_FORMAT_ANTHROPIC_MESSAGES,
    API_FORMAT_OPENAI_RESPONSES,
    API_FORMAT_CODEX_RESPONSES,
    API_FORMAT_OPENAI_CHAT_COMPLETIONS,
    API_FORMAT_GEMINI_GENERATE_CONTENT,
    API_FORMAT_ZHIPU_CHAT_COMPLETIONS,
)

API_FORMAT_TO_SDK_PLATFORM: dict[LlmApiFormat, LlmSdkPlatform] = {
    API_FORMAT_ANTHROPIC_MESSAGES: SDK_PLATFORM_ANTHROPIC,
    API_FORMAT_OPENAI_RESPONSES: SDK_PLATFORM_OPENAI,
    API_FORMAT_CODEX_RESPONSES: SDK_PLATFORM_OPENAI,
    API_FORMAT_OPENAI_CHAT_COMPLETIONS: SDK_PLATFORM_OPENAI,
    API_FORMAT_GEMINI_GENERATE_CONTENT: SDK_PLATFORM_GEMINI,
    API_FORMAT_ZHIPU_CHAT_COMPLETIONS: SDK_PLATFORM_ZHIPU,
}


def validate_llm_sdk_platform(
    value: str,
    field_name: str = "sdk_platform",
) -> LlmSdkPlatform:
    """校验 LLM SDK 平台标识并返回规范值。

    SDK 平台用于选择 SDK 客户端或生成工具 schema。它不是具体请求协议；例如 OpenAI 平台下
    仍可能存在 openai_responses 与 openai_chat_completions 两种 api_format。
    """
    normalized = _normalize_required_identifier(value, field_name)
    if normalized not in SUPPORTED_LLM_SDK_PLATFORMS:
        raise ValueError(
            f"{field_name} '{normalized}' 不受支持，允许值："
            + ", ".join(SUPPORTED_LLM_SDK_PLATFORMS)
        )
    return normalized


def validate_llm_api_format(
    value: str,
    field_name: str = "api_format",
) -> LlmApiFormat:
    """校验 LLM API 协议格式标识并返回规范值。

    api_format 表示具体 SDK 平台下的请求/响应协议格式，例如 openai_responses 或
    openai_chat_completions。该字段使用全局枚举，避免各模块各自维护字符串常量。
    """
    normalized = _normalize_required_identifier(value, field_name)
    if normalized not in SUPPORTED_LLM_API_FORMATS:
        raise ValueError(
            f"{field_name} '{normalized}' 不受支持，允许值："
            + ", ".join(SUPPORTED_LLM_API_FORMATS)
        )
    return normalized


def get_sdk_platform_for_api_format(api_format: str) -> LlmSdkPlatform:
    """根据具体 API 协议格式获取所属 SDK 平台。"""
    normalized = validate_llm_api_format(api_format)
    return API_FORMAT_TO_SDK_PLATFORM[normalized]


def _normalize_required_identifier(value: str, field_name: str) -> str:
    """校验必填标识字段并返回去空白后的字符串。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空。")
    if value != value.strip():
        raise ValueError(f"{field_name} 不能包含首尾空白字符。")
    return value

"""工具 Schema 生成器接口。

各 LLM SDK 平台（OpenAI / Anthropic / Gemini / Zhipu）在各自模块中实现此接口，
使用各自 SDK 从函数签名（类型注解 + Pydantic 模型）解析生成该平台所需格式的 JSON Schema。
"""
from __future__ import annotations

from typing import Any, Protocol

from agent_runtime.tool.local_tool_definition import LocalToolDefinition


class ToolSchemaGenerator(Protocol):
    """各 LLM 平台的工具 Schema 生成器接口。

    各平台 adapter 在自己的模块中实现此接口（如 OpenAISchemaGenerator），
    注册到 ToolSchemaCache 中。
    """

    @property
    def platform(self) -> str:
        """SDK 平台标识（如 "openai"、"anthropic"、"gemini"、"zhipu"）。"""
        ...

    def generate(self, tool_def: LocalToolDefinition) -> dict[str, Any]:
        """根据工具定义生成该平台格式的 JSON Schema。

        Args:
            tool_def: 本地工具定义实体。

        Returns:
            该平台格式的 JSON Schema 字典。
        """
        ...

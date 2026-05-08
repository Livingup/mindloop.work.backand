"""OpenAI Chat Completions 协议工具 Schema 生成器。

使用 openai SDK 的 pydantic_function_tool 从 Pydantic 模型生成
OpenAI Chat Completions Function Calling 格式的工具定义（嵌套 function 结构）。
"""
from __future__ import annotations

from typing import Any

from openai import pydantic_function_tool

from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_CHAT_COMPLETIONS
from agent_runtime.tool.infrastructure._pydantic_model_builder import (
    build_pydantic_model,
)
from agent_runtime.tool.local_tool_definition import LocalToolDefinition


class OpenAIChatCompletionsSchemaGenerator:
    """OpenAI Chat Completions 协议工具 Schema 生成器。

    实现 ToolSchemaGenerator 协议。
    使用 openai SDK 的 pydantic_function_tool 生成 Chat Completions Function Calling 格式（嵌套 function 结构）。
    """

    @property
    def platform(self) -> str:
        """SDK 平台标识。"""
        return API_FORMAT_OPENAI_CHAT_COMPLETIONS

    def generate(self, tool_def: LocalToolDefinition) -> dict[str, Any]:
        """生成 OpenAI Chat Completions 格式的工具 Schema。

        委托给 openai SDK 的 pydantic_function_tool，
        由 SDK 负责 Pydantic 模型→JSON Schema 的转换和格式包装。

        Args:
            tool_def: 本地工具定义实体。

        Returns:
            Chat Completions Function Calling 格式字典（ChatCompletionToolParam）。
        """
        model = build_pydantic_model(tool_def.fn)
        return pydantic_function_tool(
            model,
            name=tool_def.tool_name,
            description=tool_def.description,
        )

"""智谱 GLM Chat Completions 协议工具 Schema 生成器。

智谱 GLM 使用 OpenAI 兼容的 Chat Completions 协议，
工具 Schema 格式与 OpenAI Chat Completions 完全一致（嵌套 function 结构）。
复用 openai SDK 的 pydantic_function_tool 生成。
"""
from __future__ import annotations

from typing import Any

from openai import pydantic_function_tool

from agent_runtime.llm_api_protocol import API_FORMAT_ZHIPU_CHAT_COMPLETIONS
from agent_runtime.tool.infrastructure._pydantic_model_builder import (
    build_pydantic_model,
)
from agent_runtime.tool.local_tool_definition import LocalToolDefinition


class ZhipuChatCompletionsSchemaGenerator:
    """智谱 GLM Chat Completions 协议工具 Schema 生成器。

    实现 ToolSchemaGenerator 协议。
    智谱 GLM 兼容 OpenAI Chat Completions 工具格式，
    使用 openai SDK 的 pydantic_function_tool 生成嵌套 function 结构。
    """

    @property
    def platform(self) -> str:
        """协议格式标识。"""
        return API_FORMAT_ZHIPU_CHAT_COMPLETIONS

    def generate(self, tool_def: LocalToolDefinition) -> dict[str, Any]:
        """生成智谱 GLM Chat Completions 格式的工具 Schema。

        Args:
            tool_def: 本地工具定义实体。

        Returns:
            Chat Completions Function Calling 格式字典（嵌套 function 结构）。
        """
        model = build_pydantic_model(tool_def.fn)
        return pydantic_function_tool(
            model,
            name=tool_def.tool_name,
            description=tool_def.description,
        )

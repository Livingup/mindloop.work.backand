"""OpenAI Responses API 协议工具 Schema 生成器。

使用 openai SDK 的 pydantic_function_tool 生成参数 JSON Schema，
再将 Chat Completions 嵌套结构展平为 Responses API 扁平结构。
strict 设为 false，保留 SDK 原始生成的 JSON Schema，不做后处理。
"""
from __future__ import annotations

from typing import Any

from openai import pydantic_function_tool

from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_RESPONSES
from agent_runtime.tool.infrastructure._pydantic_model_builder import (
    build_pydantic_model,
)
from agent_runtime.tool.local_tool_definition import LocalToolDefinition


class OpenAIResponsesSchemaGenerator:
    """OpenAI Responses API 协议工具 Schema 生成器。

    实现 ToolSchemaGenerator 协议。
    使用 openai SDK 的 pydantic_function_tool 生成参数 Schema，
    然后将嵌套 function 结构展平为 Responses API 要求的扁平结构。
    strict 设为 false，直接使用 SDK 原始输出，不做额外后处理。
    """

    @property
    def platform(self) -> str:
        """协议格式标识。"""
        return API_FORMAT_OPENAI_RESPONSES

    def generate(self, tool_def: LocalToolDefinition) -> dict[str, Any]:
        """生成 OpenAI Responses API 格式的工具 Schema。

        先用 SDK 的 pydantic_function_tool 生成 Chat Completions 嵌套格式，
        再将 function 子对象展平到顶层。

        Args:
            tool_def: 本地工具定义实体。

        Returns:
            Responses API 扁平结构字典：
            {"type": "function", "name": ..., "description": ..., "parameters": ..., "strict": false}
        """
        model = build_pydantic_model(tool_def.fn)
        chat_schema = pydantic_function_tool(
            model,
            name=tool_def.tool_name,
            description=tool_def.description,
        )
        # pydantic_function_tool 返回:
        # {"type": "function", "function": {"name": ..., "description": ..., "parameters": ..., "strict": ...}}
        # Responses API 需要扁平结构，将 function 子对象展平到顶层。
        fn = chat_schema.get("function", {})
        return {
            "type": "function",
            "name": fn.get("name", tool_def.tool_name),
            "description": fn.get("description", tool_def.description),
            "parameters": fn.get("parameters", {}),
            "strict": False,
        }

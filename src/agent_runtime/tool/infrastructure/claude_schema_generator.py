"""Claude (Anthropic) 平台工具 Schema 生成器。

使用 Pydantic model_json_schema 生成 input_schema，
符合 Anthropic SDK ToolParam 所要求的标准 JSON Schema 格式。
"""
from __future__ import annotations

from typing import Any

from agent_runtime.tool.infrastructure._pydantic_model_builder import (
    build_pydantic_model,
)
from agent_runtime.tool.local_tool_definition import LocalToolDefinition


class ClaudeSchemaGenerator:
    """Claude (Anthropic) 平台工具 Schema 生成器。

    实现 ToolSchemaGenerator 协议。
    使用 Pydantic model_json_schema 生成 Anthropic Tool Use 格式。
    Anthropic SDK 的 ToolParam 要求 input_schema 为标准 JSON Schema。
    """

    @property
    def platform(self) -> str:
        """平台标识。"""
        return "claude"

    def generate(self, tool_def: LocalToolDefinition) -> dict[str, Any]:
        """生成 Claude 格式的工具 Schema。

        从函数签名提取 Pydantic 模型，使用 model_json_schema() 生成
        标准 JSON Schema 作为 input_schema。

        Args:
            tool_def: 本地工具定义实体。

        Returns:
            Anthropic ToolParam 格式字典。
        """
        model = build_pydantic_model(tool_def.fn)
        return {
            "name": tool_def.tool_name,
            "description": tool_def.description,
            "input_schema": model.model_json_schema(),
        }

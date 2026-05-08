"""Claude (Anthropic) 平台工具 Schema 生成器。

使用 anthropic SDK 的 beta_tool 从 Pydantic 模型生成
Anthropic Tool Use 格式的工具 Schema，确保由 SDK 控制序列化格式。
"""
from __future__ import annotations

from typing import Any

from anthropic import beta_tool

from agent_runtime.llm_api_protocol import API_FORMAT_ANTHROPIC_MESSAGES
from agent_runtime.tool.infrastructure._pydantic_model_builder import (
    build_pydantic_model,
)
from agent_runtime.tool.local_tool_definition import LocalToolDefinition


def _noop(**_: Any) -> None:
    """beta_tool 要求的占位函数，实际不执行。"""


class ClaudeSchemaGenerator:
    """Claude (Anthropic) 平台工具 Schema 生成器。

    实现 ToolSchemaGenerator 协议。
    使用 anthropic SDK 的 beta_tool 将 Pydantic 模型转换为
    Anthropic Tool Use 格式，由 SDK 负责 input_schema 的序列化。
    """

    @property
    def platform(self) -> str:
        """协议格式标识。"""
        return API_FORMAT_ANTHROPIC_MESSAGES

    def generate(self, tool_def: LocalToolDefinition) -> dict[str, Any]:
        """生成 Claude 格式的工具 Schema。

        使用 anthropic SDK 的 beta_tool 包装 Pydantic 模型，
        再通过 to_dict() 获取 SDK 序列化后的工具定义字典。

        Args:
            tool_def: 本地工具定义实体。

        Returns:
            Anthropic ToolParam 格式字典。
        """
        model = build_pydantic_model(tool_def.fn)
        tool = beta_tool(
            _noop,
            name=tool_def.tool_name,
            description=tool_def.description,
            input_schema=model,
        )
        return tool.to_dict()

"""Gemini (Google) 平台工具 Schema 生成器。

使用 google-genai SDK 的 types.FunctionDeclaration 构建工具声明，
由 SDK 处理 Schema 格式（types.Schema）。
"""
from __future__ import annotations

from typing import Any

from google.genai import types

from agent_runtime.llm_api_protocol import SDK_PLATFORM_GEMINI
from agent_runtime.tool.infrastructure._pydantic_model_builder import (
    build_pydantic_model,
)
from agent_runtime.tool.local_tool_definition import LocalToolDefinition


class GeminiSchemaGenerator:
    """Gemini (Google) 平台工具 Schema 生成器。

    实现 ToolSchemaGenerator 协议。
    使用 google-genai SDK 的 types.FunctionDeclaration 和 types.Schema
    构建 Gemini 格式的工具声明。
    """

    @property
    def platform(self) -> str:
        """SDK 平台标识。"""
        return SDK_PLATFORM_GEMINI

    def generate(self, tool_def: LocalToolDefinition) -> dict[str, Any]:
        """生成 Gemini 格式的工具 Schema。

        从函数签名提取 Pydantic 模型，使用 model_json_schema() 生成
        标准 JSON Schema，再通过 google-genai SDK 的 types.Schema
        转换为 Gemini 平台格式。

        Args:
            tool_def: 本地工具定义实体。

        Returns:
            Gemini FunctionDeclaration 格式字典。
        """
        model = build_pydantic_model(tool_def.fn)
        json_schema = _inline_json_schema_refs(model.model_json_schema())

        # 使用 google-genai SDK 的 types.Schema 将标准 JSON Schema
        # 转换为 Gemini 平台所需格式。
        gemini_parameters = types.Schema.model_validate(json_schema)

        declaration = types.FunctionDeclaration(
            name=tool_def.tool_name,
            description=tool_def.description,
            parameters=gemini_parameters,
        )
        # 返回可序列化的字典形式。
        return declaration.model_dump(exclude_none=True)


def _inline_json_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """内联 Pydantic JSON Schema 中的 $defs/$ref，适配 google-genai types.Schema。"""
    defs = schema.get("$defs") or {}

    def resolve(value: Any) -> Any:
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if not isinstance(value, dict):
            return value
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/")
            target = defs.get(name, {})
            merged = {**target, **{k: v for k, v in value.items() if k != "$ref"}}
            return resolve(merged)
        return {
            key: resolve(item)
            for key, item in value.items()
            if key not in {"$defs"}
        }

    resolved = resolve(schema)
    if not isinstance(resolved, dict):
        raise TypeError("工具 JSON Schema 解析结果必须是字典。")
    return resolved

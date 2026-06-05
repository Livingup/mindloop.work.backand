"""LLM 可见工具 Schema 净化工具。

工具 schema 的参数名只应由 JSON Schema 的 properties key 和 required 列表表达。
Pydantic / 各 Provider SDK 自动生成的 title 注解会让模型看到与真实字段名不同的
schema 标题（例如 reason 字段生成 title=Reason），从而误用标题作为工具调用参数名。
"""
from __future__ import annotations

from typing import Any


LLM_TOOL_SCHEMA_STRIPPED_KEYS = frozenset({"title"})


def sanitize_llm_tool_schema(value: Any) -> Any:
    """递归移除 LLM 工具调用场景中会干扰参数契约的 schema 注解。

    保留 properties、required、description、type、enum、items 等真实约束，只删除
    Pydantic / SDK 自动生成且不参与工具参数寻址的 title 字段，避免 LLM 把 schema 标题
    当作实际参数名输出。返回新对象，不修改传入 schema。
    """
    if isinstance(value, list):
        return [sanitize_llm_tool_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: sanitize_llm_tool_schema(item)
        for key, item in value.items()
        if key not in LLM_TOOL_SCHEMA_STRIPPED_KEYS
    }

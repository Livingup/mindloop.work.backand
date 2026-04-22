"""本地工具内存仓储实现。

工具从代码扫描加载，无持久化需求，全部存储在内存中。
"""
from __future__ import annotations

import logging
from typing import Any

from agent_runtime.tool.local_tool_definition import LocalToolDefinition

logger = logging.getLogger(__name__)


class InMemoryToolRepository:
    """本地工具内存仓储。

    实现 LocalToolRepository 接口，工具定义全部存储在内存字典中。
    """

    def __init__(self) -> None:
        # 工具名称 → 工具定义的映射。
        self._tools: dict[str, LocalToolDefinition] = {}

    def add(self, tool_def: LocalToolDefinition) -> None:
        """添加工具定义。tool_name 已存在时抛出 ValueError。"""
        if tool_def.tool_name in self._tools:
            raise ValueError(f"工具已注册: {tool_def.tool_name}")
        self._tools[tool_def.tool_name] = tool_def
        logger.info("注册本地工具: %s", tool_def.tool_name)

    def get_by_name(self, tool_name: str) -> LocalToolDefinition:
        """根据工具名称获取工具定义。未找到时抛出 KeyError。"""
        if tool_name not in self._tools:
            raise KeyError(f"本地工具不存在: {tool_name}")
        return self._tools[tool_name]

    def list_all(self) -> list[LocalToolDefinition]:
        """列出所有已注册的工具定义。"""
        return list(self._tools.values())

    def contains(self, tool_name: str) -> bool:
        """判断工具是否已注册。"""
        return tool_name in self._tools

    @property
    def count(self) -> int:
        """已注册工具总数。"""
        return len(self._tools)

    def clear(self) -> None:
        """清空所有已注册的工具（仅用于测试）。"""
        self._tools.clear()

"""本地工具领域服务。

无状态，协调仓储和 Schema 缓存，提供四个业务用例：
1. 列出所有工具（支持 public 过滤、group 分组）
2. 按名称查询工具
3. 执行工具
4. 获取工具 wiki 列表（按 group 分组）
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from agent_runtime.tool.local_tool_definition import LocalToolDefinition
from agent_runtime.tool.local_tool_repository import LocalToolRepository
from agent_runtime.tool.tool_schema_cache import ToolSchemaCache

logger = logging.getLogger(__name__)


class LocalToolService:
    """本地工具领域服务。

    协调工具仓储和 Schema 缓存，对外提供工具查询、执行和文档能力。
    """

    def __init__(
        self,
        repository: LocalToolRepository,
        schema_cache: ToolSchemaCache,
    ) -> None:
        self._repo = repository
        self._schema_cache = schema_cache

    # ── 业务用例 1：列出所有工具 ──────────────────────────────────────

    def list_tools(
        self,
        *,
        public_only: bool = True,
        group_by: bool = False,
    ) -> list[dict[str, Any]] | dict[str, list[dict[str, Any]]]:
        """列出所有工具。

        Args:
            public_only: 是否只返回公开工具，默认 True。
            group_by: 是否按 group 分组返回，默认 False。

        Returns:
            不分组时返回工具信息列表；
            分组时返回 {group: [工具信息列表]} 字典。
        """
        tools = self._repo.list_all()
        if public_only:
            tools = [t for t in tools if t.public]

        tool_items = [self._build_tool_summary(t) for t in tools]

        if not group_by:
            return tool_items

        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in tool_items:
            group = item["group"]
            grouped.setdefault(group, []).append(item)
        return grouped

    # ── 业务用例 2：按名称查询工具 ────────────────────────────────────

    def get_tool(self, tool_name: str) -> dict[str, Any]:
        """按名称查询工具完整信息（不受 public 限制）。

        Args:
            tool_name: 工具名称。

        Returns:
            工具完整信息字典（名称、描述、wiki、分组、各平台 JSON Schema）。

        Raises:
            KeyError: 工具不存在。
        """
        tool_def = self._repo.get_by_name(tool_name)
        return self._build_tool_detail(tool_def)

    # ── 业务用例 3：执行工具 ──────────────────────────────────────────

    async def execute_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
    ) -> Any:
        """执行指定工具。

        支持同步和异步工具函数：同步函数直接调用，异步函数自动 await。

        Args:
            tool_name: 工具名称。
            params: 传给工具函数的参数字典。

        Returns:
            工具函数的返回值。

        Raises:
            KeyError: 工具不存在。
            TypeError: 参数不匹配。
        """
        tool_def = self._repo.get_by_name(tool_name)
        logger.info("执行本地工具: %s, 参数: %s", tool_name, params)
        result = tool_def.fn(**params)
        if inspect.isawaitable(result):
            return await result
        return result

    # ── 业务用例 4：获取工具 wiki 列表 ────────────────────────────────

    def get_wiki_list(self) -> dict[str, list[dict[str, Any]]]:
        """获取工具 wiki 列表，按 group 分组。

        默认只返回 public=True 的工具。
        每个工具返回工具名称、wiki、group 和各平台 JSON Schema。

        Returns:
            {group: [工具 wiki 信息列表]} 字典。
        """
        tools = self._repo.list_all()
        tools = [t for t in tools if t.public]

        grouped: dict[str, list[dict[str, Any]]] = {}
        for t in tools:
            item = {
                "tool_name": t.tool_name,
                "wiki": t.wiki,
                "group": t.group,
                "schemas": self._schema_cache.get_all_schemas(t),
            }
            grouped.setdefault(t.group, []).append(item)
        return grouped

    # ── 内部方法 ──────────────────────────────────────────────────────

    def _build_tool_summary(self, tool_def: LocalToolDefinition) -> dict[str, Any]:
        """构建工具摘要信息（列表用）。不包含 schemas，schemas 只在详情中返回。"""
        return {
            "tool_name": tool_def.tool_name,
            "description": tool_def.description,
            "group": tool_def.group,
            "wiki": tool_def.wiki,
        }

    def _build_tool_detail(self, tool_def: LocalToolDefinition) -> dict[str, Any]:
        """构建工具完整信息（详情用）。"""
        return {
            "tool_name": tool_def.tool_name,
            "description": tool_def.description,
            "group": tool_def.group,
            "public": tool_def.public,
            "wiki": tool_def.wiki,
            "schemas": self._schema_cache.get_all_schemas(tool_def),
        }

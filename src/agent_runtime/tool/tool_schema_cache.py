"""工具 Schema 缓存管理。

按 (tool_name, platform) 维度缓存已生成的 JSON Schema，
同一工具同一平台只生成一次。
"""
from __future__ import annotations

import logging
from typing import Any

from agent_runtime.tool.local_tool_definition import LocalToolDefinition
from agent_runtime.tool.tool_schema_generator import ToolSchemaGenerator

logger = logging.getLogger(__name__)


class ToolSchemaCache:
    """工具 Schema 缓存管理器。

    管理各平台 ToolSchemaGenerator 的注册，
    按 (tool_name, platform) 维度缓存 JSON Schema。
    """

    def __init__(self) -> None:
        # 平台标识 → Schema 生成器。
        self._generators: dict[str, ToolSchemaGenerator] = {}
        # (tool_name, platform) → 已生成的 JSON Schema。
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}

    def register_generator(self, generator: ToolSchemaGenerator) -> None:
        """注册一个平台的 Schema 生成器。

        同一平台重复注册会覆盖旧的生成器。
        """
        self._generators[generator.platform] = generator
        logger.info("注册 Schema 生成器: %s", generator.platform)

    def get_schema(
        self,
        tool_def: LocalToolDefinition,
        platform: str,
    ) -> dict[str, Any]:
        """获取指定工具在指定平台的 JSON Schema。

        首次调用时自动生成并缓存，后续直接返回缓存。

        Args:
            tool_def: 工具定义实体。
            platform: 平台标识（如 "openai"）。

        Returns:
            该平台格式的 JSON Schema 字典。

        Raises:
            KeyError: 指定平台的生成器未注册。
        """
        cache_key = (tool_def.tool_name, platform)
        if cache_key not in self._cache:
            if platform not in self._generators:
                raise KeyError(f"未注册平台 Schema 生成器: {platform}")
            schema = self._generators[platform].generate(tool_def)
            self._cache[cache_key] = schema
            logger.debug(
                "生成并缓存 Schema: tool=%s, platform=%s",
                tool_def.tool_name,
                platform,
            )
        return self._cache[cache_key]

    def get_all_schemas(
        self,
        tool_def: LocalToolDefinition,
    ) -> dict[str, dict[str, Any]]:
        """获取指定工具在所有已注册平台的 JSON Schema。

        Args:
            tool_def: 工具定义实体。

        Returns:
            平台标识 → JSON Schema 字典的映射。
        """
        return {
            platform: self.get_schema(tool_def, platform)
            for platform in self._generators
        }

    @property
    def registered_platforms(self) -> list[str]:
        """已注册的平台标识列表。"""
        return list(self._generators.keys())

    def invalidate(self, tool_name: str) -> None:
        """清除指定工具的所有平台缓存。"""
        keys_to_remove = [k for k in self._cache if k[0] == tool_name]
        for key in keys_to_remove:
            del self._cache[key]

    def clear(self) -> None:
        """清空所有缓存（仅用于测试）。"""
        self._cache.clear()

"""ToolSchemaCache 单元测试。"""
from __future__ import annotations

from typing import Any

import pytest

from agent_runtime.tool.local_tool_definition import LocalToolDefinition
from agent_runtime.tool.tool_schema_cache import ToolSchemaCache


def _dummy_fn() -> str:
    return "ok"


def _make_tool(name: str = "test_tool") -> LocalToolDefinition:
    return LocalToolDefinition(tool_name=name, description="测试", fn=_dummy_fn)


class FakeGenerator:
    """假的 Schema 生成器，用于测试。"""

    def __init__(self, platform: str, schema: dict[str, Any] | None = None) -> None:
        self._platform = platform
        self._schema = schema or {"type": "function", "platform": platform}
        self.call_count = 0

    @property
    def platform(self) -> str:
        return self._platform

    def generate(self, tool_def: LocalToolDefinition) -> dict[str, Any]:
        self.call_count += 1
        return {**self._schema, "name": tool_def.tool_name}


@pytest.fixture()
def cache() -> ToolSchemaCache:
    return ToolSchemaCache()


# ── 注册生成器 ───────────────────────────────────────────────────────


def test_register_generator(cache: ToolSchemaCache) -> None:
    """注册生成器后平台列表包含该平台。"""
    gen = FakeGenerator("openai")
    cache.register_generator(gen)
    assert "openai" in cache.registered_platforms


def test_register_multiple_generators(cache: ToolSchemaCache) -> None:
    """注册多个生成器。"""
    cache.register_generator(FakeGenerator("openai"))
    cache.register_generator(FakeGenerator("claude"))
    assert set(cache.registered_platforms) == {"openai", "claude"}


# ── 获取 Schema ──────────────────────────────────────────────────────


def test_get_schema(cache: ToolSchemaCache) -> None:
    """获取 Schema 调用生成器并返回结果。"""
    gen = FakeGenerator("openai")
    cache.register_generator(gen)
    tool_def = _make_tool("search")
    schema = cache.get_schema(tool_def, "openai")
    assert schema["name"] == "search"
    assert schema["platform"] == "openai"
    assert gen.call_count == 1


def test_get_schema_cached(cache: ToolSchemaCache) -> None:
    """重复获取同一工具同一平台的 Schema 使用缓存。"""
    gen = FakeGenerator("openai")
    cache.register_generator(gen)
    tool_def = _make_tool("search")
    cache.get_schema(tool_def, "openai")
    cache.get_schema(tool_def, "openai")
    assert gen.call_count == 1


def test_get_schema_unregistered_platform_raises(cache: ToolSchemaCache) -> None:
    """查询未注册平台的 Schema 抛出 KeyError。"""
    with pytest.raises(KeyError, match="未注册平台 Schema 生成器: unknown"):
        cache.get_schema(_make_tool(), "unknown")


# ── 获取所有平台 Schema ──────────────────────────────────────────────


def test_get_all_schemas(cache: ToolSchemaCache) -> None:
    """获取工具在所有平台的 Schema。"""
    cache.register_generator(FakeGenerator("openai"))
    cache.register_generator(FakeGenerator("claude"))
    tool_def = _make_tool("search")
    schemas = cache.get_all_schemas(tool_def)
    assert set(schemas.keys()) == {"openai", "claude"}
    assert schemas["openai"]["name"] == "search"
    assert schemas["claude"]["name"] == "search"


def test_get_all_schemas_empty(cache: ToolSchemaCache) -> None:
    """无生成器时返回空字典。"""
    schemas = cache.get_all_schemas(_make_tool())
    assert schemas == {}


# ── 缓存失效 ─────────────────────────────────────────────────────────


def test_invalidate(cache: ToolSchemaCache) -> None:
    """invalidate 清除指定工具的缓存，再次获取时重新生成。"""
    gen = FakeGenerator("openai")
    cache.register_generator(gen)
    tool_def = _make_tool("search")
    cache.get_schema(tool_def, "openai")
    assert gen.call_count == 1
    cache.invalidate("search")
    cache.get_schema(tool_def, "openai")
    assert gen.call_count == 2


def test_clear(cache: ToolSchemaCache) -> None:
    """clear 清空全部缓存。"""
    gen = FakeGenerator("openai")
    cache.register_generator(gen)
    cache.get_schema(_make_tool("a"), "openai")
    cache.get_schema(_make_tool("b"), "openai")
    assert gen.call_count == 2
    cache.clear()
    cache.get_schema(_make_tool("a"), "openai")
    assert gen.call_count == 3

"""LocalToolService 领域服务单元测试。"""
from __future__ import annotations

from typing import Any

import pytest

from agent_runtime.tool.infrastructure.in_memory_tool_repository import (
    InMemoryToolRepository,
)
from agent_runtime.tool.local_tool_definition import LocalToolDefinition
from agent_runtime.tool.local_tool_service import LocalToolService
from agent_runtime.tool.tool_schema_cache import ToolSchemaCache


# ── 辅助 ─────────────────────────────────────────────────────────────


def _add(a: int, b: int) -> int:
    return a + b


def _greet(name: str) -> str:
    return f"Hello, {name}!"


def _noop() -> None:
    pass


class FakeGenerator:
    """假的 Schema 生成器。"""

    def __init__(self, platform: str) -> None:
        self._platform = platform

    @property
    def platform(self) -> str:
        return self._platform

    def generate(self, tool_def: LocalToolDefinition) -> dict[str, Any]:
        return {"type": "function", "name": tool_def.tool_name, "platform": self._platform}


def _make_tool(name: str, desc: str = "测试", fn: object = None, **kw: object) -> LocalToolDefinition:
    return LocalToolDefinition(tool_name=name, description=desc, fn=fn or _noop, **kw)


@pytest.fixture()
def service() -> LocalToolService:
    """创建带示例数据的服务实例。"""
    repo = InMemoryToolRepository()
    repo.add(_make_tool("add", "两数相加", _add, group="数学", public=True))
    repo.add(_make_tool("greet", "打招呼", _greet, group="社交", public=True, wiki="打招呼文档"))
    repo.add(_make_tool("noop", "无操作", _noop, group="数学", public=False))

    cache = ToolSchemaCache()
    cache.register_generator(FakeGenerator("openai"))

    return LocalToolService(repository=repo, schema_cache=cache)


# ── 业务用例 1：列出所有工具 ──────────────────────────────────────────


def test_list_tools_public_only(service: LocalToolService) -> None:
    """默认只返回 public=True 的工具。"""
    result = service.list_tools()
    assert isinstance(result, list)
    names = {t["tool_name"] for t in result}
    assert names == {"add", "greet"}


def test_list_tools_include_private(service: LocalToolService) -> None:
    """public_only=False 返回所有工具。"""
    result = service.list_tools(public_only=False)
    assert isinstance(result, list)
    names = {t["tool_name"] for t in result}
    assert names == {"add", "greet", "noop"}


def test_list_tools_group_by(service: LocalToolService) -> None:
    """group_by=True 按分组返回。"""
    result = service.list_tools(group_by=True)
    assert isinstance(result, dict)
    assert "数学" in result
    assert "社交" in result
    math_names = {t["tool_name"] for t in result["数学"]}
    assert math_names == {"add"}


def test_list_tools_has_wiki(service: LocalToolService) -> None:
    """列表结果包含 wiki 字段（不含 schemas，schemas 只在详情中返回）。"""
    result = service.list_tools()
    for item in result:
        assert "wiki" in item
        assert "schemas" not in item


# ── 业务用例 2：按名称查询工具 ────────────────────────────────────────


def test_get_tool(service: LocalToolService) -> None:
    """按名称查询工具返回完整信息。"""
    detail = service.get_tool("greet")
    assert detail["tool_name"] == "greet"
    assert detail["description"] == "打招呼"
    assert detail["wiki"] == "打招呼文档"
    assert detail["group"] == "社交"
    assert detail["public"] is True
    assert "openai" in detail["schemas"]


def test_get_tool_private(service: LocalToolService) -> None:
    """不受 public 限制，可查询私有工具。"""
    detail = service.get_tool("noop")
    assert detail["tool_name"] == "noop"
    assert detail["public"] is False


def test_get_tool_nonexistent_raises(service: LocalToolService) -> None:
    """查询不存在的工具抛出 KeyError。"""
    with pytest.raises(KeyError, match="本地工具不存在"):
        service.get_tool("unknown")


# ── 业务用例 3：执行工具 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_tool(service: LocalToolService) -> None:
    """执行工具函数并获取返回值。"""
    result = await service.execute_tool("add", {"a": 3, "b": 5})
    assert result == 8


@pytest.mark.asyncio
async def test_execute_tool_greet(service: LocalToolService) -> None:
    """执行打招呼工具。"""
    result = await service.execute_tool("greet", {"name": "World"})
    assert result == "Hello, World!"


@pytest.mark.asyncio
async def test_execute_tool_nonexistent_raises(service: LocalToolService) -> None:
    """执行不存在的工具抛出 KeyError。"""
    with pytest.raises(KeyError):
        await service.execute_tool("unknown", {})


@pytest.mark.asyncio
async def test_execute_tool_wrong_params_raises(service: LocalToolService) -> None:
    """参数不匹配时抛出 TypeError。"""
    with pytest.raises(TypeError):
        await service.execute_tool("add", {"x": 1})


# ── 业务用例 4：获取工具 wiki 列表 ────────────────────────────────────


def test_get_wiki_list(service: LocalToolService) -> None:
    """wiki 列表按 group 分组，只返回公开工具。"""
    result = service.get_wiki_list()
    assert isinstance(result, dict)
    # noop 是 private，不应出现
    all_names = {
        item["tool_name"]
        for items in result.values()
        for item in items
    }
    assert "noop" not in all_names
    assert "add" in all_names
    assert "greet" in all_names


def test_get_wiki_list_has_wiki_and_schemas(service: LocalToolService) -> None:
    """wiki 列表每项包含 wiki 和 schemas。"""
    result = service.get_wiki_list()
    for items in result.values():
        for item in items:
            assert "wiki" in item
            assert "schemas" in item
            assert "openai" in item["schemas"]

"""InMemoryToolRepository 内存仓储单元测试。"""
from __future__ import annotations

import pytest

from agent_runtime.tool.infrastructure.in_memory_tool_repository import (
    InMemoryToolRepository,
)
from agent_runtime.tool.local_tool_definition import LocalToolDefinition


# ── 测试用工具函数 ──────────────────────────────────────────────────


def _add(a: int, b: int) -> int:
    return a + b


def _greet(name: str, greeting: str = "Hello") -> str:
    return f"{greeting}, {name}!"


def _noop() -> None:
    pass


# ── fixture ──────────────────────────────────────────────────────────


@pytest.fixture()
def repo() -> InMemoryToolRepository:
    """创建空的内存仓储。"""
    return InMemoryToolRepository()


def _make_tool(
    name: str = "test_tool",
    desc: str = "测试工具",
    fn: object = None,
    **kwargs: object,
) -> LocalToolDefinition:
    """快捷创建工具定义。"""
    return LocalToolDefinition(
        tool_name=name,
        description=desc,
        fn=fn or _noop,
        **kwargs,
    )


# ── 添加 ─────────────────────────────────────────────────────────────


def test_add_and_contains(repo: InMemoryToolRepository) -> None:
    """添加工具后能通过 contains 查到。"""
    repo.add(_make_tool("add", "两数相加", _add))
    assert repo.contains("add")
    assert repo.count == 1


def test_add_duplicate_raises(repo: InMemoryToolRepository) -> None:
    """重复添加同名工具抛出 ValueError。"""
    repo.add(_make_tool("dup"))
    with pytest.raises(ValueError, match="工具已注册: dup"):
        repo.add(_make_tool("dup"))


# ── 查询 ─────────────────────────────────────────────────────────────


def test_get_by_name(repo: InMemoryToolRepository) -> None:
    """根据名称获取工具定义。"""
    repo.add(_make_tool("add", "两数相加", _add))
    td = repo.get_by_name("add")
    assert td.tool_name == "add"
    assert td.description == "两数相加"
    assert td.fn is _add


def test_get_by_name_nonexistent_raises(repo: InMemoryToolRepository) -> None:
    """获取不存在的工具抛出 KeyError。"""
    with pytest.raises(KeyError, match="本地工具不存在: unknown"):
        repo.get_by_name("unknown")


def test_contains_false(repo: InMemoryToolRepository) -> None:
    """工具不存在时返回 False。"""
    assert repo.contains("nonexistent") is False


# ── 列出 ─────────────────────────────────────────────────────────────


def test_list_all_empty(repo: InMemoryToolRepository) -> None:
    """空仓储返回空列表。"""
    assert repo.list_all() == []


def test_list_all_multiple(repo: InMemoryToolRepository) -> None:
    """添加多个工具后全部列出。"""
    repo.add(_make_tool("add", "加法", _add))
    repo.add(_make_tool("greet", "打招呼", _greet))
    repo.add(_make_tool("noop", "无操作", _noop))
    tools = repo.list_all()
    names = {t.tool_name for t in tools}
    assert names == {"add", "greet", "noop"}
    assert repo.count == 3


# ── 清空 ─────────────────────────────────────────────────────────────


def test_clear(repo: InMemoryToolRepository) -> None:
    """清空仓储后所有工具被移除。"""
    repo.add(_make_tool("add", "加法", _add))
    repo.clear()
    assert repo.count == 0
    assert not repo.contains("add")

"""@tool 装饰器单元测试。

注意：由于 @tool 装饰器会自动注册到全局仓储，
这些测试中的装饰函数需要唯一名称以避免注册冲突。
"""
from __future__ import annotations

import pytest

from agent_runtime.tool.tool_decorator import (
    get_tool_description,
    get_tool_group,
    get_tool_name,
    get_tool_public,
    get_tool_wiki,
    is_tool,
    tool,
)


# ── 基础装饰（含全部字段） ─────────────────────────────────────────


@tool(
    name="dec_greet",
    description="打招呼工具",
    group="社交",
    public=False,
    wiki="用于打招呼的工具说明文档。",
)
def _greet(name: str) -> str:
    return f"Hello, {name}!"


def _plain_func() -> None:
    pass


def test_decorator_attaches_name() -> None:
    """装饰器在函数上附加 _tool_name 属性。"""
    assert get_tool_name(_greet) == "dec_greet"


def test_decorator_attaches_description() -> None:
    """装饰器在函数上附加 _tool_description 属性。"""
    assert get_tool_description(_greet) == "打招呼工具"


def test_decorator_attaches_group() -> None:
    """装饰器在函数上附加 _tool_group 属性。"""
    assert get_tool_group(_greet) == "社交"


def test_decorator_attaches_public() -> None:
    """装饰器在函数上附加 _tool_public 属性。"""
    assert get_tool_public(_greet) is False


def test_decorator_attaches_wiki() -> None:
    """装饰器在函数上附加 _tool_wiki 属性。"""
    assert get_tool_wiki(_greet) == "用于打招呼的工具说明文档。"


def test_is_tool_returns_true_for_decorated() -> None:
    """被 @tool 装饰的函数返回 True。"""
    assert is_tool(_greet) is True


def test_is_tool_returns_false_for_plain() -> None:
    """普通函数返回 False。"""
    assert is_tool(_plain_func) is False


def test_is_tool_returns_false_for_non_callable() -> None:
    """非可调用对象返回 False。"""
    assert is_tool("not a function") is False


def test_decorated_function_still_callable() -> None:
    """装饰后函数本身行为不变。"""
    result = _greet("World")
    assert result == "Hello, World!"


def test_get_tool_name_raises_for_plain() -> None:
    """对未装饰函数调用 get_tool_name 抛出 AttributeError。"""
    with pytest.raises(AttributeError):
        get_tool_name(_plain_func)


def test_get_tool_description_raises_for_plain() -> None:
    """对未装饰函数调用 get_tool_description 抛出 AttributeError。"""
    with pytest.raises(AttributeError):
        get_tool_description(_plain_func)


# ── 默认值 ──────────────────────────────────────────────────────────


@tool(name="dec_defaults", description="默认值工具")
def _defaults_tool() -> str:
    return "defaults"


def test_default_group() -> None:
    """不指定 group 时默认为 'default'。"""
    assert get_tool_group(_defaults_tool) == "default"


def test_default_public() -> None:
    """不指定 public 时默认为 True。"""
    assert get_tool_public(_defaults_tool) is True


def test_default_wiki() -> None:
    """不指定 wiki 时默认为 None。"""
    assert get_tool_wiki(_defaults_tool) is None


# ── 多工具互不干扰 ──────────────────────────────────────────────────


@tool(name="dec_tool_a", description="工具 A", group="A组")
def _tool_a() -> str:
    return "a"


@tool(name="dec_tool_b", description="工具 B", group="B组")
def _tool_b() -> str:
    return "b"


def test_multiple_tools_independent_metadata() -> None:
    """多个 @tool 装饰的函数元数据互不影响。"""
    assert get_tool_name(_tool_a) == "dec_tool_a"
    assert get_tool_name(_tool_b) == "dec_tool_b"
    assert get_tool_description(_tool_a) == "工具 A"
    assert get_tool_description(_tool_b) == "工具 B"
    assert get_tool_group(_tool_a) == "A组"
    assert get_tool_group(_tool_b) == "B组"


# ── 自动注册到全局仓储 ──────────────────────────────────────────────


def test_auto_registration() -> None:
    """@tool 装饰器在 import 时自动注册到全局仓储。"""
    from agent_runtime.tool._global_registry import get_global_repository
    repo = get_global_repository()
    assert repo.contains("dec_greet")
    assert repo.contains("dec_defaults")
    assert repo.contains("dec_tool_a")
    assert repo.contains("dec_tool_b")

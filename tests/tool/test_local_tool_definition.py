"""LocalToolDefinition 领域实体单元测试。"""
from __future__ import annotations

import pytest

from agent_runtime.tool.local_tool_definition import LocalToolDefinition


def _dummy_fn() -> str:
    return "ok"


# ── 正常创建 ─────────────────────────────────────────────────────────


def test_create_valid_tool_definition() -> None:
    """正常参数创建成功，默认字段正确。"""
    td = LocalToolDefinition(
        tool_name="search_files",
        description="在项目中搜索文件",
        fn=_dummy_fn,
    )
    assert td.tool_name == "search_files"
    assert td.description == "在项目中搜索文件"
    assert td.fn is _dummy_fn
    assert td.group == "default"
    assert td.public is True
    assert td.wiki is None


def test_create_with_all_fields() -> None:
    """显式指定 group、public、wiki 创建成功。"""
    td = LocalToolDefinition(
        tool_name="advanced_search",
        description="高级搜索",
        fn=_dummy_fn,
        group="文件",
        public=False,
        wiki="高级搜索工具的详细说明。",
    )
    assert td.group == "文件"
    assert td.public is False
    assert td.wiki == "高级搜索工具的详细说明。"


def test_tool_name_allows_hyphen_and_underscore() -> None:
    """工具名称允许连字符和下划线。"""
    td = LocalToolDefinition(
        tool_name="my-tool_v2",
        description="测试工具",
        fn=_dummy_fn,
    )
    assert td.tool_name == "my-tool_v2"


# ── tool_name 校验 ──────────────────────────────────────────────────


def test_empty_tool_name_raises() -> None:
    """工具名称为空时抛出 ValueError。"""
    with pytest.raises(ValueError, match="工具名称不能为空"):
        LocalToolDefinition(tool_name="", description="描述", fn=_dummy_fn)


def test_whitespace_tool_name_raises() -> None:
    """工具名称为纯空白时抛出 ValueError。"""
    with pytest.raises(ValueError, match="工具名称不能为空"):
        LocalToolDefinition(tool_name="   ", description="描述", fn=_dummy_fn)


def test_too_long_tool_name_raises() -> None:
    """工具名称超过最大长度时抛出 ValueError。"""
    long_name = "a" * 81
    with pytest.raises(ValueError, match="工具名称长度不能超过"):
        LocalToolDefinition(tool_name=long_name, description="描述", fn=_dummy_fn)


def test_invalid_chars_in_tool_name_raises() -> None:
    """工具名称含非法字符时抛出 ValueError。"""
    with pytest.raises(ValueError, match="仅允许字母、数字、下划线和连字符"):
        LocalToolDefinition(tool_name="search files", description="描述", fn=_dummy_fn)


def test_tool_name_with_dot_raises() -> None:
    """工具名称含点号时抛出 ValueError。"""
    with pytest.raises(ValueError, match="仅允许字母、数字、下划线和连字符"):
        LocalToolDefinition(tool_name="search.files", description="描述", fn=_dummy_fn)


# ── description 校验 ────────────────────────────────────────────────


def test_empty_description_raises() -> None:
    """工具描述为空时抛出 ValueError。"""
    with pytest.raises(ValueError, match="工具描述不能为空"):
        LocalToolDefinition(tool_name="test", description="", fn=_dummy_fn)


def test_whitespace_description_raises() -> None:
    """工具描述为纯空白时抛出 ValueError。"""
    with pytest.raises(ValueError, match="工具描述不能为空"):
        LocalToolDefinition(tool_name="test", description="  ", fn=_dummy_fn)


# ── fn 校验 ──────────────────────────────────────────────────────────


def test_non_callable_fn_raises() -> None:
    """fn 不可调用时抛出 ValueError。"""
    with pytest.raises(ValueError, match="fn 必须是可调用对象"):
        LocalToolDefinition(tool_name="test", description="描述", fn="not callable")  # type: ignore[arg-type]


# ── group 校验 ───────────────────────────────────────────────────────


def test_empty_group_raises() -> None:
    """工具分组为空时抛出 ValueError。"""
    with pytest.raises(ValueError, match="工具分组不能为空"):
        LocalToolDefinition(tool_name="test", description="描述", fn=_dummy_fn, group="")


def test_whitespace_group_raises() -> None:
    """工具分组为纯空白时抛出 ValueError。"""
    with pytest.raises(ValueError, match="工具分组不能为空"):
        LocalToolDefinition(tool_name="test", description="描述", fn=_dummy_fn, group="  ")

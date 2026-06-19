"""@tool 装饰器 — 声明本地工具函数。

使用方式：

    from agent_runtime.tool import tool

    @tool(name="search_files", description="在项目中搜索文件", group="文件")
    def search_files(query: str, max_results: int = 10) -> str:
        ...

装饰器在函数对象上附加元数据（name, description, group, public, wiki），
不改变函数本身的行为。被装饰的函数在模块导入时自动注册到全局注册表。
"""
from __future__ import annotations

from typing import Any, Callable, TypeVar

from agent_runtime.tool.tool_execution_models import LoopDecision, ToolCategory

F = TypeVar("F", bound=Callable[..., Any])

# ── 装饰器附加在函数上的元数据属性名 ──────────────────────────────
TOOL_NAME_ATTR = "_tool_name"
TOOL_DESCRIPTION_ATTR = "_tool_description"
TOOL_GROUP_ATTR = "_tool_group"
TOOL_PUBLIC_ATTR = "_tool_public"
TOOL_WIKI_ATTR = "_tool_wiki"
TOOL_CATEGORY_ATTR = "_tool_category"
TOOL_ALWAYS_ENABLED_ATTR = "_tool_always_enabled"
TOOL_ALLOWED_LOOP_DECISIONS_ATTR = "_tool_allowed_loop_decisions"
TOOL_EXECUTION_MODE_ATTR = "_tool_execution_mode"
TOOL_BLOCKING_POOL_ATTR = "_tool_blocking_pool"
TOOL_BLOCKING_OPERATION_ATTR = "_tool_blocking_operation"
TOOL_BLOCKING_SLOW_EXECUTION_SECONDS_ATTR = "_tool_blocking_slow_execution_seconds"


def tool(
    *,
    name: str,
    description: str,
    group: str = "default",
    public: bool = True,
    wiki: str | None = None,
    category: ToolCategory = "business",
    always_enabled: bool = False,
    allowed_loop_decisions: tuple[LoopDecision, ...] = ("continue",),
    execution_mode: str = "direct",
    blocking_pool: str | None = None,
    blocking_operation: str | None = None,
    blocking_slow_execution_seconds: float | None = None,
) -> Callable[[F], F]:
    """声明一个本地工具函数的装饰器。

    被装饰的函数在模块导入时自动注册到全局注册表，
    各业务模块开发者无需关心注册过程。

    Args:
        name: 工具唯一名称（如 "search_files"）。
        description: LLM 看到的工具描述（简洁、功能导向）。
        group: 工具分组（如 "文件"、"网络"、"数据库"），默认 "default"。
        public: 是否公开（列出所有工具时是否可见），默认 True。
        wiki: 人类可读的工具介绍文档（用于生成系统工具说明文档），可为 None。
        execution_mode: 工具执行模式，默认 direct；显式 blocking 的同步工具进入 BlockingScheduler。
        blocking_pool: blocking 模式使用的逻辑池名称。
        blocking_operation: blocking 模式使用的稳定操作名。

    Returns:
        装饰后的原始函数（附加了元数据属性）。
    """

    def decorator(fn: F) -> F:
        setattr(fn, TOOL_NAME_ATTR, name)
        setattr(fn, TOOL_DESCRIPTION_ATTR, description)
        setattr(fn, TOOL_GROUP_ATTR, group)
        setattr(fn, TOOL_PUBLIC_ATTR, public)
        setattr(fn, TOOL_WIKI_ATTR, wiki)
        setattr(fn, TOOL_CATEGORY_ATTR, category)
        setattr(fn, TOOL_ALWAYS_ENABLED_ATTR, always_enabled)
        setattr(fn, TOOL_ALLOWED_LOOP_DECISIONS_ATTR, allowed_loop_decisions)
        setattr(fn, TOOL_EXECUTION_MODE_ATTR, execution_mode)
        setattr(fn, TOOL_BLOCKING_POOL_ATTR, blocking_pool)
        setattr(fn, TOOL_BLOCKING_OPERATION_ATTR, blocking_operation)
        setattr(fn, TOOL_BLOCKING_SLOW_EXECUTION_SECONDS_ATTR, blocking_slow_execution_seconds)
        # 延迟导入避免循环依赖，注册到全局注册表。
        from agent_runtime.tool._global_registry import get_global_registry
        get_global_registry().register_function(fn)
        return fn

    return decorator


# ── 元数据读取工具函数 ────────────────────────────────────────────


def is_tool(fn: Any) -> bool:
    """判断一个对象是否被 @tool 装饰过。"""
    return callable(fn) and hasattr(fn, TOOL_NAME_ATTR)


def get_tool_name(fn: Any) -> str:
    """获取 @tool 装饰函数的工具名称。未装饰时抛出 AttributeError。"""
    return getattr(fn, TOOL_NAME_ATTR)


def get_tool_description(fn: Any) -> str:
    """获取 @tool 装饰函数的工具描述。未装饰时抛出 AttributeError。"""
    return getattr(fn, TOOL_DESCRIPTION_ATTR)


def get_tool_group(fn: Any) -> str:
    """获取 @tool 装饰函数的工具分组。未装饰时抛出 AttributeError。"""
    return getattr(fn, TOOL_GROUP_ATTR)


def get_tool_public(fn: Any) -> bool:
    """获取 @tool 装饰函数的公开标志。未装饰时抛出 AttributeError。"""
    return getattr(fn, TOOL_PUBLIC_ATTR)


def get_tool_wiki(fn: Any) -> str | None:
    """获取 @tool 装饰函数的 wiki 文档。未装饰时抛出 AttributeError。"""
    return getattr(fn, TOOL_WIKI_ATTR)


def get_tool_category(fn: Any) -> ToolCategory:
    """获取 @tool 装饰函数的工具类别。未装饰时抛出 AttributeError。"""
    return getattr(fn, TOOL_CATEGORY_ATTR)


def get_tool_always_enabled(fn: Any) -> bool:
    """获取 @tool 装饰函数的默认启用标志。未装饰时抛出 AttributeError。"""
    return getattr(fn, TOOL_ALWAYS_ENABLED_ATTR)


def get_tool_allowed_loop_decisions(fn: Any) -> tuple[LoopDecision, ...]:
    """获取 @tool 装饰函数允许返回的循环决策集合。未装饰时抛出 AttributeError。"""
    return getattr(fn, TOOL_ALLOWED_LOOP_DECISIONS_ATTR)


def get_tool_execution_mode(fn: Any) -> str:
    """获取 @tool 装饰函数执行模式。未装饰时抛出 AttributeError。"""
    return getattr(fn, TOOL_EXECUTION_MODE_ATTR)


def get_tool_blocking_pool(fn: Any) -> str | None:
    """获取 @tool 装饰函数阻塞调度池。未装饰时抛出 AttributeError。"""
    return getattr(fn, TOOL_BLOCKING_POOL_ATTR)


def get_tool_blocking_operation(fn: Any) -> str | None:
    """获取 @tool 装饰函数阻塞操作名。未装饰时抛出 AttributeError。"""
    return getattr(fn, TOOL_BLOCKING_OPERATION_ATTR)


def get_tool_blocking_slow_execution_seconds(fn: Any) -> float | None:
    """获取 @tool 装饰函数阻塞慢执行阈值。未装饰时抛出 AttributeError。"""
    return getattr(fn, TOOL_BLOCKING_SLOW_EXECUTION_SECONDS_ATTR)

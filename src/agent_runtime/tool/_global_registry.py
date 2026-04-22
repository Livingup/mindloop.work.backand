"""全局工具注册表单例。

持有全局 InMemoryToolRepository 实例，供 @tool 装饰器在模块导入时自动注册使用。
程序启动后通过 get_global_repository() 获取全局仓储实例。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 全局仓储实例（延迟初始化）。
_repository: Any = None


def get_global_repository() -> Any:
    """获取全局工具仓储实例（InMemoryToolRepository）。

    延迟初始化，首次调用时创建。返回 Any 避免顶层循环导入。
    """
    global _repository
    if _repository is None:
        from agent_runtime.tool.infrastructure.in_memory_tool_repository import (
            InMemoryToolRepository,
        )
        _repository = InMemoryToolRepository()
    return _repository


def reset_global_repository() -> None:
    """重置全局仓储实例（仅用于测试）。"""
    global _repository
    _repository = None


class _GlobalRegistry:
    """全局注册表包装器。

    @tool 装饰器通过此对象的 register_function 方法自动注册工具。
    封装了从函数元数据提取字段并构建 LocalToolDefinition 的逻辑。
    """

    def register_function(self, fn: Any) -> None:
        """从 @tool 装饰的函数提取元数据并注册到全局仓储。"""
        from agent_runtime.tool.local_tool_definition import LocalToolDefinition

        tool_def = LocalToolDefinition(
            tool_name=getattr(fn, "_tool_name"),
            description=getattr(fn, "_tool_description"),
            fn=fn,
            group=getattr(fn, "_tool_group", "default"),
            public=getattr(fn, "_tool_public", True),
            wiki=getattr(fn, "_tool_wiki", None),
        )
        get_global_repository().add(tool_def)


# 全局注册表单例。
_registry_instance: _GlobalRegistry | None = None


def get_global_registry() -> _GlobalRegistry:
    """获取全局注册表实例。"""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = _GlobalRegistry()
    return _registry_instance

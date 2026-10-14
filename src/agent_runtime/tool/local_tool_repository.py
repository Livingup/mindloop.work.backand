"""本地工具仓储接口。

定义在领域层，基础设施层提供具体实现。
集合语义，方法用业务语言命名。
"""
from __future__ import annotations

from typing import Protocol

from agent_runtime.tool.local_tool_definition import LocalToolDefinition


class LocalToolRepository(Protocol):
    """本地工具仓储接口。

    定义工具定义的存取操作，实现在基础设施层。
    """

    def add(self, tool_def: LocalToolDefinition) -> None:
        """添加工具定义。tool_name 已存在时抛出 ValueError。"""
        ...

    def get_by_name(self, tool_name: str) -> LocalToolDefinition:
        """根据工具名称获取工具定义。未找到时抛出 KeyError。"""
        ...

    def list_all(self) -> list[LocalToolDefinition]:
        """列出所有已注册的工具定义。"""
        ...

    def contains(self, tool_name: str) -> bool:
        """判断工具是否已注册。"""
        ...

    @property
    def count(self) -> int:
        """已注册工具总数。"""
        ...

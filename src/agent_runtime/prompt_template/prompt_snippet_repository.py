"""提示词片段仓储接口（领域层）。

定义片段持久化的读写操作，领域服务依赖此接口，不关心具体存储方式。
"""
from __future__ import annotations

from typing import Protocol

from agent_runtime.prompt_template.prompt_snippet import PromptSnippet


class PromptSnippetRepository(Protocol):
    """提示词片段仓储接口。"""

    def find_by_name(self, name: str) -> PromptSnippet | None:
        """根据唯一描述名查找片段。未找到时返回 None。"""
        ...

    def find_all(self) -> list[PromptSnippet]:
        """返回所有已持久化的片段。"""
        ...

    def save(self, snippet: PromptSnippet) -> None:
        """保存片段。name 已存在时覆写，不存在时新增。"""
        ...

    def delete(self, name: str) -> bool:
        """删除片段。返回是否实际删除了（False 表示不存在）。"""
        ...

    def exists(self, name: str) -> bool:
        """判断指定 name 的片段是否存在。"""
        ...

    def rename(self, old_name: str, new_name: str) -> None:
        """更新持久化标识的 name 映射。"""
        ...

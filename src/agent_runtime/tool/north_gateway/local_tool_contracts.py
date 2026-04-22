"""本地工具北向网关契约 — 对外数据模型（DTO）和服务接口。

所有调用方（程序内部、REST API、CLI）均依赖此契约。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# ── 对外数据模型 ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ToolSummary:
    """工具摘要信息（列表用）。"""

    # 工具唯一名称。
    tool_name: str

    # LLM 看到的工具描述。
    description: str

    # 工具分组。
    group: str

    # 人类可读的 wiki 文档。
    wiki: str | None = None


@dataclass(frozen=True, slots=True)
class ToolDetail:
    """工具完整信息（详情用）。"""

    # 工具唯一名称。
    tool_name: str

    # LLM 看到的工具描述。
    description: str

    # 工具分组。
    group: str

    # 是否公开。
    public: bool

    # 人类可读的 wiki 文档。
    wiki: str | None

    # 各平台 JSON Schema（平台标识 → schema 字典）。
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolWikiItem:
    """工具 wiki 条目（wiki 列表用）。"""

    # 工具唯一名称。
    tool_name: str

    # 人类可读的 wiki 文档。
    wiki: str | None

    # 工具分组。
    group: str

    # 各平台 JSON Schema（平台标识 → schema 字典）。
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolExecuteRequest:
    """工具执行请求。"""

    # 工具名称。
    tool_name: str

    # 传给工具函数的参数字典。
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolExecuteResult:
    """工具执行结果。"""

    # 工具名称。
    tool_name: str

    # 执行结果。
    result: Any


# ── 北向网关接口 ──────────────────────────────────────────────────────


class LocalToolGateway(Protocol):
    """本地工具北向网关接口。

    外部消费方（Agent 定义模块、执行引擎、CLI、REST）通过此契约访问本模块能力。
    """

    def list_tools(
        self,
        *,
        public_only: bool = True,
        group_by: bool = False,
    ) -> list[ToolSummary] | dict[str, list[ToolSummary]]:
        """列出所有工具。

        Args:
            public_only: 是否只返回公开工具，默认 True。
            group_by: 是否按 group 分组返回，默认 False。
        """
        ...

    def get_tool(self, tool_name: str) -> ToolDetail:
        """按名称查询工具完整信息（不受 public 限制）。

        Raises:
            KeyError: 工具不存在。
        """
        ...

    async def execute_tool(self, request: ToolExecuteRequest) -> ToolExecuteResult:
        """执行工具。

        Raises:
            KeyError: 工具不存在。
            TypeError: 参数不匹配。
        """
        ...

    def get_wiki_list(self) -> dict[str, list[ToolWikiItem]]:
        """获取工具 wiki 列表，按 group 分组。默认只返回公开工具。"""
        ...

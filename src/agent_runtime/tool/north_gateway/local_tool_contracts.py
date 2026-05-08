"""本地工具北向网关契约 — 对外数据模型（DTO）和服务接口。

所有调用方（程序内部、REST API、CLI）均依赖此契约。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import Field as PydanticField

from agent_runtime.tool.tool_execution_models import (
    BaseToolRequest,
    LoopDecision,
    ToolCategory,
    ToolResultEnvelope,
)


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

    # 工具类别。
    category: ToolCategory = "business"

    # 是否默认对所有 Agent 启用。
    always_enabled: bool = False

    # 当前工具允许返回的循环控制决策集合。
    allowed_loop_decisions: tuple[LoopDecision, ...] = ("continue",)


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

    # 工具类别。
    category: ToolCategory

    # 是否默认对所有 Agent 启用。
    always_enabled: bool

    # 当前工具允许返回的循环控制决策集合。
    allowed_loop_decisions: tuple[LoopDecision, ...]

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

    # 工具类别。
    category: ToolCategory = "business"

    # 是否默认对所有 Agent 启用。
    always_enabled: bool = False

    # 当前工具允许返回的循环控制决策集合。
    allowed_loop_decisions: tuple[LoopDecision, ...] = ("continue",)

    # 各平台 JSON Schema（平台标识 → schema 字典）。
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)


class ToolExecuteRequest(BaseToolRequest):
    """工具执行请求（网关层调用入口）。

    携带工具名、业务参数，以及 Runtime 调度需要的上下文元数据
    （request_id / task_id / agent_instance_id / trace_id / communication_stream_id）。
    这些上下文字段由 Runtime 调度器（如 AgentToolDispatcher）注入，REST / CLI 入口
    可只填 tool_name + params。

    注意：本模型是网关层执行请求，**不会**进 LLM 工具 schema，因此 Runtime 字段
    在这里声明是安全的，不会污染 LLM 上下文。
    """

    tool_name: str = PydanticField(description="工具名称。")
    params: dict[str, Any] = PydanticField(default_factory=dict, description="传给工具函数的参数字典。")

    request_id: str | None = PydanticField(default=None, description="工具调用请求标识。")
    task_id: str | None = PydanticField(default=None, description="所属 Agent 任务标识。")
    agent_instance_id: str | None = PydanticField(default=None, description="所属 Agent 运行时实例标识。")
    trace_id: str | None = PydanticField(default=None, description="执行链路追踪标识。")
    communication_stream_id: str | None = PydanticField(default=None, description="所属通信流标识。")

    def __init__(
        self,
        tool_name: str | None = None,
        params: dict[str, Any] | None = None,
        **data: Any,
    ) -> None:
        if tool_name is not None:
            data.setdefault("tool_name", tool_name)
        if params is not None:
            data.setdefault("params", params)
        super().__init__(**data)


class ToolExecuteResult(ToolResultEnvelope):
    """工具执行结果。"""

    def __init__(
        self,
        tool_name: str | None = None,
        result: Any = None,
        **data: Any,
    ) -> None:
        if tool_name is not None:
            data.setdefault("tool_name", tool_name)
        if result is not None:
            data.setdefault("output", result)
        super().__init__(**data)

    @property
    def result(self) -> Any:
        """兼容旧调用方的 result 字段，等价于统一协议中的 output。"""
        return self.output


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

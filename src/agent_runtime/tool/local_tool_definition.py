"""本地工具领域实体。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from agent_runtime.tool.tool_execution_models import LoopDecision, ToolCategory

LocalToolExecutionMode = Literal["direct", "blocking"]

# 工具名称最大长度。
_TOOL_NAME_MAX_LENGTH = 80

# 工具名称允许的字符正则。
_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")


@dataclass(frozen=True, slots=True)
class LocalToolDefinition:
    """本地工具定义实体（聚合根）。

    描述一个本地工具的静态元数据和可执行引用。
    Agent 定义中通过 tool_name 引用本地工具。

    业务规则（在实体内部校验）：
    - tool_name：非空，长度不超过 80，仅允许字母、数字、下划线、连字符。
    - description：非空（LLM 看到的工具描述）。
    - fn：必须是可调用对象。
    - group：非空，默认 "default"（工具分组，如 "文件"、"网络"）。
    - public：是否公开，默认 True（列出所有工具时是否可见）。
    - wiki：人类可读的工具介绍文档，可为 None。
    - execution_mode：默认 direct；只有显式 blocking 的同步工具可进入阻塞调度池。
    - blocking_pool/blocking_operation：blocking 工具必须显式声明调度池和稳定操作名。
    """

    # 工具唯一名称（如 "search_files"），Agent 定义中通过此名称引用。
    tool_name: str

    # 工具描述，传给 LLM 的 function description（简洁、功能导向）。
    description: str

    # 实际执行函数引用。
    fn: Callable[..., Any]

    # 工具分组（如 "文件"、"网络"、"数据库"），默认 "default"。
    group: str = "default"

    # 是否公开（列出所有工具时是否可见），默认 True。
    public: bool = True

    # 人类可读的工具介绍文档（用于生成系统工具说明文档），可为 None。
    wiki: str | None = None

    # 工具类别。普通业务工具默认不表达生命周期，只返回 continue 决策。
    category: ToolCategory = "business"

    # 是否默认对所有 Agent 可用。生命周期工具等系统级工具可使用该能力自动注入。
    always_enabled: bool = False

    # 当前工具允许返回的循环控制决策集合，用于 Runtime 侧做协议约束。
    allowed_loop_decisions: tuple[LoopDecision, ...] = ("continue",)

    # 工具执行模式。direct 保持兼容；blocking 表示同步 callable 需要进入 BlockingScheduler。
    execution_mode: LocalToolExecutionMode = "direct"

    # blocking 模式下使用的 BlockingScheduler 逻辑池名称，必须由工具定义显式声明。
    blocking_pool: str | None = None

    # blocking 模式下用于诊断和限流统计的稳定操作名，必须由工具定义显式声明。
    blocking_operation: str | None = None

    # 可选的阻塞执行慢执行阈值（秒）。未设置时使用 BlockingScheduler 默认值。
    blocking_slow_execution_seconds: float | None = None

    def __post_init__(self) -> None:
        """实体创建时校验业务规则。"""
        # ── tool_name 校验 ──────────────────────────────────────────
        if not self.tool_name or not self.tool_name.strip():
            raise ValueError("工具名称不能为空。")
        if len(self.tool_name) > _TOOL_NAME_MAX_LENGTH:
            raise ValueError(
                f"工具名称长度不能超过 {_TOOL_NAME_MAX_LENGTH}，"
                f"当前长度: {len(self.tool_name)}。",
            )
        if not _TOOL_NAME_PATTERN.match(self.tool_name):
            raise ValueError(
                "工具名称仅允许字母、数字、下划线和连字符。"
                f"当前值: '{self.tool_name}'。",
            )

        # ── description 校验 ────────────────────────────────────────
        if not self.description or not self.description.strip():
            raise ValueError("工具描述不能为空。")

        # ── fn 校验 ─────────────────────────────────────────────────
        if not callable(self.fn):
            raise ValueError("fn 必须是可调用对象。")

        # ── group 校验 ──────────────────────────────────────────────
        if not self.group or not self.group.strip():
            raise ValueError("工具分组不能为空。")

        # ── allowed_loop_decisions 校验 ──────────────────────────────
        if not self.allowed_loop_decisions:
            raise ValueError("工具允许的循环决策集合不能为空。")

        # ── execution classification 校验 ───────────────────────────
        if self.execution_mode not in ("direct", "blocking"):
            raise ValueError(f"未知工具执行模式: {self.execution_mode}")

        if self.execution_mode == "blocking":
            if not self.blocking_pool or not str(self.blocking_pool).strip():
                raise ValueError("blocking 工具必须声明 blocking_pool。")
            if not self.blocking_operation or not self.blocking_operation.strip():
                raise ValueError("blocking 工具必须声明 blocking_operation。")
        else:
            if self.blocking_pool is not None:
                raise ValueError("direct 工具不能声明 blocking_pool。")
            if self.blocking_operation is not None:
                raise ValueError("direct 工具不能声明 blocking_operation。")

        if self.blocking_slow_execution_seconds is not None and self.blocking_slow_execution_seconds <= 0:
            raise ValueError("blocking_slow_execution_seconds 必须大于 0。")

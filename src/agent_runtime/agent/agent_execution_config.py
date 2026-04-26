# -*- coding: utf-8 -*-
"""Agent 执行循环配置与默认值。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentExecutionConfig:
    """Agent 执行循环运行参数。

    当前只保存执行循环自身的安全边界配置，不保存任何用户动态业务数据。
    """

    # 单个 Agent 实例一次执行中允许的最大工具调用轮次，防止工具调用死循环。
    max_tool_rounds: int = 20

    def __post_init__(self) -> None:
        """校验执行循环配置。"""
        if self.max_tool_rounds < 0:
            raise ValueError("agent_execution.max_tool_rounds 必须是非负整数。")

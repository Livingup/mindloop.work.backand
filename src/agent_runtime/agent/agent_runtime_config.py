# -*- coding: utf-8 -*-
"""Agent 运行态管理模块配置。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final


AGENT_RUNTIME_REPOSITORY_MEMORY: Final[str] = "memory"
AGENT_RUNTIME_REPOSITORY_SQLITE: Final[str] = "sqlite"
SUPPORTED_AGENT_RUNTIME_REPOSITORIES: Final[frozenset[str]] = frozenset({
    AGENT_RUNTIME_REPOSITORY_MEMORY,
    AGENT_RUNTIME_REPOSITORY_SQLITE,
})


@dataclass(frozen=True, slots=True)
class AgentRuntimeStateConfig:
    """Agent 任务与实例运行态管理配置。

    该配置只描述运行态基础设施：使用哪种仓储实现、SQLite 数据目录等。
    task、Agent 实例、父子关系和状态本身是程序运行过程中的动态事实，
    不写入服务 YAML 配置文件。
    """

    # Agent 运行态仓储类型。memory 仅适合单进程开发测试；sqlite 支持本机多 worker / 多进程共享。
    repository_type: str = AGENT_RUNTIME_REPOSITORY_SQLITE

    # SQLite 运行态仓储的数据目录。repository_type=sqlite 时使用。
    data_dir: str = "~/.spread-ai/agent-runtime"

    # Agent 实例执行过程 Markdown Trace 文件根目录。
    trace_dir: str = "~/.spread-ai/agent-traces"

    def __post_init__(self) -> None:
        """校验运行态仓储配置合法性。"""
        if self.repository_type not in SUPPORTED_AGENT_RUNTIME_REPOSITORIES:
            raise ValueError(
                "agent_runtime.repository_type 不受支持，允许值："
                + ", ".join(sorted(SUPPORTED_AGENT_RUNTIME_REPOSITORIES))
            )
        if not isinstance(self.data_dir, str) or not self.data_dir.strip():
            raise ValueError("agent_runtime.data_dir 不能为空。")
        if not isinstance(self.trace_dir, str) or not self.trace_dir.strip():
            raise ValueError("agent_runtime.trace_dir 不能为空。")
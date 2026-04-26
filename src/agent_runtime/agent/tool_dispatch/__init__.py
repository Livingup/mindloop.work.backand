"""Agent 内部工具调度模块。

该模块根据当前 AgentDefinition 的能力边界构造 LLM 可见工具 schema，并在 LLM 返回工具调用后，
把调用分发到本地工具或 MCP 工具执行。
"""

from agent_runtime.agent.tool_dispatch.tool_dispatcher import (
    AGENT_MCP_TOOL_NAME_PREFIX,
    AgentToolDispatcher,
    ToolDispatchResult,
)

__all__ = [
    "AGENT_MCP_TOOL_NAME_PREFIX",
    "AgentToolDispatcher",
    "ToolDispatchResult",
]

"""Agent 运行时实例与任务管理模块。

该模块负责程序内部动态运行态与执行循环管理：
- 任务 AgentTask；
- 运行时 Agent 实例 AgentInstance；
- 一个任务内唯一根 Agent 实例；
- 任务内 Agent 实例父子关系树；
- 任务和实例状态流转；
- Agent 执行循环、LLM 请求、工具调度与实例 Trace 记录。

"""

from agent_runtime.agent.agent_instance import (
    AGENT_INSTANCE_STATUS_READY,
    AGENT_INSTANCE_STATUS_RUNNING,
    EXECUTION_SUBSTATE_NOTE_UPDATE,
    AgentInstance,
    AgentInstanceStatus,
    PendingAgentUserMessage,
)
from agent_runtime.agent.agent_task import (
    AGENT_TASK_STATUS_COMPLETED,
    AGENT_TASK_STATUS_FAILED,
    AGENT_TASK_STATUS_UNFINISHED,
    AgentParentChildRelation,
    AgentTask,
    AgentTaskStatus,
)
from agent_runtime.agent.agent_runtime_factory import AgentRuntimeFactory
from agent_runtime.agent.agent_execution_engine import (
    AgentExecutionEngine,
    AgentExecutionRequest,
    AgentExecutionResult,
)
from agent_runtime.agent.agent_execution_factory import AgentExecutionFactory

__all__ = [
    "AgentInstance",
    "AgentInstanceStatus",
    "PendingAgentUserMessage",
    "AGENT_INSTANCE_STATUS_READY",
    "AGENT_INSTANCE_STATUS_RUNNING",
    "EXECUTION_SUBSTATE_NOTE_UPDATE",
    "AgentTask",
    "AgentTaskStatus",
    "AgentParentChildRelation",
    "AGENT_TASK_STATUS_UNFINISHED",
    "AGENT_TASK_STATUS_COMPLETED",
    "AGENT_TASK_STATUS_FAILED",
    "AgentRuntimeFactory",
    "AgentExecutionEngine",
    "AgentExecutionRequest",
    "AgentExecutionResult",
    "AgentExecutionFactory",
]
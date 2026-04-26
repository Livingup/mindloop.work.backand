"""Agent 运行时实例与任务管理模块。

该模块负责程序内部动态运行态管理：
- 任务 AgentTask；
- 运行时 Agent 实例 AgentInstance；
- 一个任务内唯一根 Agent 实例；
- 任务内 Agent 实例父子关系树；
- 任务和实例状态流转。

真正的 Agent 执行循环、LLM 请求客户端和工具调度由后续模块实现。
"""

from agent_runtime.agent.agent_instance import (
    AGENT_INSTANCE_STATUS_CANCELLED,
    AGENT_INSTANCE_STATUS_COMPLETED,
    AGENT_INSTANCE_STATUS_CREATED,
    AGENT_INSTANCE_STATUS_FAILED,
    AGENT_INSTANCE_STATUS_PAUSED,
    AGENT_INSTANCE_STATUS_RUNNING,
    AgentInstance,
    AgentInstanceStatus,
)
from agent_runtime.agent.agent_task import (
    AGENT_TASK_STATUS_CANCELLED,
    AGENT_TASK_STATUS_COMPLETED,
    AGENT_TASK_STATUS_CREATED,
    AGENT_TASK_STATUS_FAILED,
    AGENT_TASK_STATUS_PAUSED,
    AGENT_TASK_STATUS_RUNNING,
    AgentParentChildRelation,
    AgentTask,
    AgentTaskStatus,
)
from agent_runtime.agent.agent_runtime_factory import AgentRuntimeFactory

__all__ = [
    "AgentInstance",
    "AgentInstanceStatus",
    "AGENT_INSTANCE_STATUS_CREATED",
    "AGENT_INSTANCE_STATUS_RUNNING",
    "AGENT_INSTANCE_STATUS_PAUSED",
    "AGENT_INSTANCE_STATUS_COMPLETED",
    "AGENT_INSTANCE_STATUS_FAILED",
    "AGENT_INSTANCE_STATUS_CANCELLED",
    "AgentTask",
    "AgentTaskStatus",
    "AgentParentChildRelation",
    "AGENT_TASK_STATUS_CREATED",
    "AGENT_TASK_STATUS_RUNNING",
    "AGENT_TASK_STATUS_PAUSED",
    "AGENT_TASK_STATUS_COMPLETED",
    "AGENT_TASK_STATUS_FAILED",
    "AGENT_TASK_STATUS_CANCELLED",
    "AgentRuntimeFactory",
]

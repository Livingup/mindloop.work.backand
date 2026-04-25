# -*- coding: utf-8 -*-
"""Agent 定义模块。

负责管理 Agent 的静态模板数据，包括系统提示词混合片段、本地工具、MCP 工具、Skill、子 Agent、
分组、内置后台标记和禁用状态。
"""
from agent_runtime.agent_definition.agent_definition import (
    AgentDefinition,
    McpToolBinding,
    SystemPromptSegment,
)
from agent_runtime.agent_definition.agent_definition_config import AgentDefinitionConfig
from agent_runtime.agent_definition.agent_definition_factory import AgentDefinitionFactory
from agent_runtime.agent_definition.north_gateway.agent_definition_gateway_impl import (
    AgentDefinitionGatewayImpl,
)

__all__ = [
    "AgentDefinition",
    "AgentDefinitionConfig",
    "AgentDefinitionFactory",
    "AgentDefinitionGatewayImpl",
    "McpToolBinding",
    "SystemPromptSegment",
]

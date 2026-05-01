"""本地工具模块。

负责本地工具的定义、自动发现、注册、执行，
以及各 LLM 平台工具 JSON Schema 的生成与缓存管理。
工具通过 @tool 装饰器声明，import 时自动注册到全局注册表。
"""
from agent_runtime.tool.local_tool_definition import LocalToolDefinition
from agent_runtime.tool.local_tool_factory import LocalToolFactory
from agent_runtime.tool.local_tool_repository import LocalToolRepository
from agent_runtime.tool.local_tool_service import LocalToolService
from agent_runtime.tool.north_gateway.local_tool_contracts import (
    LocalToolGateway,
)
from agent_runtime.tool.tool_decorator import tool
from agent_runtime.tool.tool_execution_models import (
    BaseToolRequest,
    BaseToolResult,
    FrontendMessage,
    LoopControl,
)
from agent_runtime.tool.tool_schema_cache import ToolSchemaCache
from agent_runtime.tool.tool_schema_generator import ToolSchemaGenerator

__all__ = [
    "LocalToolDefinition",
    "LocalToolFactory",
    "LocalToolGateway",
    "LocalToolRepository",
    "LocalToolService",
    "BaseToolRequest",
    "BaseToolResult",
    "FrontendMessage",
    "LoopControl",
    "ToolSchemaCache",
    "ToolSchemaGenerator",
    "tool",
]

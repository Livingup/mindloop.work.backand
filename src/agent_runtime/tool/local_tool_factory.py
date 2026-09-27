"""本地工具模块工厂。

负责复杂对象创建和依赖装配，是唯一知道所有具体实现类的地方。
"""
from __future__ import annotations

from typing import Any

from agent_runtime.tool._global_registry import get_global_repository
from agent_runtime.tool.infrastructure.claude_schema_generator import (
    ClaudeSchemaGenerator,
)
from agent_runtime.tool.infrastructure.gemini_schema_generator import (
    GeminiSchemaGenerator,
)
from agent_runtime.tool.infrastructure.openai_responses_schema_generator import (
    CodexResponsesSchemaGenerator,
    OpenAIResponsesSchemaGenerator,
)
from agent_runtime.tool.infrastructure.openai_schema_generator import (
    OpenAIChatCompletionsSchemaGenerator,
)
from agent_runtime.tool.infrastructure.zhipu_schema_generator import (
    ZhipuChatCompletionsSchemaGenerator,
)
from agent_runtime.tool.local_tool_service import LocalToolService
from agent_runtime.tool.north_gateway.local_tool_gateway_impl import (
    LocalToolGatewayImpl,
)
from agent_runtime.tool.tool_schema_cache import ToolSchemaCache


class LocalToolFactory:
    """本地工具模块工厂。

    组装全局仓储、Schema 缓存（含各协议格式生成器）、领域服务和北向网关。
    """

    @staticmethod
    def create(
        schema_cache: ToolSchemaCache | None = None,
        execution_loop_logger: Any | None = None,
    ) -> LocalToolGatewayImpl:
        """创建本地工具北向网关实例。

        自动注册各协议格式的 Schema 生成器。

        Args:
            schema_cache: Schema 缓存管理器。为 None 时创建默认缓存并注册所有平台生成器。

        Returns:
            LocalToolGatewayImpl 实例，实现 LocalToolGateway 协议。
        """
        import agent_runtime.agent_lifecycle.agent_lifecycle_tool  # noqa: F401
        import agent_runtime.file_tools.file_tools  # noqa: F401
        import agent_runtime.file_tools.file_edit_tools  # noqa: F401
        import agent_runtime.file_tools.image_file_tools  # noqa: F401
        import agent_runtime.frontend_debug.frontend_debug_tool  # noqa: F401
        import agent_runtime.image_generation.image_generation_tool  # noqa: F401
        import agent_runtime.mcp.mcp_meta_tools.mcp_meta_tools  # noqa: F401
        import agent_runtime.subagent.subagent_tool  # noqa: F401
        import agent_runtime.terminal.terminal_tool  # noqa: F401
        import agent_runtime.task_note.task_note_tool  # noqa: F401
        import agent_runtime.web_tools.web_tools  # noqa: F401

        repository = get_global_repository()
        if schema_cache is None:
            schema_cache = ToolSchemaCache()
            # 注册各协议格式的 Schema 生成器。
            schema_cache.register_generator(OpenAIResponsesSchemaGenerator())
            schema_cache.register_generator(CodexResponsesSchemaGenerator())
            schema_cache.register_generator(OpenAIChatCompletionsSchemaGenerator())
            schema_cache.register_generator(ClaudeSchemaGenerator())
            schema_cache.register_generator(GeminiSchemaGenerator())
            schema_cache.register_generator(ZhipuChatCompletionsSchemaGenerator())
        service = LocalToolService(
            repository=repository,
            schema_cache=schema_cache,
            execution_loop_logger=execution_loop_logger,
        )
        return LocalToolGatewayImpl(
            service=service,
            execution_loop_logger=execution_loop_logger,
        )

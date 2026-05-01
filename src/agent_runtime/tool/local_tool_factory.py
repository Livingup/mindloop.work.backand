"""本地工具模块工厂。

负责复杂对象创建和依赖装配，是唯一知道所有具体实现类的地方。
"""
from __future__ import annotations

from agent_runtime.tool._global_registry import get_global_repository
from agent_runtime.tool.infrastructure.claude_schema_generator import (
    ClaudeSchemaGenerator,
)
from agent_runtime.tool.infrastructure.gemini_schema_generator import (
    GeminiSchemaGenerator,
)
from agent_runtime.tool.infrastructure.openai_schema_generator import (
    OpenAISchemaGenerator,
)
from agent_runtime.tool.local_tool_service import LocalToolService
from agent_runtime.tool.north_gateway.local_tool_gateway_impl import (
    LocalToolGatewayImpl,
)
from agent_runtime.tool.tool_schema_cache import ToolSchemaCache


class LocalToolFactory:
    """本地工具模块工厂。

    组装全局仓储、Schema 缓存（含三个平台生成器）、领域服务和北向网关。
    """

    @staticmethod
    def create(
        schema_cache: ToolSchemaCache | None = None,
    ) -> LocalToolGatewayImpl:
        """创建本地工具北向网关实例。

        自动注册 OpenAI / Claude / Gemini 三个平台的 Schema 生成器。

        Args:
            schema_cache: Schema 缓存管理器。为 None 时创建默认缓存并注册所有平台生成器。

        Returns:
            LocalToolGatewayImpl 实例，实现 LocalToolGateway 协议。
        """
        import agent_runtime.agent_lifecycle.agent_lifecycle_tool  # noqa: F401

        repository = get_global_repository()
        if schema_cache is None:
            schema_cache = ToolSchemaCache()
            # 注册所有平台的 Schema 生成器。
            schema_cache.register_generator(OpenAISchemaGenerator())
            schema_cache.register_generator(ClaudeSchemaGenerator())
            schema_cache.register_generator(GeminiSchemaGenerator())
        service = LocalToolService(repository=repository, schema_cache=schema_cache)
        return LocalToolGatewayImpl(service=service)

# -*- coding: utf-8 -*-
"""Agent 执行循环引擎工厂。"""
from __future__ import annotations

from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine
from agent_runtime.agent.agent_runtime_factory import AgentRuntimeFactory
from agent_runtime.agent.agent_trace_recorder import AgentTraceRecorder
from agent_runtime.agent.tool_dispatch.tool_dispatcher import AgentToolDispatcher
from agent_runtime.agent_definition.agent_definition_factory import AgentDefinitionFactory
from agent_runtime.conversation_message.infrastructure import SqliteConversationRepository
from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import (
    ResourceMeta,
)
from agent_runtime.file_resource import FileResourceServiceFactory
from agent_runtime.llm_client import LlmClientFactory
from agent_runtime.llm_model_registry.llm_model_registry_factory import (
    LlmModelRegistryFactory,
)
from agent_runtime.mcp.mcp_client.mcp_client_factory import McpClientFactory
from agent_runtime.prompt_template import PromptTemplateFactory
from agent_runtime.tool.local_tool_factory import LocalToolFactory


class _LazyFileResourceResolver:
    """按需创建统一文件资源服务的资源解析器。

    Agent 执行循环经常只处理纯文本消息。如果工厂创建时就解析 file_resource_service 配置，
    会导致纯文本执行也被 OSS 环境变量阻塞。该解析器只在 image/pdf 渲染真正请求资源内容时，
    才读取 ConfigRegistry.file_resource_service 并创建 FileResourceService。
    """

    def __init__(self) -> None:
        self._file_resource_service = None

    async def get_base64(self, resource_id: str) -> str:
        """按资源标识获取 Base64 编码内容。"""
        results = self._service().get_resources([resource_id])
        result = results[0]
        if result is None:
            raise ValueError(f"资源不存在: {resource_id}")
        return result.resource_base64_content

    async def get_resource_meta(self, resource_id: str) -> ResourceMeta:
        """按资源标识获取 MIME 类型和资源类别。"""
        results = self._service().get_resources([resource_id])
        result = results[0]
        if result is None:
            raise ValueError(f"资源不存在: {resource_id}")
        return ResourceMeta(
            mime_type=result.resource_mime_type,
            resource_category=result.resource_metadata.resource_category,
        )

    def _service(self):
        """懒创建统一文件资源服务。"""
        if self._file_resource_service is None:
            from agent_runtime.config import ConfigRegistry

            self._file_resource_service = FileResourceServiceFactory.create_from_config(
                ConfigRegistry.instance().file_resource_service,
            )
        return self._file_resource_service


class AgentExecutionFactory:
    """Agent 执行循环引擎工厂。

    工厂是执行循环的组合根，负责装配运行态管理、Agent 静态定义、LLM 注册、Conversation
    持久化、文件资源解析、LLM 请求客户端、本地工具、MCP 工具和 Trace 记录器。
    """

    @staticmethod
    def create() -> AgentExecutionEngine:
        """创建 Agent 执行循环引擎。"""
        from agent_runtime.config import ConfigRegistry

        config = ConfigRegistry.instance()
        conversation_repository = SqliteConversationRepository(
            config.conversation_repository.data_dir,
        )
        local_tool_gateway = LocalToolFactory.create()
        mcp_gateway = McpClientFactory.create()
        tool_dispatcher = AgentToolDispatcher(
            local_tool_gateway=local_tool_gateway,
            mcp_client_gateway=mcp_gateway,
        )
        trace_recorder = AgentTraceRecorder(
            trace_dir_accessor=lambda: ConfigRegistry.instance().agent_runtime.trace_dir,
        )
        return AgentExecutionEngine(
            runtime_gateway=AgentRuntimeFactory.create(),
            agent_definition_gateway=AgentDefinitionFactory.create(),
            llm_registry_gateway=LlmModelRegistryFactory.create(),
            conversation_repository=conversation_repository,
            prompt_snippet_gateway=PromptTemplateFactory.create(),
            llm_request_service=LlmClientFactory.create(_LazyFileResourceResolver()),
            tool_dispatcher=tool_dispatcher,
            trace_recorder=trace_recorder,
        )
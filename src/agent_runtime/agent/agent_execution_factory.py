# -*- coding: utf-8 -*-
"""Agent 执行循环引擎工厂。"""
from __future__ import annotations

from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine
from agent_runtime.agent.agent_runtime_factory import AgentRuntimeFactory
from agent_runtime.agent.agent_trace_recorder import AgentTraceRecorder
from agent_runtime.agent.tool_dispatch.tool_dispatcher import AgentToolDispatcher
from agent_runtime.agent_definition.agent_definition_factory import AgentDefinitionFactory
from agent_runtime.communication.communication_factory import CommunicationFactory
from agent_runtime.communication.runtime_event_publisher import CommunicationRuntimeEventPublisher
from agent_runtime.conversation_message.infrastructure import SqliteConversationRepository
from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import (
    ResolvedResource,
    ResourceMeta,
)
from agent_runtime.file_resource.shared_service import get_file_resource_service
from agent_runtime.llm_client import LlmClientFactory
from agent_runtime.llm_model_registry.llm_model_registry_factory import (
    LlmModelRegistryFactory,
)
from agent_runtime.mcp.mcp_client.mcp_client_factory import McpClientFactory
from agent_runtime.prompt_template import PromptTemplateFactory
from agent_runtime.skill.skill_factory import SkillFactory
from agent_runtime.tool.local_tool_factory import LocalToolFactory


class _LazyFileResourceResolver:
    """按需创建统一文件资源服务的资源解析器。

    Agent 执行循环经常只处理纯文本消息。如果工厂创建时就解析 file_resource_service 配置，
    会导致纯文本执行也被 OSS 环境变量阻塞。该解析器只在 image/pdf 渲染真正请求资源内容时，
    才读取 ConfigRegistry.file_resource_service 并创建 FileResourceService。
    """

    def __init__(self) -> None:
        pass

    async def get_base64(self, resource_id: str) -> str:
        """按资源标识获取 Base64 编码内容。"""
        return (await self.resolve_resource(resource_id)).base64_content or ""

    async def get_public_url(self, resource_id: str) -> str | None:
        return (await self.resolve_resource(resource_id)).object_storage_url

    async def get_resource_meta(self, resource_id: str) -> ResourceMeta:
        """按资源标识获取 MIME 类型和资源类别。"""
        resource = await self.resolve_resource(resource_id)
        return ResourceMeta(
            mime_type=resource.mime_type,
            resource_category=resource.resource_category,
            size_in_bytes=resource.size_in_bytes,
            image_width=resource.image_width,
            image_height=resource.image_height,
            pdf_page_count=resource.pdf_page_count,
        )

    async def resolve_resource(self, resource_id: str) -> ResolvedResource:
        results = self._service().get_resources([resource_id])
        result = results[0]
        if result is None:
            raise ValueError(f"资源不存在: {resource_id}")
        metadata = result.resource_metadata
        return ResolvedResource(
            resource_id=result.resource_id,
            mime_type=result.resource_mime_type,
            resource_category=metadata.resource_category,
            object_storage_url=result.object_storage_url,
            base64_content=result.resource_base64_content,
            local_storage_path=result.local_storage_path,
            image_width=getattr(metadata, "image_width", None),
            image_height=getattr(metadata, "image_height", None),
            pdf_page_count=getattr(metadata, "pdf_page_count", None),
        )

    def _service(self):
        """懒创建统一文件资源服务。"""
        return get_file_resource_service()


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
        communication_gateway = CommunicationFactory.create()
        event_publisher = CommunicationRuntimeEventPublisher(communication_gateway)
        local_tool_gateway = LocalToolFactory.create()
        mcp_gateway = McpClientFactory.create()
        tool_dispatcher = AgentToolDispatcher(
            local_tool_gateway=local_tool_gateway,
            mcp_client_gateway=mcp_gateway,
            event_publisher=event_publisher,
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
            local_tool_gateway=local_tool_gateway,
            mcp_client_gateway=mcp_gateway,
            skill_gateway=SkillFactory.create(),
            llm_request_service=LlmClientFactory.create(
                _LazyFileResourceResolver(),
                event_publisher=event_publisher,
            ),
            tool_dispatcher=tool_dispatcher,
            trace_recorder=trace_recorder,
            communication_gateway=communication_gateway,
            event_publisher=event_publisher,
        )
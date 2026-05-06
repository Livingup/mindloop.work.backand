# -*- coding: utf-8 -*-
"""通用 LLM 请求客户端工厂。"""
from __future__ import annotations

from typing import Any

from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import (
    ResolvedResource,
    ResourceMeta,
)
from agent_runtime.communication.runtime_event_publisher import RuntimeEventPublisher
from agent_runtime.llm_client.llm_protocol_clients import (
    AnthropicMessagesProtocolClient,
    GeminiGenerateContentProtocolClient,
    OpenAIResponsesProtocolClient,
)
from agent_runtime.llm_client.llm_request_service import LlmRequestService


class _TextOnlyResourceResolver:
    """文本执行场景兜底资源解析器。

    当执行循环只处理文本消息时不会触发该解析器。若会话包含图片/PDF，调用方应显式注入
    FileResourceResolverAdapter，以便从统一文件资源服务读取资源内容。
    """

    async def get_base64(self, resource_id: str) -> str:
        raise RuntimeError(f"未配置文件资源解析器，无法读取资源: {resource_id}")

    async def get_public_url(self, resource_id: str) -> str | None:
        raise RuntimeError(f"未配置文件资源解析器，无法读取资源: {resource_id}")

    async def get_resource_meta(self, resource_id: str) -> ResourceMeta:
        raise RuntimeError(f"未配置文件资源解析器，无法读取资源元数据: {resource_id}")

    async def resolve_resource(self, resource_id: str) -> ResolvedResource:
        raise RuntimeError(f"未配置文件资源解析器，无法读取资源: {resource_id}")


class LlmClientFactory:
    """通用 LLM 请求客户端工厂。

    工厂装配当前已支持的三类 API 协议客户端：OpenAI Responses、Anthropic Messages、
    Gemini generateContent。OpenAI Chat Completions 和智谱 Chat Completions 需要先补齐
    conversation_message 适配器后再接入。
    """

    @staticmethod
    def create(
        resource_resolver: Any | None = None,
        event_publisher: RuntimeEventPublisher | None = None,
    ) -> LlmRequestService:
        """创建通用 LLM 请求服务。"""
        resolver = resource_resolver or _TextOnlyResourceResolver()
        return LlmRequestService(
            protocol_clients=(
                OpenAIResponsesProtocolClient(),
                AnthropicMessagesProtocolClient(),
                GeminiGenerateContentProtocolClient(),
            ),
            resource_resolver=resolver,
            event_publisher=event_publisher,
        )

"""智谱 GLM Chat Completions 协议适配器。

该模块实现 Z.AI / GLM Chat Completions API 的 ProviderAdapter，负责：
- 归一化：将 GLM 原始响应 → UniMessage（公共语义层 + providerExtras.raw）
- 渲染：将 Conversation.messages[] → GLM Chat Completions messages
- Preserved Thinking：在 clear_thinking=false 时原样回灌历史 assistant reasoning_content

GLM 5.1 的 Preserved Thinking 规则要求所有连续历史 reasoning_content block 按模型首次生成的
原始顺序完整、未修改地回传。本适配器对同协议 assistant 消息优先回灌 providerExtras.raw，避免
重建或改写 reasoning_content；跨协议降级时只输出公共文本层，不伪造推理内容。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent_runtime.conversation_message.message.models import (
    PROVIDER_ZHIPU_CHAT_COMPLETIONS,
    ROLE_ASSISTANT,
    Conversation,
    TextPart,
    ThinkingPart,
    ToolUsePart,
    UniMessage,
    UniPart,
)
from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import (
    ResourceResolver,
    resolve_resource_access,
)


@dataclass(frozen=True, slots=True)
class ZhipuChatRenderResult:
    """智谱 GLM Chat Completions 渲染结果。

    messages 是已经按 GLM Chat Completions 协议渲染后的对话历史。调用方在此基础上继续合并
    model、thinking、tools、tool_choice、max_tokens 等请求参数。
    """

    messages: list[dict[str, Any]]


class ZhipuChatAdapter:
    """智谱 GLM Chat Completions API 协议适配器。"""

    @property
    def provider(self) -> str:
        """协议标识。"""
        return PROVIDER_ZHIPU_CHAT_COMPLETIONS

    def normalize_response(self, raw_response: Any) -> UniMessage:
        """将 GLM Chat Completions 原始响应归一化为 UniMessage。

        GLM 5.1 在 thinking 启用时会在 assistant message 上返回 reasoning_content。该字段是
        Preserved Thinking 的关键上下文，必须与 content、tool_calls 一起完整保存到
        provider_extras["zhipu_chat_completions"]["raw"]，后续同协议渲染时原样回灌。
        """
        choices = raw_response.get("choices") or []
        choice = choices[0] if choices else {}
        raw_message = dict(choice.get("message") or {})
        raw_message.setdefault("role", "assistant")

        content: list[UniPart] = []
        reasoning_content = raw_message.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content:
            content.append(ThinkingPart(text=reasoning_content))

        text_content = raw_message.get("content")
        if isinstance(text_content, str) and text_content:
            content.append(TextPart(text=text_content))

        for tool_call in raw_message.get("tool_calls") or []:
            function = tool_call.get("function") or {}
            arguments = function.get("arguments", "{}")
            try:
                parsed_arguments = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                parsed_arguments = arguments
            content.append(
                ToolUsePart(
                    name=function.get("name", ""),
                    input=parsed_arguments,
                    tool_use_id=tool_call.get("id"),
                )
            )

        return UniMessage(
            role=ROLE_ASSISTANT,
            content=content,
            source_provider=PROVIDER_ZHIPU_CHAT_COMPLETIONS,
            provider_extras={
                PROVIDER_ZHIPU_CHAT_COMPLETIONS: {
                    "raw": raw_message,
                },
            },
        )

    async def render(
        self,
        conv: Conversation,
        resolver: ResourceResolver,
        options: dict[str, Any] | None = None,
    ) -> ZhipuChatRenderResult:
        """将统一会话渲染为 GLM Chat Completions messages。

        当请求参数表示 Preserved Thinking 开启（默认 clear_thinking=false）时，同协议 assistant
        消息会携带历史 raw.reasoning_content 原样回灌；当 clear_thinking=true 或 thinking disabled
        时，会从同协议 raw 中移除 reasoning_content，避免和调用方显式清理思考上下文的策略冲突。
        """
        preserve_reasoning_content = _should_preserve_reasoning_content(options)
        messages: list[dict[str, Any]] = []

        for message in conv.messages:
            if message.role == "user":
                messages.append(await _build_zhipu_user(message, resolver))
            elif message.role == "tool":
                messages.extend(_build_zhipu_tool_messages(message))
            else:
                messages.append(
                    _build_zhipu_assistant(
                        message,
                        preserve_reasoning_content=preserve_reasoning_content,
                    )
                )

        return ZhipuChatRenderResult(messages=messages)

    def preflight(self, conv: Conversation) -> None:
        """GLM Chat Completions 当前无额外预检约束。"""

    def update_state(
        self,
        conv: Conversation,
        raw_response: Any,
        options: dict[str, Any] | None = None,
    ) -> None:
        """记录 GLM Preserved Thinking 与自动上下文缓存相关运行态。"""
        preserve_reasoning_content = _should_preserve_reasoning_content(options)
        usage = raw_response.get("usage") or {}
        prompt_tokens_details = usage.get("prompt_tokens_details") or {}
        conv.provider_state[PROVIDER_ZHIPU_CHAT_COMPLETIONS] = {
            **(conv.provider_state.get(PROVIDER_ZHIPU_CHAT_COMPLETIONS) or {}),
            "preserved_thinking": preserve_reasoning_content,
            "clear_thinking": not preserve_reasoning_content,
            "cached_tokens": prompt_tokens_details.get("cached_tokens"),
        }


async def _build_zhipu_user(
    msg: UniMessage,
    resolver: ResourceResolver,
) -> dict[str, Any]:
    """将 user 消息转换为 GLM Chat Completions 格式。"""
    content_blocks: list[dict[str, Any]] = []
    text_parts: list[str] = []
    has_non_text_part = False

    for part in msg.content:
        if part.kind == "text":
            text = part.text
            text_parts.append(text)
            content_blocks.append({"type": "text", "text": text})
        elif part.kind == "image":
            has_non_text_part = True
            resource = await resolve_resource_access(resolver, part.resource_id)
            image_url = resource.object_storage_url
            if not image_url:
                data = _require_base64(resource.base64_content, part.resource_id)
                image_url = f"data:{resource.mime_type};base64,{data}"
            content_blocks.append({"type": "image_url", "image_url": {"url": image_url}})
        elif part.kind == "pdf":
            has_non_text_part = True
            resource = await resolve_resource_access(resolver, part.resource_id)
            file_url = resource.object_storage_url
            if not file_url:
                data = _require_base64(resource.base64_content, part.resource_id)
                file_url = f"data:{resource.mime_type or 'application/pdf'};base64,{data}"
            content_blocks.append({"type": "file_url", "file_url": {"url": file_url}})

    if not has_non_text_part:
        return {"role": "user", "content": "\n".join(text_parts)}
    return {"role": "user", "content": content_blocks}


def _build_zhipu_tool_messages(msg: UniMessage) -> list[dict[str, Any]]:
    """将统一 tool 消息转换为 GLM Chat Completions tool 消息列表。"""
    messages: list[dict[str, Any]] = []
    for part in msg.content:
        if part.kind == "tool_result":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": part.tool_use_ref,
                    "content": _serialize_tool_output(part.output),
                }
            )
    return messages


def _build_zhipu_assistant(
    msg: UniMessage,
    *,
    preserve_reasoning_content: bool,
) -> dict[str, Any]:
    """将 assistant 消息转换为 GLM Chat Completions 格式。"""
    if msg.source_provider == PROVIDER_ZHIPU_CHAT_COMPLETIONS:
        extras = (msg.provider_extras or {}).get(PROVIDER_ZHIPU_CHAT_COMPLETIONS)
        if extras and extras.get("raw") is not None:
            raw_message = dict(extras["raw"])
            raw_message.setdefault("role", "assistant")
            if not preserve_reasoning_content:
                raw_message.pop("reasoning_content", None)
            return raw_message

    text = "\n".join(
        part.text for part in msg.content if part.kind == "text" and hasattr(part, "text")
    )
    return {"role": "assistant", "content": text or "(empty)"}


def _should_preserve_reasoning_content(options: dict[str, Any] | None) -> bool:
    """根据请求参数判断是否需要回灌历史 reasoning_content。"""
    request_params = (options or {}).get("request_params") or {}
    thinking = request_params.get("thinking")
    if isinstance(thinking, dict):
        if thinking.get("type") == "disabled":
            return False
        if thinking.get("clear_thinking") is True:
            return False
    return True


def _serialize_tool_output(output: Any) -> str:
    """将工具执行结果稳定序列化为 tool 消息 content。"""
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False)


def _require_base64(base64_content: str | None, resource_id: str) -> str:
    if not base64_content:
        raise ValueError(f"资源缺少公网 URL 和 Base64 内容: {resource_id}")
    return base64_content

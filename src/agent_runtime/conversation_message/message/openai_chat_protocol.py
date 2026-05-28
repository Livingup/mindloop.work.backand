"""OpenAI Chat Completions 协议适配器。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent_runtime.conversation_message.message.assistant_projection import (
    AssistantTextSegment,
    AssistantToolUseSegment,
    clone_raw_value,
    iter_assistant_semantic_segments,
    serialize_tool_arguments,
    should_include_cross_protocol_thinking_text,
)
from agent_runtime.conversation_message.message.models import (
    PROVIDER_OPENAI_CHAT_COMPLETIONS,
    ROLE_ASSISTANT,
    Conversation,
    ImagePart,
    PdfPart,
    TextPart,
    ThinkingPart,
    ToolUsePart,
    UniMessage,
    UniPart,
)
from agent_runtime.conversation_message.message.tool_result_output import (
    prefix_tool_result_error_output,
    tool_result_output_to_text,
)
from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import (
    ResourceResolver,
    resolve_resource_access,
)


@dataclass(frozen=True, slots=True)
class OpenAIChatRenderResult:
    """OpenAI Chat Completions 渲染结果。"""

    messages: list[dict[str, Any]]


class OpenAIChatAdapter:
    """OpenAI Chat Completions API 协议适配器。"""

    @property
    def provider(self) -> str:
        """协议标识。"""
        return PROVIDER_OPENAI_CHAT_COMPLETIONS

    def normalize_response(self, raw_response: Any) -> UniMessage:
        """将 OpenAI Chat Completions 原始响应归一化为 UniMessage。"""
        choices = raw_response.get("choices") or []
        choice = choices[0] if choices else {}
        raw_message = dict(choice.get("message") or {})
        raw_message.setdefault("role", "assistant")

        content: list[UniPart] = []
        reasoning_content = raw_message.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content:
            content.append(ThinkingPart(text=reasoning_content))

        message_content = raw_message.get("content")
        if isinstance(message_content, str) and message_content:
            content.append(TextPart(text=message_content))
        elif isinstance(message_content, list):
            for block in message_content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        content.append(TextPart(text=text))

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
            source_provider=PROVIDER_OPENAI_CHAT_COMPLETIONS,
            provider_extras={
                PROVIDER_OPENAI_CHAT_COMPLETIONS: {
                    "raw": raw_message,
                },
            },
        )

    async def render(
        self,
        conv: Conversation,
        resolver: ResourceResolver,
        options: dict[str, Any] | None = None,
    ) -> OpenAIChatRenderResult:
        """将统一会话渲染为 OpenAI Chat Completions messages。"""
        messages: list[dict[str, Any]] = []
        for message in conv.messages:
            if message.role == "user":
                messages.append(await _build_openai_chat_user(message, resolver))
            elif message.role == "tool":
                messages.extend(await _build_openai_chat_tool_messages(message, resolver))
            else:
                messages.append(_build_openai_chat_assistant(message, options))
        return OpenAIChatRenderResult(messages=messages)

    def preflight(self, conv: Conversation) -> None:
        """OpenAI Chat Completions 当前无额外预检约束。"""

    def update_state(
        self,
        conv: Conversation,
        raw_response: Any,
        options: dict[str, Any] | None = None,
    ) -> None:
        """OpenAI Chat Completions 不维护平台会话态。"""


async def _build_openai_chat_user(
    msg: UniMessage,
    resolver: ResourceResolver,
) -> dict[str, Any]:
    """将 user 消息转换为 OpenAI Chat Completions 格式。"""
    content_blocks: list[dict[str, Any]] = []
    text_parts: list[str] = []
    has_non_text_part = False

    for part in msg.content:
        if part.kind == "text":
            text_parts.append(part.text)
            content_blocks.append({"type": "text", "text": part.text})
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
            content_blocks.append({"type": "file", "file": {"file_data": file_url}})

    if not has_non_text_part:
        return {"role": "user", "content": "\n".join(text_parts)}
    return {"role": "user", "content": content_blocks}


async def _build_openai_chat_tool_messages(
    msg: UniMessage,
    resolver: ResourceResolver,
) -> list[dict[str, Any]]:
    """将统一 tool 消息转换为 OpenAI Chat Completions tool 消息。

    OpenAI Chat tool 消息 content 只支持文本，多模态需降级为文本描述。
    is_error 通过在 content 前加 [ERROR] 标记表达。
    """
    messages: list[dict[str, Any]] = []
    for part in msg.content:
        if part.kind != "tool_result":
            continue
        output = prefix_tool_result_error_output(part.output) if part.is_error else part.output
        text_content = await _build_openai_chat_tool_result_text(output, resolver)
        messages.append({
            "role": "tool",
            "tool_call_id": part.tool_use_ref,
            "content": text_content or "(empty)",
        })
    return messages


async def _build_openai_chat_tool_result_text(
    output: Any,
    resolver: ResourceResolver,
) -> str:
    """构造 OpenAI Chat tool 消息的文本内容。

    多模态资源降级为 [<type> file: <路径或URL>] 文本引用。
    """
    return await tool_result_output_to_text(output, resolver)


def _build_openai_chat_assistant(
    msg: UniMessage,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """将 assistant 消息转换为 OpenAI Chat Completions 格式。"""
    if msg.source_provider == PROVIDER_OPENAI_CHAT_COMPLETIONS:
        extras = (msg.provider_extras or {}).get(PROVIDER_OPENAI_CHAT_COMPLETIONS)
        if extras and extras.get("raw") is not None:
            raw_message = clone_raw_value(extras["raw"])
            raw_message.setdefault("role", "assistant")
            return raw_message

    include_thinking_text = should_include_cross_protocol_thinking_text(options)
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for segment in iter_assistant_semantic_segments(
        msg,
        include_thinking_text=include_thinking_text,
    ):
        if isinstance(segment, AssistantTextSegment):
            text_parts.append(segment.text)
        elif isinstance(segment, AssistantToolUseSegment):
            tool_calls.append({
                "id": segment.tool_use_ref,
                "type": "function",
                "function": {
                    "name": segment.name,
                    "arguments": serialize_tool_arguments(segment.input),
                },
            })

    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if not message["content"] and not tool_calls:
        message["content"] = "(empty)"
    return message


def _require_base64(base64_content: str | None, resource_id: str) -> str:
    if not base64_content:
        raise ValueError(f"资源缺少公网 URL 和 Base64 内容: {resource_id}")
    return base64_content

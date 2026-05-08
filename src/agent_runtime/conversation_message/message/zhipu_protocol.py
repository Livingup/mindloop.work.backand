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

from agent_runtime.input_modality import (
    INPUT_MODALITY_IMAGE,
    INPUT_MODALITY_PDF,
    INPUT_MODALITY_TEXT,
)
from agent_runtime.conversation_message.message.assistant_projection import (
    AssistantTextSegment,
    AssistantToolUseSegment,
    clone_raw_value,
    iter_assistant_semantic_segments,
    serialize_tool_arguments,
    serialize_tool_output,
    should_include_cross_protocol_thinking_text,
)
from agent_runtime.conversation_message.message.models import (
    PROVIDER_ZHIPU_CHAT_COMPLETIONS,
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
                messages.append(await _build_zhipu_user(message, resolver, options))
            elif message.role == "tool":
                messages.extend(await _build_zhipu_tool_messages(message, resolver))
            else:
                messages.append(
                    _build_zhipu_assistant(
                        message,
                        preserve_reasoning_content=preserve_reasoning_content,
                        options=options,
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
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """将 user 消息转换为 GLM Chat Completions 格式。"""
    content_blocks: list[dict[str, Any]] = []
    text_parts: list[str] = []
    has_non_text_part = False
    supported_modalities = _supported_input_modalities(options)

    for part in msg.content:
        if part.kind == "text":
            text = part.text
            text_parts.append(text)
            content_blocks.append({"type": "text", "text": text})
        elif part.kind == "image":
            resource = await resolve_resource_access(resolver, part.resource_id)
            if INPUT_MODALITY_IMAGE not in supported_modalities:
                text_parts.append(_resource_path_text("image", resource))
                continue
            has_non_text_part = True
            image_url = resource.object_storage_url
            if not image_url:
                data = _require_base64(resource.base64_content, part.resource_id)
                image_url = f"data:{resource.mime_type};base64,{data}"
            content_blocks.append({"type": "image_url", "image_url": {"url": image_url}})
        elif part.kind == "pdf":
            resource = await resolve_resource_access(resolver, part.resource_id)
            if INPUT_MODALITY_PDF not in supported_modalities:
                text_parts.append(_resource_path_text("pdf", resource))
                continue
            has_non_text_part = True
            file_url = resource.object_storage_url
            if not file_url:
                data = _require_base64(resource.base64_content, part.resource_id)
                file_url = f"data:{resource.mime_type or 'application/pdf'};base64,{data}"
            content_blocks.append({"type": "file_url", "file_url": {"url": file_url}})

    if not has_non_text_part:
        return {"role": "user", "content": "\n".join(text_parts)}
    return {"role": "user", "content": content_blocks}


async def _build_zhipu_tool_messages(
    msg: UniMessage,
    resolver: ResourceResolver,
) -> list[dict[str, Any]]:
    """将统一 tool 消息转换为 GLM Chat Completions tool 消息列表。

    Zhipu tool 消息 content 只支持文本，多模态需降级为文本描述。
    is_error 通过在 content 前加 [ERROR] 标记表达。
    会顺带附带在 tool 消息中的外层 image / pdf part，作为文本引用拼接到首个 tool_result 后面。
    """
    tool_results: list[dict[str, Any]] = []
    resource_texts: list[str] = []
    for part in msg.content:
        if part.kind == "tool_result":
            text_content = await _build_zhipu_tool_result_text(part.output, resolver)
            if part.is_error:
                text_content = f"[ERROR] {text_content}"
            tool_results.append({
                "tool_call_id": part.tool_use_ref,
                "content": text_content,
            })
        elif part.kind == "image":
            resource = await resolve_resource_access(resolver, part.resource_id)
            resource_texts.append(_resource_path_text("image", resource))
        elif part.kind == "pdf":
            resource = await resolve_resource_access(resolver, part.resource_id)
            resource_texts.append(_resource_path_text("pdf", resource))

    messages: list[dict[str, Any]] = []
    for result in tool_results:
        content = result["content"]
        if resource_texts:
            content = "\n".join([content, *resource_texts])
        messages.append({
            "role": "tool",
            "tool_call_id": result["tool_call_id"],
            "content": content,
        })
    if resource_texts and not tool_results:
        messages.append({
            "role": "tool",
            "tool_call_id": "",
            "content": "\n".join(resource_texts),
        })
    return messages


async def _build_zhipu_tool_result_text(
    output: Any,
    resolver: ResourceResolver,
) -> str:
    """构造 Zhipu tool 消息的文本内容。

    多模态资源降级为 [<type> file: <路径或URL>] 文本引用。
    """
    if isinstance(output, list) and all(
        isinstance(item, (TextPart, ImagePart, PdfPart)) for item in output
    ):
        text_pieces: list[str] = []
        for item in output:
            if isinstance(item, TextPart):
                text_pieces.append(item.text)
            elif isinstance(item, ImagePart):
                resource = await resolve_resource_access(resolver, item.resource_id)
                text_pieces.append(_resource_path_text("image", resource))
            elif isinstance(item, PdfPart):
                resource = await resolve_resource_access(resolver, item.resource_id)
                text_pieces.append(_resource_path_text("pdf", resource))
        return "\n".join(text_pieces)
    return serialize_tool_output(output)


def _build_zhipu_assistant(
    msg: UniMessage,
    *,
    preserve_reasoning_content: bool,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """将 assistant 消息转换为 GLM Chat Completions 格式。"""
    if msg.source_provider == PROVIDER_ZHIPU_CHAT_COMPLETIONS:
        extras = (msg.provider_extras or {}).get(PROVIDER_ZHIPU_CHAT_COMPLETIONS)
        if extras and extras.get("raw") is not None:
            raw_message = clone_raw_value(extras["raw"])
            raw_message.setdefault("role", "assistant")
            if not preserve_reasoning_content:
                raw_message.pop("reasoning_content", None)
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


def _supported_input_modalities(options: dict[str, Any] | None) -> set[str]:
    modalities = (options or {}).get("supported_input_modalities")
    if modalities is None:
        return {INPUT_MODALITY_TEXT}
    return {str(item) for item in modalities}


def _resource_path_text(resource_type: str, resource: Any) -> str:
    path = resource.local_storage_path or resource.object_storage_url or resource.resource_id
    return f"[{resource_type} file: {path}]"


def _require_base64(base64_content: str | None, resource_id: str) -> str:
    if not base64_content:
        raise ValueError(f"资源缺少公网 URL 和 Base64 内容: {resource_id}")
    return base64_content

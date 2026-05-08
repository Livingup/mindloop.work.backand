"""OpenAI Responses API 协议适配器。

该模块实现 OpenAI Responses API 的 ProviderAdapter，负责：
- 归一化：将 OpenAI 原始响应 → UniMessage（公共语义层 + providerExtras.raw）
- 渲染：将 Conversation.messages[] → OpenAI Responses stateless input
- 状态更新：API 成功后记录 stateless 模式和 prompt_cache_key

渲染决策树：
- user 消息：通过 ResourceResolver 将 resourceId → data URI / input_file
- assistant 消息（同平台）：展开 output items 到顶层 input（含 reasoning / message / function_call）
- assistant 消息（跨平台）：合成一条 assistant message item（降级）
- tool 消息：展开为顶层 function_call_output item

OpenAI Responses 采用 stateless 路径，不依赖 previous_response_id / store=true。
每次请求都全量展开历史消息，并用 conversation id 作为 prompt_cache_key。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

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
    PROVIDER_OPENAI,
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
class OpenAIRenderResult:
    """OpenAI 渲染结果。

    包含渲染后的 input 数组和 prompt_cache_key。
    调用方在此基础上合并 model、instructions、reasoning 等参数后发送请求。

    注意：store / instructions 不在此处；请求客户端会强制 store=false。
    """

    # OpenAI Responses API 的 input 数组。
    input: list[Any]

    # OpenAI Responses stateless 路径不使用 previous_response_id。
    previous_response_id: str | None = None

    # 稳定缓存路由 key，默认使用 conversation id。
    prompt_cache_key: str | None = None

    # 始终包含，确保 raw 里有 encrypted_content。
    include: list[str] = field(
        default_factory=lambda: ["reasoning.encrypted_content"]
    )


class OpenAIAdapter:
    """OpenAI Responses API 协议适配器。

    实现 ProviderAdapter 接口，负责 OpenAI 协议的归一化、渲染和状态更新。
    内置预注册到 adapter 注册表中。
    """

    @property
    def provider(self) -> str:
        """协议标识。"""
        return PROVIDER_OPENAI

    # ── 归一化 ───────────────────────────────────────────────────

    def normalize_response(self, raw_response: Any) -> UniMessage:
        """将 OpenAI Responses API 原始响应归一化为 UniMessage。

        从响应的 output 数组中提取公共语义 parts（reasoning → thinking，
        message → text，function_call → tool_use），同时将完整的 output 数组
        存入 provider_extras["openai_responses"]["raw"]。

        OpenAI 响应结构示例：
        {
            "id": "resp_xxx",
            "output": [
                {"type": "reasoning", "id": "rs_xxx", "encrypted_content": "...", "summary": [...]},
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "..."}]}
            ]
        }
        """
        content: list[UniPart] = []

        for item in raw_response.get("output") or []:
            item_type = item.get("type")
            if item_type == "reasoning":
                # reasoning 通常无明文，可从 summary 提取摘要供 UI
                summary_parts = item.get("summary") or []
                summary_text = "\n".join(
                    s.get("text", "") for s in summary_parts if s.get("text")
                )
                content.append(
                    ThinkingPart(text=summary_text if summary_text else None)
                )
            elif item_type == "message":
                for c in item.get("content") or []:
                    if c.get("type") == "output_text":
                        content.append(TextPart(text=c.get("text", "")))
            elif item_type == "function_call":
                arguments = item.get("arguments", "{}")
                try:
                    parsed_input = json.loads(arguments)
                except (json.JSONDecodeError, TypeError):
                    parsed_input = arguments
                content.append(
                    ToolUsePart(
                        name=item.get("name", ""),
                        input=parsed_input,
                        tool_use_id=item.get("call_id") or item.get("id"),
                    )
                )

        return UniMessage(
            role=ROLE_ASSISTANT,
            content=content,
            source_provider=PROVIDER_OPENAI,
            provider_extras={
                PROVIDER_OPENAI: {
                    "raw": raw_response.get("output"),  # 完整 output 数组
                },
            },
        )

    # ── 渲染 ─────────────────────────────────────────────────────

    async def render(
        self,
        conv: Conversation,
        resolver: ResourceResolver,
        options: dict[str, Any] | None = None,
    ) -> OpenAIRenderResult:
        """将会话消息渲染为 OpenAI Responses API 的请求体。

        只产出消息相关字段：input + prompt_cache_key + include。
        模型名、instructions、reasoning、store 等由调用方合并；请求客户端会强制 store=false。
        """
        message_slice = conv.messages

        input_items: list[Any] = []
        for m in message_slice:
            if m.role == "user":
                input_items.append(await _build_openai_user(m, resolver))
            elif m.role == "tool":
                # tool 消息展开为顶层 function_call_output items
                for p in m.content:
                    if p.kind == "tool_result":
                        input_items.append({
                            "type": "function_call_output",
                            "call_id": p.tool_use_ref,
                            "output": await _build_openai_tool_result_output(
                                p.output, p.is_error, resolver,
                            ),
                        })
            else:
                # assistant 消息
                input_items.extend(_build_openai_assistant(m, options))

        return OpenAIRenderResult(
            input=input_items,
            previous_response_id=None,
            prompt_cache_key=conv.id,
        )

    # ── 预检 ─────────────────────────────────────────────────────

    def preflight(self, conv: Conversation) -> None:
        """OpenAI 当前无特殊预检约束，跳过。"""

    # ── 状态更新 ─────────────────────────────────────────────────

    def update_state(
        self,
        conv: Conversation,
        raw_response: Any,
        options: dict[str, Any] | None = None,
    ) -> None:
        """记录 OpenAI Responses stateless 模式运行态。"""
        provider_state = dict(conv.provider_state.get(PROVIDER_OPENAI) or {})
        provider_state.pop("last_response_id", None)
        provider_state.pop("last_message_index", None)
        provider_state["prompt_cache_key"] = conv.id
        provider_state["stateless"] = True
        conv.provider_state[PROVIDER_OPENAI] = provider_state


# ── 渲染内部函数 ─────────────────────────────────────────────────

async def _build_openai_user(
    msg: UniMessage,
    resolver: ResourceResolver,
) -> dict[str, Any]:
    """将 user 消息转换为 OpenAI Responses API 格式。

    通过 ResourceResolver 将 resourceId 转为 data URI。
    图片使用 input_image + data URI，PDF 使用 input_file + data URI。
    """
    content: list[dict[str, Any]] = []

    for p in msg.content:
        if p.kind == "text":
            content.append({"type": "input_text", "text": p.text})
        elif p.kind == "image":
            resource = await resolve_resource_access(resolver, p.resource_id)
            image_url = resource.object_storage_url
            if not image_url:
                data = _require_base64(resource.base64_content, p.resource_id)
                image_url = f"data:{resource.mime_type};base64,{data}"
            content.append({
                "type": "input_image",
                "image_url": image_url,
            })
        elif p.kind == "pdf":
            resource = await resolve_resource_access(resolver, p.resource_id)
            if resource.object_storage_url:
                content.append({
                    "type": "input_file",
                    "file_url": resource.object_storage_url,
                })
            else:
                data = _require_base64(resource.base64_content, p.resource_id)
                content.append({
                    "type": "input_file",
                    "file_data": f"data:{resource.mime_type or 'application/pdf'};base64,{data}",
                })

    return {"type": "message", "role": "user", "content": content}


def _require_base64(base64_content: str | None, resource_id: str) -> str:
    if not base64_content:
        raise ValueError(f"资源缺少公网 URL 和 Base64 内容: {resource_id}")
    return base64_content


async def _build_openai_tool_result_output(
    output: Any,
    is_error: bool,
    resolver: ResourceResolver,
) -> str | list[dict[str, Any]]:
    """构造 OpenAI Responses function_call_output 的 output 字段。

    OpenAI Responses 没有原生 is_error 字段，错误以文本标记编码进 output 内容。
    多模态资源优先使用 object_storage_url，无则降级到 base64 data URI。
    """
    if isinstance(output, list) and all(
        isinstance(item, (TextPart, ImagePart, PdfPart)) for item in output
    ):
        items: list[dict[str, Any]] = []
        if is_error:
            items.append({"type": "input_text", "text": "[ERROR] Tool execution failed."})
        for item in output:
            if isinstance(item, TextPart):
                items.append({"type": "input_text", "text": item.text})
            elif isinstance(item, ImagePart):
                resource = await resolve_resource_access(resolver, item.resource_id)
                image_url = resource.object_storage_url
                if not image_url:
                    data = _require_base64(resource.base64_content, item.resource_id)
                    image_url = f"data:{resource.mime_type};base64,{data}"
                items.append({"type": "input_image", "image_url": image_url})
            elif isinstance(item, PdfPart):
                resource = await resolve_resource_access(resolver, item.resource_id)
                if resource.object_storage_url:
                    items.append({
                        "type": "input_file",
                        "file_url": resource.object_storage_url,
                    })
                else:
                    data = _require_base64(resource.base64_content, item.resource_id)
                    items.append({
                        "type": "input_file",
                        "file_data": f"data:{resource.mime_type or 'application/pdf'};base64,{data}",
                    })
        return items
    text = serialize_tool_output(output)
    if is_error:
        return f"[ERROR] {text}"
    return text


def _build_openai_assistant(
    msg: UniMessage,
    options: dict[str, Any] | None = None,
) -> list[Any]:
    """将 assistant 消息转换为 OpenAI Responses API 格式。

    同平台（source_provider == "openai_responses"）：展开 output items 到顶层 input
    （含 reasoning / message / function_call，无损）。
    跨平台：合成一条 assistant message item（降级）。

    返回列表是因为同平台回灌时一条 UniMessage 可展开为多个 input items。
    """
    # 同平台无损回灌：展开 output items
    if msg.source_provider == PROVIDER_OPENAI:
        extras = (msg.provider_extras or {}).get(PROVIDER_OPENAI)
        if extras and extras.get("raw") is not None:
            return list(clone_raw_value(extras["raw"]))

    include_thinking_text = should_include_cross_protocol_thinking_text(options)
    input_items: list[Any] = []
    pending_text: list[str] = []

    def flush_text() -> None:
        if not pending_text:
            return
        input_items.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "\n".join(pending_text)}],
        })
        pending_text.clear()

    for segment in iter_assistant_semantic_segments(
        msg,
        include_thinking_text=include_thinking_text,
    ):
        if isinstance(segment, AssistantTextSegment):
            pending_text.append(segment.text)
        elif isinstance(segment, AssistantToolUseSegment):
            flush_text()
            input_items.append({
                "type": "function_call",
                "call_id": segment.tool_use_ref,
                "name": segment.name,
                "arguments": serialize_tool_arguments(segment.input),
            })
    flush_text()

    if input_items:
        return input_items
    return [{
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "(empty)"}],
    }]

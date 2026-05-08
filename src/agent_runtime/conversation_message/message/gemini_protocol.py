"""Gemini 协议适配器。

该模块实现 Google Gemini generateContent API 的 ProviderAdapter，负责：
- 归一化：将 Gemini 原始响应 → UniMessage（公共语义层 + providerExtras.raw）
- 渲染：将 Conversation.messages[] → Gemini 请求体（contents + cachedContent）
- 状态更新：当前不自动维护（cachedContent 由调用方管理）

渲染决策树：
- user 消息：通过 ResourceResolver 将 resourceId → inlineData base64
- assistant 消息（同平台）：原样回灌 providerExtras.gemini.raw（含 thoughtSignature，无损）
- assistant 消息（跨平台）：公共层降级，只取 text part 合并
- tool 消息：转为 Gemini 的 functionResponse 格式，合并进 user role

关键约束：
- assistant 角色映射为 "model"
- contents[0].role 必须是 "user"；user/model 交替
- cachedContent 仅在 providerState.gemini 存在时带上
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_runtime.conversation_message.message.assistant_projection import (
    AssistantTextSegment,
    AssistantToolUseSegment,
    build_tool_name_by_ref,
    clone_raw_value,
    iter_assistant_semantic_segments,
    should_include_cross_protocol_thinking_text,
    tool_name_for_result,
)
from agent_runtime.conversation_message.message.models import (
    PROVIDER_GEMINI,
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
class GeminiRenderResult:
    """Gemini 渲染结果。

    包含渲染后的 contents 数组和可选的 cachedContent 标识。
    调用方在此基础上合并 generationConfig、thinkingConfig、
    systemInstruction 等参数后发送请求。
    """

    # Gemini generateContent API 的 contents 数组。
    contents: list[dict[str, Any]]

    # 同平台续聊时的缓存内容标识（可选）。
    cached_content: str | None = None


class GeminiAdapter:
    """Gemini generateContent API 协议适配器。

    实现 ProviderAdapter 接口，负责 Gemini 协议的归一化和渲染。
    内置预注册到 adapter 注册表中。
    """

    @property
    def provider(self) -> str:
        """协议标识。"""
        return PROVIDER_GEMINI

    # ── 归一化 ───────────────────────────────────────────────────

    def normalize_response(self, raw_response: Any) -> UniMessage:
        """将 Gemini 原始响应归一化为 UniMessage。

        从响应的 candidates[0].content.parts 中提取公共语义 parts，
        同时将完整的 parts 数组存入 provider_extras["gemini_generate_content"]["raw"]。

        Gemini 响应结构示例：
        {
            "candidates": [{
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "思考内容", "thought": true, "thoughtSignature": "..."},
                        {"text": "最终答案"}
                    ]
                }
            }]
        }
        """
        candidates = raw_response.get("candidates") or []
        cand = candidates[0] if candidates else {}
        parts = (cand.get("content") or {}).get("parts") or []

        content: list[UniPart] = []
        for part_index, part in enumerate(parts):
            if part.get("thought") is True:
                # 思考块：thought=true 标识的 text part
                text = part.get("text") if isinstance(part.get("text"), str) else None
                content.append(ThinkingPart(text=text))
            elif isinstance(part.get("text"), str):
                content.append(TextPart(text=part["text"]))
            elif part.get("functionCall"):
                fc = part["functionCall"]
                content.append(
                    ToolUsePart(
                        name=fc.get("name", ""),
                        input=fc.get("args"),
                        tool_use_id=_build_gemini_tool_use_ref(fc.get("name", ""), part_index),
                    )
                )
            # inlineData / fileData 仅用户侧出现，响应一般不会

        return UniMessage(
            role=ROLE_ASSISTANT,
            content=content,
            source_provider=PROVIDER_GEMINI,
            provider_extras={
                PROVIDER_GEMINI: {
                    "raw": parts,  # 完整 parts 数组
                },
            },
        )

    # ── 渲染 ─────────────────────────────────────────────────────

    async def render(
        self,
        conv: Conversation,
        resolver: ResourceResolver,
        options: dict[str, Any] | None = None,
    ) -> GeminiRenderResult:
        """将会话消息渲染为 Gemini generateContent API 的请求体。

        只产出消息相关字段：contents + cachedContent。
        generationConfig、thinkingConfig、systemInstruction 等由调用方合并。
        """
        contents: list[dict[str, Any]] = []
        tool_name_by_ref = build_tool_name_by_ref(conv.messages)

        for m in conv.messages:
            if m.role == "user":
                contents.append(await _build_gemini_user(m, resolver))
            elif m.role == "tool":
                contents.append(await _build_gemini_tool(m, tool_name_by_ref, resolver))
            else:
                contents.append(_build_gemini_model(m, options))

        # 规范化：确保 contents[0].role == "user"，相邻同 role 合并
        normalized = _normalize_gemini_contents(contents)

        # cachedContent 仅在 providerState 中有记录时带上
        gemini_state = conv.provider_state.get(PROVIDER_GEMINI) or {}
        cached_content = gemini_state.get("cached_content")

        return GeminiRenderResult(
            contents=normalized,
            cached_content=cached_content,
        )

    # ── 预检 ─────────────────────────────────────────────────────

    def preflight(self, conv: Conversation) -> None:
        """Gemini 当前无特殊预检约束，跳过。"""

    # ── 状态更新 ─────────────────────────────────────────────────

    def update_state(
        self,
        conv: Conversation,
        raw_response: Any,
        options: dict[str, Any] | None = None,
    ) -> None:
        """Gemini 会话状态更新。

        当前 cachedContent 由调用方管理，本 adapter 不自动维护。
        """


# ── 渲染内部函数 ─────────────────────────────────────────────────

async def _build_gemini_user(
    msg: UniMessage,
    resolver: ResourceResolver,
) -> dict[str, Any]:
    """将 user 消息转换为 Gemini 格式。

    通过 ResourceResolver 将 resourceId 转为 inlineData base64 内容。
    """
    parts: list[dict[str, Any]] = []

    for p in msg.content:
        if p.kind == "text":
            parts.append({"text": p.text})
        elif p.kind == "image":
            resource = await resolve_resource_access(resolver, p.resource_id)
            if resource.object_storage_url:
                parts.append({
                    "fileData": {
                        "mimeType": resource.mime_type,
                        "fileUri": resource.object_storage_url,
                    }
                })
            else:
                data = _require_base64(resource.base64_content, p.resource_id)
                parts.append({"inlineData": {"mimeType": resource.mime_type, "data": data}})
        elif p.kind == "pdf":
            resource = await resolve_resource_access(resolver, p.resource_id)
            if resource.object_storage_url:
                parts.append({
                    "fileData": {
                        "mimeType": resource.mime_type or "application/pdf",
                        "fileUri": resource.object_storage_url,
                    }
                })
            else:
                data = _require_base64(resource.base64_content, p.resource_id)
                parts.append({
                    "inlineData": {
                        "mimeType": resource.mime_type or "application/pdf",
                        "data": data,
                    }
                })

    return {"role": "user", "parts": parts}


def _require_base64(base64_content: str | None, resource_id: str) -> str:
    if not base64_content:
        raise ValueError(f"资源缺少公网 URL 和 Base64 内容: {resource_id}")
    return base64_content


def _build_gemini_tool_use_ref(name: str, part_index: int) -> str:
    """为 Gemini functionCall 构造公共工具调用引用，供其它协议切换时使用。"""
    safe_name = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in name)
    return f"gemini_call_{part_index}_{safe_name or 'tool'}"


async def _build_gemini_tool(
    msg: UniMessage,
    tool_name_by_ref: dict[str, str],
    resolver: ResourceResolver,
) -> dict[str, Any]:
    """将 tool 消息转换为 Gemini 格式。

    Gemini 的 functionResponse 放在 user role 的 parts 中。
    is_error 编码为 response.error key，多模态资源走 functionResponse.parts。
    """
    parts: list[dict[str, Any]] = []

    for p in msg.content:
        if p.kind != "tool_result":
            continue
        function_response = await _build_gemini_function_response(
            p.tool_use_ref, p.is_error, p.output, tool_name_by_ref, resolver,
        )
        parts.append({"functionResponse": function_response})

    return {"role": "user", "parts": parts}


async def _build_gemini_function_response(
    tool_use_ref: str,
    is_error: bool,
    output: Any,
    tool_name_by_ref: dict[str, str],
    resolver: ResourceResolver,
) -> dict[str, Any]:
    """构造 Gemini functionResponse 对象。

    Gemini 约定 response 中使用 "output" key 表达输出，"error" key 表达错误。
    多模态资源优先 fileData (公网 URL)，无则降级到 inlineData (base64)。
    """
    response: dict[str, Any] = {}
    media_parts: list[dict[str, Any]] = []

    if isinstance(output, list) and all(
        isinstance(item, (TextPart, ImagePart, PdfPart)) for item in output
    ):
        text_pieces: list[str] = []
        for item in output:
            if isinstance(item, TextPart):
                text_pieces.append(item.text)
            elif isinstance(item, ImagePart):
                resource = await resolve_resource_access(resolver, item.resource_id)
                media_parts.append(_build_gemini_media_part(
                    resource, item.resource_id, default_mime=resource.mime_type,
                ))
            elif isinstance(item, PdfPart):
                resource = await resolve_resource_access(resolver, item.resource_id)
                media_parts.append(_build_gemini_media_part(
                    resource, item.resource_id, default_mime="application/pdf",
                ))
        text_content = "\n".join(text_pieces) if text_pieces else None
        if is_error:
            response["error"] = text_content or "Tool execution failed."
        elif text_content is not None:
            response["output"] = text_content
    elif isinstance(output, dict):
        if is_error:
            response["error"] = output
        else:
            response = dict(output)
    else:
        text = serialize_tool_output(output)
        if is_error:
            response["error"] = text
        else:
            response["output"] = text

    function_response: dict[str, Any] = {
        "name": tool_name_for_result(tool_use_ref, tool_name_by_ref),
        "response": response,
    }
    if media_parts:
        function_response["parts"] = media_parts
    return function_response


def _build_gemini_media_part(
    resource: Any,
    resource_id: str,
    default_mime: str | None,
) -> dict[str, Any]:
    """构造 Gemini functionResponse.parts 中的单个多模态 part。

    优先使用 fileData (公网 URL)，无则降级到 inlineData (base64)。
    """
    mime_type = resource.mime_type or default_mime
    if resource.object_storage_url:
        return {
            "fileData": {
                "mimeType": mime_type,
                "fileUri": resource.object_storage_url,
            }
        }
    data = _require_base64(resource.base64_content, resource_id)
    return {
        "inlineData": {
            "mimeType": mime_type,
            "data": data,
        }
    }


def _build_gemini_model(
    msg: UniMessage,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """将 assistant 消息转换为 Gemini 格式。

    同平台（source_provider == "gemini_generate_content"）：原样回灌 raw（含 thoughtSignature，无损）。
    跨平台：公共层降级，只取 text part。
    """
    # 同平台无损回灌
    if msg.source_provider == PROVIDER_GEMINI:
        extras = (msg.provider_extras or {}).get(PROVIDER_GEMINI)
        if extras and extras.get("raw") is not None:
            return {"role": "model", "parts": clone_raw_value(extras["raw"])}

    include_thinking_text = should_include_cross_protocol_thinking_text(options)
    parts: list[dict[str, Any]] = []
    for segment in iter_assistant_semantic_segments(
        msg,
        include_thinking_text=include_thinking_text,
    ):
        if isinstance(segment, AssistantTextSegment):
            parts.append({"text": segment.text})
        elif isinstance(segment, AssistantToolUseSegment):
            parts.append({
                "functionCall": {
                    "name": segment.name,
                    "args": segment.input,
                },
            })
    if not parts:
        parts.append({"text": "(empty)"})
    return {"role": "model", "parts": parts}


def _normalize_gemini_contents(
    contents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """规范化 Gemini contents 列表。

    确保满足 Gemini API 的约束：
    1. contents[0].role 必须是 "user"
    2. user/model 严格交替；相邻同 role 合并 parts
    """
    if not contents:
        return contents

    # 确保首条是 user
    if contents[0].get("role") != "user":
        contents.insert(0, {"role": "user", "parts": [{"text": "(placeholder)"}]})

    # 相邻同 role 合并
    merged: list[dict[str, Any]] = [contents[0]]
    for item in contents[1:]:
        if item.get("role") == merged[-1].get("role"):
            prev_parts = merged[-1].get("parts", [])
            curr_parts = item.get("parts", [])
            if isinstance(prev_parts, list) and isinstance(curr_parts, list):
                prev_parts.extend(curr_parts)
        else:
            merged.append(item)

    return merged

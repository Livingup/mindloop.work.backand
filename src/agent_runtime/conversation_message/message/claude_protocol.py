"""Claude 协议适配器。

该模块实现 Claude Messages API 的 ProviderAdapter，负责：
- 归一化：将 Claude 原始响应 → UniMessage（公共语义层 + providerExtras.raw）
- 渲染：将 Conversation.messages[] → Claude 请求体（messages + 缓存断点）
- 预检：校验 Claude 图片 ≤100 / PDF 页数 ≤100 的硬约束
- 状态更新：当前由调用方在 API 成功后手动写回缓存断点（本 adapter 不自动维护）

渲染决策树：
- user 消息：通过 ResourceResolver 将 resourceId → base64 inline
- assistant 消息（同平台）：原样回灌 providerExtras.claude.raw（含 signature，无损）
- assistant 消息（跨平台）：公共层降级，保留 text 和 tool_use
- tool 消息：转为 Claude 的 user/tool_result 消息

关键约束：
- messages[0].role 必须是 "user"；首条非 user 时插占位
- 不主动合并相邻同 role 消息，保持客户端消息边界
- 跨平台降级空 text 用 "(empty)" 占位
- 缓存断点（cache_control）不进 raw，渲染时动态打
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_runtime.conversation_message.message.assistant_projection import (
    AssistantTextSegment,
    AssistantToolUseSegment,
    clone_raw_value,
    iter_assistant_semantic_segments,
    serialize_tool_output,
    should_include_cross_protocol_thinking_text,
)
from agent_runtime.conversation_message.message.models import (
    PROVIDER_CLAUDE,
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

# Claude 硬约束上限（取 200K 模型的最紧值）。
_CLAUDE_IMAGE_LIMIT = 100
_CLAUDE_PDF_PAGE_LIMIT = 100


@dataclass(frozen=True, slots=True)
class ClaudeRenderResult:
    """Claude 渲染结果。

    包含渲染后的 messages 数组和本次实际应用的缓存断点位置。
    调用方在 API 调用成功后应将 applied_cache_breakpoint_indexes
    写回 conv.provider_state["anthropic_messages"]["cache_breakpoint_indexes"]，
    供下次渲染时复用缓存前缀。
    """

    # Claude Messages API 的 messages 数组。
    messages: list[dict[str, Any]]

    # 本次渲染实际应用的缓存断点位置（Claude messages[] 下标列表）。
    applied_cache_breakpoint_indexes: list[int]

    # 最近一条 Claude assistant 在统一 Conversation.messages 中的下标。
    last_claude_assistant_message_index: int | None = None


class ClaudeAdapter:
    """Claude Messages API 协议适配器。

    实现 ProviderAdapter 接口，负责 Claude 协议的归一化、渲染、预检。
    内置预注册到 adapter 注册表中。
    """

    @property
    def provider(self) -> str:
        """协议标识。"""
        return PROVIDER_CLAUDE

    # ── 归一化 ───────────────────────────────────────────────────

    def normalize_response(self, raw_response: Any) -> UniMessage:
        """将 Claude 原始响应归一化为 UniMessage。

        从响应的 content 数组中提取公共语义 parts（text / thinking / tool_use），
        同时将完整的 content 数组存入 provider_extras["anthropic_messages"]["raw"] 供同平台回灌。

        Claude 响应结构示例：
        {
            "content": [
                {"type": "thinking", "thinking": "...", "signature": "sig_..."},
                {"type": "text", "text": "..."}
            ]
        }
        """
        content: list[UniPart] = []

        for block in raw_response.get("content") or []:
            block_type = block.get("type")
            if block_type == "text":
                content.append(TextPart(text=block["text"]))
            elif block_type in ("thinking", "redacted_thinking"):
                content.append(ThinkingPart(text=block.get("thinking")))
            elif block_type == "tool_use":
                content.append(
                    ToolUsePart(
                        name=block["name"],
                        input=block.get("input"),
                        tool_use_id=block.get("id"),
                    )
                )
            # 其他块（citations 等）暂不解出公共语义，但保留在消息级 raw 中

        return UniMessage(
            role=ROLE_ASSISTANT,
            content=content,
            source_provider=PROVIDER_CLAUDE,
            provider_extras={
                PROVIDER_CLAUDE: {
                    "raw": raw_response.get("content"),  # 完整响应 content 数组，下轮下发原样回灌
                },
            },
        )

    # ── 渲染 ─────────────────────────────────────────────────────

    async def render(
        self,
        conv: Conversation,
        resolver: ResourceResolver,
        options: dict[str, Any] | None = None,
    ) -> ClaudeRenderResult:
        """将会话消息渲染为 Claude Messages API 的请求体。

        只产出消息相关字段：messages + appliedCacheBreakpointIndexes。
        模型名、max_tokens、thinking、system 等由调用方合并。
        """
        messages: list[dict[str, Any]] = []

        last_claude_assistant_message_index: int | None = None
        for source_index, m in enumerate(conv.messages):
            if m.role == "user":
                messages.append(await _build_claude_user(m, resolver))
            elif m.role == "tool":
                messages.append(_build_claude_tool(m))
            else:
                if m.source_provider == PROVIDER_CLAUDE:
                    last_claude_assistant_message_index = source_index
                messages.append(_build_claude_assistant(m, options))

        # 规范化：只保证首条消息满足 Claude 客户端请求约束，不合并相邻同 role。
        normalized = _ensure_first_claude_message_is_user(messages)

        # 缓存断点策略（动态打，不进 raw）：仅基于本次客户端输出 messages 边界打点。
        # 不复用历史 messages[] 下标，因为连续 user 消息不合并后，下标不再是稳定缓存锚点。
        applied_breakpoints = _attach_cache_breakpoints(normalized)

        return ClaudeRenderResult(
            messages=normalized,
            applied_cache_breakpoint_indexes=applied_breakpoints,
            last_claude_assistant_message_index=last_claude_assistant_message_index,
        )

    # ── 预检 ─────────────────────────────────────────────────────

    def preflight(self, conv: Conversation) -> None:
        """预检校验 Claude 的图片和 PDF 页数硬约束。

        Claude API 对单次请求中的图片数量和 PDF 总页数有硬性上限，
        超出时直接返回 400 错误。本方法在渲染前检查累计配额，
        超限时主动抛出错误以提供清晰的错误信息。
        """
        state = conv.state
        if state.image_count > _CLAUDE_IMAGE_LIMIT:
            raise ValueError(
                f"Claude 图片数 {state.image_count} 超过上限 {_CLAUDE_IMAGE_LIMIT}"
            )
        if state.pdf_page_count > _CLAUDE_PDF_PAGE_LIMIT:
            raise ValueError(
                f"Claude PDF 页数 {state.pdf_page_count} 超过上限 {_CLAUDE_PDF_PAGE_LIMIT}"
            )

    # ── 状态更新 ─────────────────────────────────────────────────

    def update_state(
        self,
        conv: Conversation,
        raw_response: Any,
        options: dict[str, Any] | None = None,
    ) -> None:
        """Claude 的会话状态更新。

        render 阶段产生的缓存断点由调用方写回；本方法在 assistant 响应追加后记录
        最近一次 Claude assistant 在统一消息列表中的位置，供后续缓存锚点策略使用。
        """
        if not conv.messages:
            return
        latest_message_index = len(conv.messages) - 1
        latest_message = conv.messages[latest_message_index]
        if latest_message.role != ROLE_ASSISTANT:
            return
        if latest_message.source_provider != PROVIDER_CLAUDE:
            return
        conv.provider_state[PROVIDER_CLAUDE] = {
            **(conv.provider_state.get(PROVIDER_CLAUDE) or {}),
            "last_claude_assistant_message_index": latest_message_index,
        }


# ── 渲染内部函数 ─────────────────────────────────────────────────

async def _build_claude_user(
    msg: UniMessage,
    resolver: ResourceResolver,
) -> dict[str, Any]:
    """将 user 消息转换为 Claude 格式。

    通过 ResourceResolver 将 resourceId 转为 base64 inline 内容。
    Claude 要求图片使用 base64 source，PDF 使用 document source。
    """
    content: list[dict[str, Any]] = []

    for p in msg.content:
        if p.kind == "text":
            content.append({"type": "text", "text": p.text})
        elif p.kind == "image":
            resource = await resolve_resource_access(resolver, p.resource_id)
            if resource.object_storage_url:
                source = {"type": "url", "url": resource.object_storage_url}
            else:
                data = _require_base64(resource.base64_content, p.resource_id)
                source = {
                    "type": "base64",
                    "media_type": resource.mime_type,
                    "data": data,
                }
            content.append({
                "type": "image",
                "source": source,
            })
        elif p.kind == "pdf":
            resource = await resolve_resource_access(resolver, p.resource_id)
            if resource.object_storage_url:
                source = {"type": "url", "url": resource.object_storage_url}
            else:
                data = _require_base64(resource.base64_content, p.resource_id)
                source = {
                    "type": "base64",
                    "media_type": resource.mime_type or "application/pdf",
                    "data": data,
                }
            content.append({
                "type": "document",
                "source": source,
            })

    return {"role": "user", "content": content}


def _require_base64(base64_content: str | None, resource_id: str) -> str:
    if not base64_content:
        raise ValueError(f"资源缺少公网 URL 和 Base64 内容: {resource_id}")
    return base64_content


def _build_claude_tool(msg: UniMessage) -> dict[str, Any]:
    """将 tool 消息转换为 Claude 格式。

    Claude API 中 tool_result 必须放在 user role 的 content 中。
    """
    content: list[dict[str, Any]] = []

    for p in msg.content:
        if p.kind == "tool_result":
            content.append({
                "type": "tool_result",
                "tool_use_id": p.tool_use_ref,
                "content": serialize_tool_output(p.output),
            })

    return {"role": "user", "content": content}


def _build_claude_assistant(
    msg: UniMessage,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """将 assistant 消息转换为 Claude 格式。

    同平台（source_provider == "anthropic_messages"）：原样回灌 raw（无损，含 signature）。
    跨平台：公共层降级，保留 text 和 tool_use，不伪造目标协议 thinking。
    """
    # 同平台无损回灌
    if msg.source_provider == PROVIDER_CLAUDE:
        extras = (msg.provider_extras or {}).get(PROVIDER_CLAUDE)
        if extras and extras.get("raw") is not None:
            return {"role": "assistant", "content": clone_raw_value(extras["raw"])}

    include_thinking_text = should_include_cross_protocol_thinking_text(options)
    content: list[dict[str, Any]] = []
    for segment in iter_assistant_semantic_segments(
        msg,
        include_thinking_text=include_thinking_text,
    ):
        if isinstance(segment, AssistantTextSegment):
            content.append({"type": "text", "text": segment.text})
        elif isinstance(segment, AssistantToolUseSegment):
            content.append({
                "type": "tool_use",
                "id": segment.tool_use_ref,
                "name": segment.name,
                "input": segment.input,
            })
    if not content:
        content.append({"type": "text", "text": "(empty)"})
    return {
        "role": "assistant",
        "content": content,
    }


def _ensure_first_claude_message_is_user(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """确保 Claude 请求 messages 首条为 user，不改变其它客户端消息边界。"""
    if not messages:
        return messages

    # 确保首条是 user
    if messages[0].get("role") != "user":
        messages.insert(0, {
            "role": "user",
            "content": [{"type": "text", "text": "(placeholder)"}],
        })

    return messages


def _attach_cache_breakpoints(
    messages: list[dict[str, Any]],
) -> list[int]:
    """在消息列表上附加缓存断点。

    当前只在本次请求最新消息末尾打动态断点，并严格限制 messages 内断点数量不超过 4。
    系统提示词和工具定义的静态缓存断点由请求客户端在对应字段上单独管理。

    返回本次实际应用的全部断点位置列表。
    """
    applied: list[int] = []
    msg_count = len(messages)

    breakpoint_indexes: list[int] = []
    if msg_count >= 1:
        breakpoint_indexes.append(msg_count - 1)

    # 在对应位置的最后一个 content block 上打 cache_control
    for idx in breakpoint_indexes[:4]:
        msg = messages[idx]
        content = msg.get("content")
        if isinstance(content, list) and len(content) > 0:
            content[-1]["cache_control"] = {"type": "ephemeral"}
            applied.append(idx)

    return applied

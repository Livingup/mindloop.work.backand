"""Claude 协议适配器。

该模块实现 Claude Messages API 的 ProviderAdapter，负责：
- 归一化：将 Claude 原始响应 → UniMessage（公共语义层 + providerExtras.raw）
- 渲染：将 Conversation.messages[] → Claude 请求体（messages + 缓存断点）
- 预检：校验 Claude 图片 ≤100 / PDF 页数 ≤100 的硬约束
- 状态更新：当前由调用方在 API 成功后手动写回缓存断点（本 adapter 不自动维护）

渲染决策树：
- user 消息：通过 ResourceResolver 将 resourceId → base64 inline
- assistant 消息（同平台）：原样回灌 providerExtras.claude.raw（含 signature，无损）
- assistant 消息（跨平台）：公共层降级，只取 text part 合并
- tool 消息：转为 Claude 的 tool_result 格式，合并进 user role

关键约束：
- messages[0].role 必须是 "user"；首条非 user 时插占位
- user/assistant 严格交替；相邻同 role 合并 content
- 跨平台降级空 text 用 "(empty)" 占位
- 缓存断点（cache_control）不进 raw，渲染时动态打
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_runtime.uni_llm.message.models import (
    PROVIDER_CLAUDE,
    ROLE_ASSISTANT,
    Conversation,
    TextPart,
    ThinkingPart,
    ToolUsePart,
    UniMessage,
    UniPart,
)
from agent_runtime.uni_llm.south_gateway.resource_resolver.contracts import (
    ResourceResolver,
)

# Claude 硬约束上限（取 200K 模型的最紧值）。
_CLAUDE_IMAGE_LIMIT = 100
_CLAUDE_PDF_PAGE_LIMIT = 100


@dataclass(frozen=True, slots=True)
class ClaudeRenderResult:
    """Claude 渲染结果。

    包含渲染后的 messages 数组和本次实际应用的缓存断点位置。
    调用方在 API 调用成功后应将 applied_cache_breakpoint_indexes
    写回 conv.provider_state["claude"]["cache_breakpoint_indexes"]，
    供下次渲染时复用缓存前缀。
    """

    # Claude Messages API 的 messages 数组。
    messages: list[dict[str, Any]]

    # 本次渲染实际应用的缓存断点位置（messages[] 下标列表）。
    applied_cache_breakpoint_indexes: list[int]


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
        同时将完整的 content 数组存入 provider_extras["claude"]["raw"] 供同平台回灌。

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
                content.append(ToolUsePart(name=block["name"], input=block.get("input")))
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

        for m in conv.messages:
            if m.role == "user":
                messages.append(await _build_claude_user(m, resolver))
            elif m.role == "tool":
                messages.append(_build_claude_tool(m))
            else:
                messages.append(_build_claude_assistant(m))

        # 规范化：确保 messages[0].role == "user"，相邻同 role 合并
        normalized = _normalize_claude_messages(messages)

        # 缓存断点策略（动态打，不进 raw）
        # 累积策略：在历史断点位置 + 新位置都打断点，确保跨平台切换后前缀缓存可复用
        claude_state = conv.provider_state.get(PROVIDER_CLAUDE) or {}
        history_breakpoints = claude_state.get("cache_breakpoint_indexes") or []
        applied_breakpoints = _attach_cache_breakpoints(normalized, history_breakpoints)

        return ClaudeRenderResult(
            messages=normalized,
            applied_cache_breakpoint_indexes=applied_breakpoints,
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

        当前 Claude 的缓存断点需要调用方在 API 成功后手动写回
        （因为断点信息来自 render 结果而非响应），本方法暂不自动维护。
        """


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
            meta = await resolver.get_resource_meta(p.resource_id)
            data = await resolver.get_base64(p.resource_id)
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": meta.mime_type, "data": data},
            })
        elif p.kind == "pdf":
            data = await resolver.get_base64(p.resource_id)
            content.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": data},
            })

    return {"role": "user", "content": content}


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
                "content": str(p.output),
            })

    return {"role": "user", "content": content}


def _build_claude_assistant(msg: UniMessage) -> dict[str, Any]:
    """将 assistant 消息转换为 Claude 格式。

    同平台（source_provider == "claude"）：原样回灌 raw（无损，含 signature）。
    跨平台：公共层降级，只合并 text part（丢 thinking / tool_*）。
    """
    # 同平台无损回灌
    if msg.source_provider == PROVIDER_CLAUDE:
        extras = (msg.provider_extras or {}).get(PROVIDER_CLAUDE)
        if extras and extras.get("raw") is not None:
            return {"role": "assistant", "content": extras["raw"]}

    # 跨平台降级：只合并 text part
    text = "\n".join(
        p.text for p in msg.content if p.kind == "text" and hasattr(p, "text")
    )
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text or "(empty)"}],
    }


def _normalize_claude_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """规范化 Claude 消息列表。

    确保满足 Claude API 的约束：
    1. messages[0].role 必须是 "user"
    2. 相邻同 role 消息合并 content
    """
    if not messages:
        return messages

    # 确保首条是 user
    if messages[0].get("role") != "user":
        messages.insert(0, {
            "role": "user",
            "content": [{"type": "text", "text": "(placeholder)"}],
        })

    # 相邻同 role 合并
    merged: list[dict[str, Any]] = [messages[0]]
    for msg in messages[1:]:
        if msg.get("role") == merged[-1].get("role"):
            prev_content = merged[-1].get("content", [])
            curr_content = msg.get("content", [])
            if isinstance(prev_content, list) and isinstance(curr_content, list):
                prev_content.extend(curr_content)
            elif isinstance(prev_content, str) and isinstance(curr_content, str):
                merged[-1]["content"] = prev_content + "\n" + curr_content
        else:
            merged.append(msg)

    return merged


def _attach_cache_breakpoints(
    messages: list[dict[str, Any]],
    history_breakpoints: list[int],
) -> list[int]:
    """在消息列表上附加缓存断点。

    累积策略：在历史断点位置 + 新断点位置都打 cache_control，
    确保跨平台切换后前缀缓存可复用。

    新断点默认打在倒数第二条消息（若存在），使前缀部分尽可能被缓存。

    返回本次实际应用的全部断点位置列表。
    """
    applied: list[int] = []
    msg_count = len(messages)

    # 收集所有需要打断点的位置
    breakpoint_indexes: set[int] = set()

    # 历史断点
    for idx in history_breakpoints:
        if 0 <= idx < msg_count:
            breakpoint_indexes.add(idx)

    # 新断点：倒数第二条消息（如果有至少两条消息）
    if msg_count >= 2:
        breakpoint_indexes.add(msg_count - 2)

    # 在对应位置的最后一个 content block 上打 cache_control
    for idx in sorted(breakpoint_indexes):
        msg = messages[idx]
        content = msg.get("content")
        if isinstance(content, list) and len(content) > 0:
            content[-1]["cache_control"] = {"type": "ephemeral"}
            applied.append(idx)

    return applied

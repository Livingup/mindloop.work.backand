"""OpenAI Responses API 协议适配器。

该模块实现 OpenAI Responses API 的 ProviderAdapter，负责：
- 归一化：将 OpenAI 原始响应 → UniMessage（公共语义层 + providerExtras.raw）
- 渲染：将 Conversation.messages[] → OpenAI 请求体（input + checkpoint 相关字段）
- 状态更新：API 成功后记录 checkpoint（lastResponseId / lastMessageIndex / promptCacheKey）

渲染决策树：
- user 消息：通过 ResourceResolver 将 resourceId → data URI / input_file
- assistant 消息（同平台）：展开 output items 到顶层 input（含 reasoning / message / function_call）
- assistant 消息（跨平台）：合成一条 assistant message item（降级）
- tool 消息：展开为顶层 function_call_output item

stored 参数由调用方传入，决定本次请求是否走 checkpoint 模式：
- stored=true 且有 checkpoint：带 previous_response_id，只发 gap 消息
- stored=true 但无 checkpoint（首次）：全量展开
- stored=false：全量展开，忽略 checkpoint
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.uni_llm.message.models import (
    PROVIDER_OPENAI,
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


@dataclass(frozen=True, slots=True)
class OpenAIRenderResult:
    """OpenAI 渲染结果。

    包含渲染后的 input 数组和 checkpoint 相关字段。
    调用方在此基础上合并 model、instructions、reasoning、store 等参数后发送请求。

    注意：store / instructions 不在此处，由调用方自行决定并合并。
    """

    # OpenAI Responses API 的 input 数组。
    input: list[Any]

    # stored=true 且有 checkpoint 时带上，走 gap 模式。
    previous_response_id: str | None = None

    # 上次记录的 cache key，带上以命中前缀缓存。
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
        存入 provider_extras["openai"]["raw"]。

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
                    ToolUsePart(name=item.get("name", ""), input=parsed_input)
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

        只产出消息相关字段：input + previous_response_id? + prompt_cache_key? + include。
        模型名、instructions、reasoning、store 等由调用方合并。

        stored 参数通过 options["stored"] 传入，控制 checkpoint 行为：
        - stored=True 且有 checkpoint：带 previous_response_id，只发 gap 消息
        - stored=True 但无 checkpoint / stored=False：全量展开
        """
        opts = options or {}
        stored = opts.get("stored", False)

        oai_state = conv.provider_state.get(PROVIDER_OPENAI) or {}
        last_response_id = oai_state.get("last_response_id")
        last_message_index = oai_state.get("last_message_index")
        prompt_cache_key = oai_state.get("prompt_cache_key")

        # stored=True 且有记录的 checkpoint 才走 gap 模式
        use_checkpoint = (
            stored
            and last_response_id is not None
            and last_message_index is not None
        )

        # 决定从哪条消息开始发送
        start_index = (last_message_index + 1) if use_checkpoint else 0
        message_slice = conv.messages[start_index:]

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
                            "output": str(p.output),
                        })
            else:
                # assistant 消息
                input_items.extend(_build_openai_assistant(m))

        return OpenAIRenderResult(
            input=input_items,
            previous_response_id=last_response_id if use_checkpoint else None,
            prompt_cache_key=prompt_cache_key if prompt_cache_key else None,
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
        """OpenAI store=true 时记录 checkpoint。

        将响应中的 id（作为 last_response_id）、当前消息索引
        （作为 last_message_index）和 prompt_cache_key 写入
        provider_state["openai"]，供下次 store=true 渲染时走 gap 模式。

        store=false 时不更新 checkpoint。
        """
        opts = options or {}
        stored = opts.get("stored", False)
        if not stored:
            return

        conv.provider_state[PROVIDER_OPENAI] = {
            **(conv.provider_state.get(PROVIDER_OPENAI) or {}),
            "last_response_id": raw_response.get("id"),
            "last_message_index": len(conv.messages) - 1,
            "prompt_cache_key": raw_response.get("prompt_cache_key"),
        }


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
            meta = await resolver.get_resource_meta(p.resource_id)
            data = await resolver.get_base64(p.resource_id)
            content.append({
                "type": "input_image",
                "image_url": f"data:{meta.mime_type};base64,{data}",
            })
        elif p.kind == "pdf":
            data = await resolver.get_base64(p.resource_id)
            content.append({
                "type": "input_file",
                "file_data": f"data:application/pdf;base64,{data}",
            })

    return {"role": "user", "content": content}


def _build_openai_assistant(msg: UniMessage) -> list[Any]:
    """将 assistant 消息转换为 OpenAI Responses API 格式。

    同平台（source_provider == "openai"）：展开 output items 到顶层 input
    （含 reasoning / message / function_call，无损）。
    跨平台：合成一条 assistant message item（降级）。

    返回列表是因为同平台回灌时一条 UniMessage 可展开为多个 input items。
    """
    # 同平台无损回灌：展开 output items
    if msg.source_provider == PROVIDER_OPENAI:
        extras = (msg.provider_extras or {}).get(PROVIDER_OPENAI)
        if extras and extras.get("raw") is not None:
            return list(extras["raw"])

    # 跨平台降级：合成一条 assistant message item
    text = "\n".join(
        p.text for p in msg.content if p.kind == "text" and hasattr(p, "text")
    )
    return [{
        "role": "assistant",
        "content": [{"type": "output_text", "text": text or "(empty)"}],
    }]

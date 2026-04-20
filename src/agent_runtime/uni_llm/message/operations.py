"""统一 LLM 消息领域操作。

该模块提供消息领域模型的核心领域能力函数，包括：
- 协议适配器注册表（静态预定义，模块加载时一次性就绪）
- 会话创建（create_conversation）
- 追加 user 消息（add_user_message）
- 渲染消息（render_messages）—— 通过注册表分发到目标平台
- 接收响应（ingest_response）—— 归一化 → 追加消息 → 更新会话状态

这些是消息领域模型对内提供的领域能力，将来由上层统一 LLM 调用服务编排使用。
本层只负责消息相关字段，不关心 model / temperature / max_tokens 等参数。

设计要点：
- 统一分发：render_messages / ingest_response 通过注册表查找 adapter，
  所有平台走同一条路径，无 switch/case。
- 全平台静态预定义：所有支持的平台在代码层定义，模块加载时全部就绪。
  接入新平台 = 实现 ProviderAdapter + 加入 _ADAPTERS 列表。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from agent_runtime.uni_llm.message.claude_protocol import ClaudeAdapter
from agent_runtime.uni_llm.message.gemini_protocol import GeminiAdapter
from agent_runtime.uni_llm.message.openai_protocol import OpenAIAdapter
from agent_runtime.uni_llm.message.models import (
    Conversation,
    ConversationState,
    PartMeta,
    Provider,
    ProviderAdapter,
    UniMessage,
    UniPart,
)
from agent_runtime.uni_llm.south_gateway.resource_resolver.contracts import (
    ResourceResolver,
)

logger = logging.getLogger(__name__)


# ── 协议适配器注册表（静态预定义） ─────────────────────────────────
# 所有支持的平台在此列表中声明。接入新平台：
#   1. 实现 ProviderAdapter
#   2. 加入 _ADAPTERS 列表
# 模块加载时一次性构建 registry，运行期间只读。

_ADAPTERS: list[ProviderAdapter] = [
    ClaudeAdapter(),
    GeminiAdapter(),
    OpenAIAdapter(),
]

_adapter_registry: dict[str, ProviderAdapter] = {
    a.provider: a for a in _ADAPTERS
}


def get_supported_providers() -> list[str]:
    """Return all supported provider identifiers."""
    return list(_adapter_registry.keys())


def _get_adapter(provider: str) -> ProviderAdapter:
    adapter = _adapter_registry.get(provider)
    if adapter is None:
        raise ValueError(
            f'Unsupported provider "{provider}". '
            f"Supported: {list(_adapter_registry.keys())}"
        )
    return adapter


# ── 会话创建 ─────────────────────────────────────────────────────

def create_conversation() -> Conversation:
    """创建一个空的统一 LLM 会话。

    会话是消息领域的聚合根，包含 append-only 的历史消息列表、
    各平台的会话运行态和由消息列表派生的统计状态。
    """
    return Conversation(
        id=str(uuid.uuid4()),
        messages=[],
        state=ConversationState(),
        provider_state={},
    )


# ── 追加 user 消息 ──────────────────────────────────────────────

def add_user_message(
    conv: Conversation,
    parts: list[UniPart],
    parts_meta: list[PartMeta],
) -> None:
    """Delegate to Conversation aggregate root."""
    conv.add_user_message(parts, parts_meta)


# ── 渲染消息 ─────────────────────────────────────────────────────

async def render_messages(
    conv: Conversation,
    target: Provider,
    resolver: ResourceResolver,
    options: dict[str, Any] | None = None,
) -> Any:
    """渲染消息 → 目标平台请求体（仅消息相关字段）。

    通过 adapter 注册表动态分发，内置三家和第三方走同一条路径。
    渲染前会调用 adapter 的 preflight 进行预检校验。

    返回值的具体类型由各 adapter 定义（如 ClaudeRenderResult、
    GeminiRenderResult、OpenAIRenderResult），调用方在此基础上
    合并 model / temperature / max_tokens 等参数后发送请求。

    参数说明：
    - target: 目标协议标识（如 "claude" / "deepseek"）
    - resolver: 资源解析器，按需将 resourceId → base64/URL
    - options: 透传给 adapter.render()，各 adapter 自定义
      （如 OpenAI 的 {"stored": True}）
    """
    adapter = _get_adapter(target)

    # 预检校验（如 Claude 图片/PDF 配额）
    adapter.preflight(conv)

    return await adapter.render(conv, resolver, options)


# ── 接收响应 ─────────────────────────────────────────────────────

def ingest_response(
    conv: Conversation,
    provider: Provider,
    raw_response: Any,
    options: dict[str, Any] | None = None,
) -> UniMessage:
    """接收响应 → 归一化 → 追加到会话 → 更新 providerState。

    通过 adapter 注册表动态分发。完整流程：
    1. adapter.normalize_response：将原始响应归一化为 UniMessage
    2. 设置 source_vendor（如有）
    3. append_message：追加到会话并增量更新派生状态
    4. adapter.update_state：更新 providerState（如 OpenAI 记录 checkpoint）

    参数说明：
    - provider: 响应来源协议标识（如 "claude" / "deepseek"）
    - raw_response: LLM API 原始响应（JSON 解析后的 dict）
    - options: 可选参数，支持：
      - vendor: 实际服务商标识（如 "openrouter"），记录到 source_vendor
      - 其他字段透传给 adapter.update_state（如 OpenAI 的 {"stored": True}）
    """
    adapter = _get_adapter(provider)

    # 归一化：原始响应 → UniMessage
    msg = adapter.normalize_response(raw_response)

    # 设置实际服务商标识（元数据）
    opts = options or {}
    if opts.get("vendor"):
        msg.source_vendor = opts["vendor"]

    # Delegate to aggregate root
    conv.append_message(msg)

    # 调用 adapter 的状态更新（如 OpenAI 记录 checkpoint）
    adapter.update_state(conv, raw_response, options)

    return msg

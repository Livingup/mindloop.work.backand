"""recompute_state: full rebuild of ConversationState from messages.

Only used for data migration or repair. Normal flow uses
Conversation.append_message() for incremental updates.
"""
from __future__ import annotations

from typing import Any

from agent_runtime.conversation_message.message.models import (
    MODALITY_IMAGE,
    MODALITY_PDF,
    MODALITY_TEXT,
    ConversationState,
    UniMessage,
)


async def recompute_state(
    msgs: list[UniMessage],
    resolver: Any,
) -> ConversationState:
    """从消息列表全量重建会话派生状态。

    该函数是异步操作，需要通过 resolver 查询 PDF 资源的页数信息。
    仅用于数据迁移或修复场景，正常流程走 append_message 增量更新。
    """
    state = ConversationState()

    providers: set[str] = set()
    vendors: set[str] = set()
    modalities: set[str] = set()
    image_count = 0
    pdf_page_count = 0

    for m in msgs:
        # 仅 assistant 消息有值
        if m.source_provider:
            providers.add(m.source_provider)
        if m.source_vendor:
            vendors.add(m.source_vendor)

        for p in m.content:
            if p.kind == "text":
                modalities.add(MODALITY_TEXT)
            elif p.kind == "image":
                modalities.add(MODALITY_IMAGE)
                image_count += 1
            elif p.kind == "pdf":
                modalities.add(MODALITY_PDF)
                meta = await resolver.get_resource_meta(p.resource_id)
                if meta is not None and meta.resource_category == "pdf":
                    pdf_page_count += meta.pdf_page_count or 0
            # thinking / tool_* 不计入模态或配额

    state.providers = list(providers)
    state.vendors = list(vendors)
    state.modalities = list(modalities)
    state.image_count = image_count
    state.pdf_page_count = pdf_page_count
    return state

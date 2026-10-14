# -*- coding: utf-8 -*-
"""Task Note 起算范围（计数、观察切片、替换尾部共用的唯一规则）。

起算范围 = 起点消息中合并进来的原文（content[1:]）+ 起点之后到当前最后一条消息。

起点解析顺序：
1. covered_until_message_id，没有则 prefix_end_message_id；
2. 起点 ID 不在当前消息中时，回退到最后一条首文本块以“# 任务手记快照”开头的消息；
3. 仍找不到时从第 0 条开始。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from agent_runtime.task_note.task_note_unit_slicer import estimate_message_tokens

logger = logging.getLogger(__name__)

SNAPSHOT_HEADER_PREFIX = "# 任务手记快照"


def get_msg_id(msg: Any) -> str | None:
    if msg is None:
        return None
    if isinstance(msg, dict):
        return str(msg.get("message_id") or msg.get("id") or "") or None
    mid = getattr(msg, "message_id", None) or getattr(msg, "id", None)
    return str(mid) if mid else None


def _get_content(msg: Any) -> Any:
    if msg is None:
        return None
    if isinstance(msg, dict):
        return msg.get("content")
    return getattr(msg, "content", None)


def is_snapshot_header(msg: Any) -> bool:
    """消息首个文本块是否以快照标题开头。"""
    content = _get_content(msg)
    if isinstance(content, list) and content:
        first = content[0]
        text = first.get("text", "") if isinstance(first, dict) else getattr(first, "text", "")
        return str(text or "").startswith(SNAPSHOT_HEADER_PREFIX)
    if isinstance(content, str):
        return content.startswith(SNAPSHOT_HEADER_PREFIX)
    return False


def build_merged_head(msg: Any) -> Any | None:
    """快照消息与 user 消息合并时，取出合并进来的原文（content[1:]），沿用原消息 ID。

    非快照消息或只含快照文本时返回 None。
    """
    if not is_snapshot_header(msg):
        return None
    content = _get_content(msg)
    if not isinstance(content, list) or len(content) <= 1:
        return None
    tail_content = list(content[1:])
    mid = get_msg_id(msg)
    if isinstance(msg, dict):
        head = dict(msg)
        head["role"] = "user"
        head["content"] = tail_content
        return head
    from agent_runtime.conversation_message.message.models import ROLE_USER, UniMessage

    return UniMessage(role=ROLE_USER, content=tail_content, message_id=mid)


@dataclass(slots=True)
class RangeStart:
    """起算范围解析结果。"""

    # 实际生效的起点消息 ID（已完成回退）；None 表示从第 0 条开始
    start_message_id: str | None
    # 起点之后第一条完整消息的下标
    start_idx: int
    # 起点消息中合并进来的原文（content[1:]），没有则为 None
    merged_head: Any | None
    # 起点消息是否为快照消息
    start_is_snapshot: bool


def resolve_range_start(
    messages: Sequence[Any],
    *,
    covered_until_message_id: str | None,
    prefix_end_message_id: str | None,
) -> RangeStart:
    """按统一规则解析起算范围起点。"""
    start_mid = covered_until_message_id or prefix_end_message_id
    msg_ids = [get_msg_id(m) for m in messages]
    start_pos = -1
    if start_mid:
        for idx, mid in enumerate(msg_ids):
            if mid == start_mid:
                start_pos = idx
                break
        if start_pos == -1:
            for idx in range(len(messages) - 1, -1, -1):
                if is_snapshot_header(messages[idx]):
                    start_pos = idx
                    break
            logger.warning(
                "起点消息 %s 不在当前消息序列中，回退至快照消息 %s",
                start_mid,
                msg_ids[start_pos] if start_pos != -1 else None,
            )
    if start_pos == -1:
        return RangeStart(start_message_id=None, start_idx=0, merged_head=None, start_is_snapshot=False)
    start_msg = messages[start_pos]
    return RangeStart(
        start_message_id=msg_ids[start_pos],
        start_idx=start_pos + 1,
        merged_head=build_merged_head(start_msg),
        start_is_snapshot=is_snapshot_header(start_msg),
    )


def range_messages(messages: Sequence[Any], start: RangeStart) -> list[Any]:
    """返回起算范围内的消息：合并原文（如有）+ 起点之后的全部消息。"""
    result: list[Any] = []
    if start.merged_head is not None:
        result.append(start.merged_head)
    result.extend(messages[start.start_idx:])
    return result


def estimate_range_tokens(messages: Sequence[Any], start: RangeStart) -> int:
    """估算起算范围的 token：每轮全量重算，只用 estimate_message_tokens，不用 usage。

    范围内再出现的快照消息只计合并进来的原文，纯快照不计。
    """
    total = 0
    if start.merged_head is not None:
        total += estimate_message_tokens(start.merged_head)
    for msg in messages[start.start_idx:]:
        if is_snapshot_header(msg):
            head = build_merged_head(msg)
            if head is not None:
                total += estimate_message_tokens(head)
            continue
        total += estimate_message_tokens(msg)
    return total

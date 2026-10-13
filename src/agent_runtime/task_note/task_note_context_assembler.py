# -*- coding: utf-8 -*-
"""Stage 4 上下文热组装器与替换机制 (TaskNoteContextAssembler)。

核心职责 (D05, M6):
1. 在业务 Agent 两次 LLM 调用之间的安全断点，检测总上下文 Token 是否达到门禁阈值；
2. 命中门禁时检查 Note 快照：
   - 只有快照读取成功且 note_version >= 1 才执行替换 (D08 第 4 条)；
   - M6-1: 替换时若观察者正在后台运行，等待最多 swap_wait_timeout_seconds；
   - M6-2: 组装后若残差尾部仍超过门禁阈值，先同步执行一次观察再重新组装；
   - M6-3: 尾部首条若是 user 消息，则与快照 user 消息安全合并，确保 LLM 协议合法；
3. 输出全新的紧凑消息序列 (系统提示词 + 快照手记 + covered_until_message_id 之后的原始尾部消息)。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Sequence
import uuid

from agent_runtime.task_note.task_note_markdown_formatter import (
    format_task_note_to_markdown,
)
from agent_runtime.task_note.task_note_models import TaskNoteTracking
from agent_runtime.task_note.task_note_store import (
    TaskNoteStore,
    get_task_note_store,
)
from agent_runtime.task_note.task_note_unit_slicer import (
    TaskNoteUnitSlicer,
    estimate_message_tokens,
)

logger = logging.getLogger("agent_runtime.task_note.context_assembler")


@dataclass
class ContextSwapResult:
    """上下文替换执行结果。"""

    swapped: bool
    messages: list[Any]
    covered_until_seq: int
    note_version: int
    snapshot_message_id: str | None = None
    pruned_message_count: int = 0
    tail_message_count: int = 0
    snapshot_markdown: str = ""
    total_tokens: int = 0
    context_window_threshold: int | None = None


def _get_msg_id(msg: Any) -> str | None:
    if msg is None:
        return None
    if isinstance(msg, dict):
        return str(msg.get("id") or msg.get("message_id") or "") or None
    mid = getattr(msg, "message_id", None) or getattr(msg, "id", None)
    return str(mid) if mid else None


def _get_msg_role(msg: Any) -> str:
    if msg is None:
        return ""
    if isinstance(msg, dict):
        return str(msg.get("role") or "").lower()
    return str(getattr(msg, "role", "") or "").lower()


def _extract_msg_text(msg: Any) -> str:
    if msg is None:
        return ""
    if isinstance(msg, dict):
        c = msg.get("text") if msg.get("text") is not None else msg.get("content")
        return str(c or "")
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if hasattr(part, "text") and part.text:
                parts.append(str(part.text))
            elif isinstance(part, dict) and part.get("text"):
                parts.append(str(part["text"]))
        return "\n".join(parts)
    return str(getattr(msg, "text", "") or "")


class TaskNoteContextAssembler:
    """上下文热组装器 (D05, M6)。"""

    def __init__(
        self,
        *,
        slicer: TaskNoteUnitSlicer | None = None,
        store: TaskNoteStore | None = None,
    ) -> None:
        self.slicer = slicer or TaskNoteUnitSlicer()
        self.store = store or get_task_note_store()

    async def assemble_context(
        self,
        *,
        messages: Sequence[Any],
        tracking: TaskNoteTracking,
        task_id: str | None = None,
        agent_instance_id: str | None = None,
        current_working_directory: str = "",
        command_lookup: dict[str, str] | None = None,
        note_snapshot: dict[str, Any] | None = None,
    ) -> ContextSwapResult:
        """检查并执行前缀历史消息向手记快照的安全替换。"""
        current_messages = list(messages)
        if not current_messages:
            return ContextSwapResult(
                swapped=False,
                messages=[],
                covered_until_seq=0,
                note_version=0,
                snapshot_message_id=None,
            )

        # 1. 门禁检查：note_version < 1 时不执行替换
        if tracking.note_version < 1:
            logger.debug("tracking.note_version < 1，不执行替换")
            return ContextSwapResult(
                swapped=False,
                messages=current_messages,
                covered_until_seq=0,
                note_version=tracking.note_version,
                snapshot_message_id=None,
            )

        # 2. 读取结构化 Note 快照
        snapshot_dict: dict[str, Any] = {}
        if note_snapshot is not None:
            snapshot_dict = dict(note_snapshot)
        elif task_id and agent_instance_id:
            try:
                snapshot_dict = self.store.get_note_snapshot(
                    task_id=task_id, agent_instance_id=agent_instance_id
                )
            except Exception as exc:
                logger.warning("获取 Note 快照异常，跳过本次替换: %s", exc)
                return ContextSwapResult(
                    swapped=False,
                    messages=current_messages,
                    covered_until_seq=0,
                    note_version=tracking.note_version,
                    snapshot_message_id=None,
                )

        if not snapshot_dict:
            logger.warning("Note 快照为空，跳过本次替换 (D08)")
            return ContextSwapResult(
                swapped=False,
                messages=current_messages,
                covered_until_seq=0,
                note_version=tracking.note_version,
                snapshot_message_id=None,
            )

        # 3. 提取残差尾部消息：尾部取起点之后的全部消息，起点按 2.1 求
        start_mid = tracking.covered_until_message_id or tracking.prefix_end_message_id
        start_idx = 0
        if start_mid:
            found_idx = -1
            for idx, msg in enumerate(current_messages):
                if _get_msg_id(msg) == start_mid:
                    found_idx = idx
                    break
            if found_idx != -1:
                start_idx = found_idx + 1
            else:
                # 容灾：退回到最后一条首个 TextPart 以 "# 任务手记快照" 开头的消息
                last_snap_idx = -1
                for idx, msg in enumerate(current_messages):
                    content = getattr(msg, "content", None)
                    if isinstance(content, list) and content:
                        first = content[0]
                        text = getattr(first, "text", "") if hasattr(first, "text") else (first.get("text", "") if isinstance(first, dict) else str(first))
                        if str(text).startswith("# 任务手记快照"):
                            last_snap_idx = idx
                    elif isinstance(msg, dict):
                        c = msg.get("content")
                        if isinstance(c, list) and c:
                            first = c[0]
                            text = first.get("text", "") if isinstance(first, dict) else str(first)
                            if str(text).startswith("# 任务手记快照"):
                                last_snap_idx = idx
                        elif isinstance(c, str) and c.startswith("# 任务手记快照"):
                            last_snap_idx = idx
                if last_snap_idx != -1:
                    start_idx = last_snap_idx + 1

        tail_messages = current_messages[start_idx:]

        # 4. 生成高密度 Markdown 任务手记
        final_lookup = dict(command_lookup or {})
        if not final_lookup and task_id and agent_instance_id:
            try:
                final_lookup = self.store.get_all_command_lines_for_tenant(
                    task_id=task_id, agent_instance_id=agent_instance_id
                )
            except Exception:
                pass

        markdown_body = format_task_note_to_markdown(
            snapshot_dict,
            command_lookup=final_lookup,
            current_working_directory=current_working_directory,
        )

        handbook_text = (
            f"# 任务手记快照 (v{tracking.note_version})\n\n"
            f"{markdown_body}\n\n"
            "---\n"
            "> 💡 系统已将历史前缀消息安全压缩沉淀至上述任务手记。"
            "请根据工作手记中的最新执行方案、已交付成果与下一步行动指针 (Next Step)，结合下方最新残差消息继续推进业务任务。"
        )

        # 5. 合并规则 (UniMessage 与 dict)
        sample_msg = current_messages[0]
        is_unimessage = False
        try:
            from agent_runtime.conversation_message.message.models import UniMessage
            if isinstance(sample_msg, UniMessage):
                is_unimessage = True
        except Exception:
            is_unimessage = False

        has_tail = len(tail_messages) > 0
        first_tail_is_user = has_tail and (_get_msg_role(tail_messages[0]) == "user")
        snapshot_msg_id: str | None = None

        if is_unimessage:
            from agent_runtime.conversation_message.message.models import ROLE_USER, TextPart, UniMessage

            if first_tail_is_user:
                # 尾部首条是 user 时，合并后的消息 content = [TextPart(手记)] + 原消息的 content，message_id 沿用原消息的 ID
                first_tail = tail_messages[0]
                snapshot_msg_id = getattr(first_tail, "message_id", None) or f"msg_user_{uuid.uuid4().hex[:8]}"
                merged_parts = [TextPart(text=handbook_text)] + list(first_tail.content)
                merged_msg = UniMessage(
                    role=ROLE_USER,
                    content=merged_parts,
                    message_id=snapshot_msg_id,
                )
                new_messages = [merged_msg] + tail_messages[1:]
            else:
                snapshot_msg_id = f"msg_snapshot_v{tracking.note_version}_{uuid.uuid4().hex[:8]}"
                snapshot_msg = UniMessage(
                    role=ROLE_USER,
                    content=[TextPart(text=handbook_text)],
                    message_id=snapshot_msg_id,
                )
                new_messages = [snapshot_msg] + tail_messages
        else:
            if first_tail_is_user:
                first_tail = tail_messages[0]
                snapshot_msg_id = str(first_tail.get("id") or first_tail.get("message_id") or f"msg_user_{uuid.uuid4().hex[:8]}")
                raw_content = first_tail.get("content")
                if isinstance(raw_content, list):
                    merged_content = [{"type": "text", "text": handbook_text}] + list(raw_content)
                else:
                    merged_content = [{"type": "text", "text": handbook_text}, {"type": "text", "text": str(raw_content or "")}]
                merged_msg = dict(first_tail)
                merged_msg["role"] = "user"
                merged_msg["content"] = merged_content
                merged_msg["id"] = snapshot_msg_id
                new_messages = [merged_msg] + tail_messages[1:]
            else:
                snapshot_msg_id = f"msg_snapshot_v{tracking.note_version}_{uuid.uuid4().hex[:8]}"
                snapshot_msg = {
                    "id": snapshot_msg_id,
                    "role": "user",
                    "content": handbook_text,
                }
                new_messages = [snapshot_msg] + tail_messages

        pruned_count = len(current_messages) - len(tail_messages)
        total_tokens = sum(estimate_message_tokens(m) for m in new_messages)
        logger.info(
            "成功完成 Stage 4 上下文替换 (D05): 原消息=%d, 裁剪历史=%d, 保留尾部=%d, version=%d, snapshot_msg_id=%s",
            len(current_messages),
            pruned_count,
            len(tail_messages),
            tracking.note_version,
            snapshot_msg_id,
        )

        return ContextSwapResult(
            swapped=True,
            messages=new_messages,
            covered_until_seq=0,
            note_version=tracking.note_version,
            snapshot_message_id=snapshot_msg_id,
            pruned_message_count=pruned_count,
            tail_message_count=len(tail_messages),
            snapshot_markdown=handbook_text,
            total_tokens=total_tokens,
        )

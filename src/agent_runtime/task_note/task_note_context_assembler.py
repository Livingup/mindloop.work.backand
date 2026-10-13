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
from agent_runtime.task_note.task_note_models import TaskNoteWatermark
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
        scheduler: Any | None = None,
        swap_wait_timeout_seconds: float = 10.0,
    ) -> None:
        self.slicer = slicer or TaskNoteUnitSlicer()
        self.store = store or get_task_note_store()
        self.scheduler = scheduler
        self.swap_wait_timeout_seconds = swap_wait_timeout_seconds

    async def assemble_context(
        self,
        *,
        messages: Sequence[Any],
        watermark: TaskNoteWatermark | None = None,
        note_snapshot: dict[str, Any] | None = None,
        current_working_directory: str = "",
        command_lookup: dict[str, str] | None = None,
        last_swapped_seq: int = 0,
        task_id: str | None = None,
        agent_instance_id: str | None = None,
        context_window_threshold: int | None = None,
        current_tokens: int | None = None,
        force_swap: bool = False,
    ) -> ContextSwapResult:
        """检查并执行前缀历史消息向手记快照的安全替换。"""
        current_messages = list(messages)
        if not current_messages:
            return ContextSwapResult(
                swapped=False,
                messages=[],
                covered_until_seq=0,
                note_version=0,
            )

        # 计算当前消息序列总 Token
        total_tokens = current_tokens
        if total_tokens is None:
            total_tokens = sum(estimate_message_tokens(m) for m in current_messages)

        # 1. 业务 Agent 上下文窗口门禁检查
        if context_window_threshold is not None and not force_swap:
            if total_tokens < context_window_threshold:
                logger.debug(
                    "业务 Agent 上下文总 Token (%d) 未达系统阈值 (%d)，保持原样",
                    total_tokens,
                    context_window_threshold,
                )
                return ContextSwapResult(
                    swapped=False,
                    messages=current_messages,
                    covered_until_seq=last_swapped_seq,
                    note_version=watermark.note_version if watermark else 0,
                    tail_message_count=len(current_messages),
                    total_tokens=total_tokens,
                    context_window_threshold=context_window_threshold,
                )

        # 2. M6-1: 若替换时观察者正在后台运行，等待最多 swap_wait_timeout_seconds
        if (
            self.scheduler is not None
            and agent_instance_id
            and hasattr(self.scheduler, "is_observer_running")
            and self.scheduler.is_observer_running(agent_instance_id)
        ):
            logger.info("替换时检测到观察者运行中，等待最多 %s 秒 (M6-1)", self.swap_wait_timeout_seconds)
            try:
                if hasattr(self.scheduler, "wait_observation_done"):
                    await self.scheduler.wait_observation_done(
                        agent_instance_id,
                        timeout=self.swap_wait_timeout_seconds,
                    )
                elif hasattr(self.scheduler, "wait_for_observer"):
                    await self.scheduler.wait_for_observer(
                        agent_instance_id,
                        timeout=self.swap_wait_timeout_seconds,
                    )
            except Exception as exc:
                logger.warning("等待后台观察者完成异常或超时: %s，使用上一版快照", exc)

        # 3. 读取当前最新 Watermark
        active_watermark = watermark
        if active_watermark is None and task_id and agent_instance_id:
            try:
                active_watermark = self.store.get_watermark(
                    agent_instance_id=agent_instance_id,
                    task_id=task_id,
                )
            except Exception as exc:
                logger.warning("获取 watermark 失败: %s", exc)

        # 校验 note_version >= 1 且 covered_until_seq > 0 (D08 第 4 条：note_version < 1 时不替换)
        if active_watermark is None or active_watermark.note_version < 1 or active_watermark.covered_until_seq <= 0:
            logger.debug("note_version < 1 或无覆盖断点，不执行替换")
            return ContextSwapResult(
                swapped=False,
                messages=current_messages,
                covered_until_seq=last_swapped_seq,
                note_version=active_watermark.note_version if active_watermark else 0,
                tail_message_count=len(current_messages),
                total_tokens=total_tokens,
                context_window_threshold=context_window_threshold,
            )

        if active_watermark.covered_until_seq <= last_swapped_seq:
            return ContextSwapResult(
                swapped=False,
                messages=current_messages,
                covered_until_seq=last_swapped_seq,
                note_version=active_watermark.note_version,
                tail_message_count=len(current_messages),
                total_tokens=total_tokens,
                context_window_threshold=context_window_threshold,
            )

        # 4. 获取最新 Note 结构化快照
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
                    covered_until_seq=last_swapped_seq,
                    note_version=active_watermark.note_version,
                    total_tokens=total_tokens,
                    context_window_threshold=context_window_threshold,
                )

        if not snapshot_dict:
            logger.warning("Note 快照为空，跳过本次替换 (D08)")
            return ContextSwapResult(
                swapped=False,
                messages=current_messages,
                covered_until_seq=last_swapped_seq,
                note_version=active_watermark.note_version,
                total_tokens=total_tokens,
                context_window_threshold=context_window_threshold,
            )

        # 5. 提取残差尾部消息 (covered_until_message_id 之后的原始消息)
        covered_mid = active_watermark.covered_until_message_id
        tail_messages: list[Any] = []
        covered_idx = -1

        if covered_mid:
            for idx, msg in enumerate(current_messages):
                if _get_msg_id(msg) == covered_mid:
                    covered_idx = idx
                    break

        if covered_idx >= 0:
            tail_messages = current_messages[covered_idx + 1:]
        else:
            # 回退切片单元提取
            slice_res = self.slicer.slice_messages(
                messages=current_messages,
                watermark_covered_until_seq=active_watermark.covered_until_seq,
                covered_until_message_id=covered_mid,
            )
            tail_units = [u for u in slice_res.all_units if u.seq > active_watermark.covered_until_seq]
            for u in tail_units:
                tail_messages.extend(u.messages)

        # 6. M6-2: 组装后尾部仍超过门禁阈值，先同步执行一次观察再重新组装
        tail_tokens = sum(estimate_message_tokens(m) for m in tail_messages)
        if (
            context_window_threshold is not None
            and tail_tokens >= context_window_threshold
            and self.scheduler is not None
            and hasattr(self.scheduler, "trigger_and_await_observation")
            and task_id
            and agent_instance_id
        ):
            logger.info("残差尾部 Token (%d) 仍超门禁 (%d)，同步执行一次观察 (M6-2)", tail_tokens, context_window_threshold)
            try:
                sync_obs_res = await self.scheduler.trigger_and_await_observation(
                    agent_instance_id=agent_instance_id,
                    task_id=task_id,
                    messages=current_messages,
                    timeout=max(self.swap_wait_timeout_seconds * 2, 30.0),
                )
                if sync_obs_res and sync_obs_res.success and sync_obs_res.covered_until_seq > active_watermark.covered_until_seq:
                    active_watermark = self.store.get_watermark(agent_instance_id=agent_instance_id, task_id=task_id)
                    snapshot_dict = self.store.get_note_snapshot(task_id=task_id, agent_instance_id=agent_instance_id)
                    covered_mid = active_watermark.covered_until_message_id
                    if covered_mid:
                        for idx, msg in enumerate(current_messages):
                            if _get_msg_id(msg) == covered_mid:
                                tail_messages = current_messages[idx + 1:]
                                break
            except Exception as exc:
                logger.warning("M6-2 同步观察执行异常: %s", exc)

        # 7. 生成高密度 Markdown 任务手记
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
            f"# 任务手记快照 (v{active_watermark.note_version} | 覆盖断点 seq={active_watermark.covered_until_seq})\n\n"
            f"{markdown_body}\n\n"
            "---\n"
            f"> 💡 系统已将截至断点 seq={active_watermark.covered_until_seq} 的历史前缀消息安全压缩沉淀至上述任务手记。"
            "请根据工作手记中的最新执行方案、已交付成果与下一步行动指针 (Next Step)，结合下方最新残差消息继续推进业务任务。"
        )

        # 8. M6-3: 尾部首条是 user 消息时与快照 user 消息合并，保证消息协议合法
        sample_msg = current_messages[0]
        is_unimessage = False
        try:
            from agent_runtime.conversation_message.message.models import ROLE_USER, TextPart, UniMessage
            if isinstance(sample_msg, UniMessage):
                is_unimessage = True
        except Exception:
            is_unimessage = False

        new_messages: list[Any] = []
        has_tail = len(tail_messages) > 0
        first_tail_is_user = has_tail and (_get_msg_role(tail_messages[0]) == "user")

        if is_unimessage:
            from agent_runtime.conversation_message.message.models import ROLE_USER, TextPart, UniMessage

            if first_tail_is_user:
                # 合并首条 user 消息
                first_tail = tail_messages[0]
                merged_text = handbook_text + "\n\n---\n\n" + _extract_msg_text(first_tail)
                merged_msg = UniMessage(
                    role=ROLE_USER,
                    content=[TextPart(text=merged_text)],
                    message_id=getattr(first_tail, "message_id", None) or f"msg_merged_{uuid.uuid4().hex[:8]}",
                )
                try:
                    setattr(merged_msg, "is_snapshot", True)
                except Exception:
                    pass
                new_messages = [merged_msg] + tail_messages[1:]
            else:
                snapshot_msg = UniMessage(
                    role=ROLE_USER,
                    content=[TextPart(text=handbook_text)],
                    message_id=f"msg_snapshot_v{active_watermark.note_version}_{uuid.uuid4().hex[:8]}",
                )
                try:
                    setattr(snapshot_msg, "is_snapshot", True)
                except Exception:
                    pass
                new_messages = [snapshot_msg] + tail_messages
        else:
            if first_tail_is_user:
                first_tail = tail_messages[0]
                merged_content = handbook_text + "\n\n---\n\n" + _extract_msg_text(first_tail)
                merged_msg = {
                    "id": first_tail.get("id") or f"snapshot-merged-v{active_watermark.note_version}",
                    "role": "user",
                    "content": merged_content,
                    "is_snapshot": True,
                }
                new_messages = [merged_msg] + tail_messages[1:]
            else:
                snapshot_msg = {
                    "id": f"snapshot-v{active_watermark.note_version}-seq{active_watermark.covered_until_seq}",
                    "role": "user",
                    "content": handbook_text,
                    "is_snapshot": True,
                }
                new_messages = [snapshot_msg] + tail_messages

        pruned_count = len(current_messages) - len(tail_messages)
        logger.info(
            "成功完成 Stage 4 上下文替换 (D05): 原消息=%d, 裁剪历史=%d, 保留尾部=%d, 新水位 seq=%d, version=%d",
            len(current_messages),
            pruned_count,
            len(tail_messages),
            active_watermark.covered_until_seq,
            active_watermark.note_version,
        )

        return ContextSwapResult(
            swapped=True,
            messages=new_messages,
            covered_until_seq=active_watermark.covered_until_seq,
            note_version=active_watermark.note_version,
            pruned_message_count=pruned_count,
            tail_message_count=len(tail_messages),
            snapshot_markdown=handbook_text,
            total_tokens=total_tokens,
            context_window_threshold=context_window_threshold,
        )

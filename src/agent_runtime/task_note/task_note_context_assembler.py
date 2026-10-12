# -*- coding: utf-8 -*-
"""Stage 4 上下文热组装器与替换机制 (TaskNoteContextAssembler)。

核心职责：
1. 在业务 Agent 两次 LLM 调用之间的安全断点，检测是否有已完成的新版本 Note 快照；
2. 若 watermark.covered_until_seq > 0 且存在未合入断点：
   - 获取 Note 数据库最新全量结构化快照并格式化为高密度 Markdown 现场工作手册；
   - 将 seq <= watermark.covered_until_seq 的老历史消息全量剔除；
   - 将手记快照注入为首条手记消息 (is_snapshot=True)；
   - 无缝衔接 seq > watermark.covered_until_seq 的所有残差尾部消息 (tail messages)；
3. 输出全新的紧凑消息序列，作为下一次 LLM 调用的 Prompt 上下文，实现完全平滑无感的前缀替换。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

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

logger = logging.getLogger(__name__)


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


class TaskNoteContextAssembler:
    """上下文热组装器。"""

    def __init__(
        self,
        *,
        slicer: TaskNoteUnitSlicer | None = None,
        store: TaskNoteStore | None = None,
    ) -> None:
        self.slicer = slicer or TaskNoteUnitSlicer()
        self.store = store or get_task_note_store()

    def assemble_context(
        self,
        *,
        messages: Sequence[Any],
        watermark: TaskNoteWatermark,
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
        """检查并执行前缀历史消息向手记快照的安全替换。
        
        触发规则（严格遵守双线程解耦架构）：
        1. 仅当业务 Agent 自身的总上下文窗口达到系统阈值 (total_tokens >= context_window_threshold)
           或者显式指定 force_swap=True 时，才允许进入热替换流程；
        2. 未达到阈值时 (total_tokens < context_window_threshold)，无论后台 Note 是否更新，
           均保持原有完整历史消息，实现原汁原味的无损长会话交互；
        3. 达到阈值后，必须同时满足存在新版本覆盖断点 (watermark.covered_until_seq > last_swapped_seq)，
           方才执行历史老消息向 Note 快照的高密度热替换。
        """
        current_messages = list(messages)
        if not current_messages:
            return ContextSwapResult(
                swapped=False,
                messages=[],
                covered_until_seq=watermark.covered_until_seq,
                note_version=watermark.note_version,
            )

        # 计算当前消息序列的总 Token 占用
        total_tokens = current_tokens
        if total_tokens is None:
            total_tokens = sum(estimate_message_tokens(m) for m in current_messages)

        # 1. 业务 Agent 自身上下文窗口大小阈值门禁判断
        # 若配置了阈值且未强制替换，若总上下文尚未超出阈值，绝不执行替换，维持原样
        if context_window_threshold is not None and not force_swap:
            if total_tokens < context_window_threshold:
                logger.debug(
                    "业务 Agent 上下文总 Token (%d) 未达系统阈值 (%d)，保持完整历史，不执行替换",
                    total_tokens,
                    context_window_threshold,
                )
                return ContextSwapResult(
                    swapped=False,
                    messages=current_messages,
                    covered_until_seq=last_swapped_seq,
                    note_version=watermark.note_version,
                    tail_message_count=len(current_messages),
                    total_tokens=total_tokens,
                    context_window_threshold=context_window_threshold,
                )

        # 2. 若当前尚未有任何覆盖断点，或当前水位未超越上一次替换断点，无需替换
        if watermark.covered_until_seq <= 0 or watermark.covered_until_seq <= last_swapped_seq:
            return ContextSwapResult(
                swapped=False,
                messages=current_messages,
                covered_until_seq=last_swapped_seq,
                note_version=watermark.note_version,
                tail_message_count=len(current_messages),
                total_tokens=total_tokens,
                context_window_threshold=context_window_threshold,
            )

        # 3. 对当前会话进行原子断点切片
        slice_res = self.slicer.slice_messages(
            messages=current_messages,
            watermark_covered_until_seq=watermark.covered_until_seq,
            covered_until_message_id=watermark.covered_until_message_id,
        )

        effective_seq = slice_res.watermark_covered_until_seq
        covered_units = [u for u in slice_res.all_units if u.seq <= effective_seq]
        tail_units = [u for u in slice_res.all_units if u.seq > effective_seq]

        # 若当前没有任何历史 Unit 满足 <= watermark，说明消息序列中已被替换过或无老消息
        if not covered_units:
            return ContextSwapResult(
                swapped=False,
                messages=current_messages,
                covered_until_seq=last_swapped_seq,
                note_version=watermark.note_version,
                tail_message_count=len(current_messages),
                total_tokens=total_tokens,
                context_window_threshold=context_window_threshold,
            )

        # 4. 获取或组装最新 Note 结构化快照
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
                    note_version=watermark.note_version,
                    total_tokens=total_tokens,
                    context_window_threshold=context_window_threshold,
                )

        # 反查命令行映射（若有需要）
        final_lookup = dict(command_lookup or {})
        if not final_lookup and task_id and agent_instance_id:
            try:
                final_lookup = self.store.get_all_command_lines_for_tenant(
                    task_id=task_id, agent_instance_id=agent_instance_id
                )
            except Exception:
                pass

        # 4. 生成高密度 Markdown 任务手记
        markdown_body = format_task_note_to_markdown(
            snapshot_dict,
            command_lookup=final_lookup,
            current_working_directory=current_working_directory,
        )

        handbook_text = (
            f"# 任务手记快照 (v{watermark.note_version} | 覆盖断点 seq={watermark.covered_until_seq})\n\n"
            f"{markdown_body}\n\n"
            "---\n"
            f"> 💡 系统已将截至断点 seq={watermark.covered_until_seq} 的历史前缀消息安全压缩沉淀至上述任务手记。"
            "请根据工作手记中的最新执行方案、已交付成果与下一步行动指针 (Next Step)，结合下方最新残差消息继续推进业务任务。"
        )

        # 5. 构造快照手记消息（兼容 UniMessage 对象与 dict）
        sample_msg = current_messages[0]
        snapshot_msg: Any
        is_unimessage = False
        try:
            from agent_runtime.conversation_message.message.models import ROLE_USER, TextPart, UniMessage
            if isinstance(sample_msg, UniMessage):
                is_unimessage = True
                snapshot_msg = UniMessage(
                    role=ROLE_USER,
                    content=[TextPart(text=handbook_text)],
                )
                # 附加 is_snapshot 标记
                try:
                    setattr(snapshot_msg, "is_snapshot", True)
                except Exception:
                    pass
        except Exception:
            is_unimessage = False

        if not is_unimessage:
            snapshot_msg = {
                "id": f"snapshot-v{watermark.note_version}-seq{watermark.covered_until_seq}",
                "role": "user",
                "content": handbook_text,
                "is_snapshot": True,
            }

        # 6. 提取残差尾部消息
        tail_messages: list[Any] = []
        for u in tail_units:
            tail_messages.extend(u.messages)

        new_messages = [snapshot_msg] + tail_messages
        pruned_count = sum(len(u.messages) for u in covered_units)

        logger.info(
            "成功完成 Stage 4 上下文前缀替换: 原消息数=%d, 裁剪前缀消息数=%d, 残差消息数=%d, 新水位 seq=%d",
            len(current_messages),
            pruned_count,
            len(tail_messages),
            watermark.covered_until_seq,
        )

        return ContextSwapResult(
            swapped=True,
            messages=new_messages,
            covered_until_seq=watermark.covered_until_seq,
            note_version=watermark.note_version,
            pruned_message_count=pruned_count,
            tail_message_count=len(tail_messages),
            snapshot_markdown=handbook_text,
            total_tokens=total_tokens,
            context_window_threshold=context_window_threshold,
        )

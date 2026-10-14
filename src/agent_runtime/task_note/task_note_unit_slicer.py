# -*- coding: utf-8 -*-
"""Task Note 消息合法断点切片器 (TaskNoteUnitSlicer)。

核心职责：
1. 分析业务 Agent 的会话消息序列，精确识别合法状态断点；
2. 将不可拆分的原子消息批次划分为 TaskNoteUnit，并单调自增编号 seq；
3. 支持识别快照手记 (is_snapshot=True)，将其排除在增量 Token 计算之外，确保前缀替换时增量平滑连续；
4. 区分子 Agent 的继承段 (inherited_units) 与当前 Agent 的增量段 (incremental_units)；
5. 按照可配置的阈值（默认 80k Token）判断是否应当触发后台观察。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from agent_runtime.task_note.task_note_models import SliceResult, TaskNoteUnit

logger = logging.getLogger(__name__)

# 默认触发阈值：80k Token
DEFAULT_OBSERVER_TRIGGER_TOKENS = 80000

# 字符到 Token 估算比率（中英混杂保守估算）
_CHARS_PER_TOKEN = 4
_MESSAGE_OVERHEAD_TOKENS = 4


def _estimate_text_tokens_smart(text: str) -> int:
    """中英混杂自适应 Token 估算器 (CJK: ~1.2 tokens/字, 非 CJK: ~1/3.5 tokens/字)。"""
    if not text:
        return 0
    cjk_count = 0
    for ch in text:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0xF900 <= code <= 0xFAFF:
            cjk_count += 1
    other_count = len(text) - cjk_count
    return max(1, int(cjk_count * 1.2 + other_count / 3.5))


def estimate_message_tokens(msg: Any) -> int:
    """估算单条消息消耗的 Token 数量（兼容 UniMessage 对象与字典）。"""
    if msg is None:
        return 0

    # 1. 优先提取 UniMessage 或通用字典内容
    role = ""
    content_text = ""
    tool_text = ""

    if isinstance(msg, dict):
        role = str(msg.get("role") or "")
        content = msg.get("content")
        if isinstance(content, str):
            content_text = content
        elif isinstance(content, list):
            content_text = json.dumps(content, ensure_ascii=False)
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            tool_text = json.dumps(tool_calls, ensure_ascii=False)
    else:
        role = str(getattr(msg, "role", "") or "")
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            content_text = content
        elif isinstance(content, list):
            try:
                parts_str = []
                for p in content:
                    if hasattr(p, "text"):
                        parts_str.append(str(p.text or ""))
                    elif hasattr(p, "output"):
                        parts_str.append(str(p.output or ""))
                    elif hasattr(p, "input"):
                        parts_str.append(json.dumps(p.input, ensure_ascii=False))
                    else:
                        parts_str.append(str(p))
                content_text = " ".join(parts_str)
            except Exception:
                content_text = str(content)
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            try:
                tool_text = json.dumps(tool_calls, ensure_ascii=False, default=str)
            except Exception:
                tool_text = str(tool_calls)

    role_tokens = _estimate_text_tokens_smart(role)
    content_tokens = _estimate_text_tokens_smart(content_text)
    tool_tokens = _estimate_text_tokens_smart(tool_text)
    return _MESSAGE_OVERHEAD_TOKENS + role_tokens + content_tokens + tool_tokens


def _get_msg_id(msg: Any, fallback_index: int) -> str:
    """获取消息唯一标识 ID (优先 UniMessage.message_id)。"""
    if isinstance(msg, dict):
        return str(msg.get("message_id") or msg.get("id") or f"msg-{fallback_index}")
    mid = getattr(msg, "message_id", None) or getattr(msg, "id", None)
    if mid:
        return str(mid)
    return f"msg-{fallback_index}"


def _get_msg_role(msg: Any) -> str:
    """获取消息角色 (user, assistant, tool, system)。"""
    if isinstance(msg, dict):
        return str(msg.get("role") or "").lower()
    return str(getattr(msg, "role", "") or "").lower()


def _is_snapshot_msg(msg: Any) -> bool:
    """判断消息是否属于任务手记快照替换产生的消息。"""
    # 1. 显式字段标记
    if isinstance(msg, dict):
        if msg.get("is_snapshot") is True:
            return True
        metadata = msg.get("metadata")
        if isinstance(metadata, dict) and metadata.get("is_snapshot") is True:
            return True
        content = str(msg.get("content") or "")
    else:
        if getattr(msg, "is_snapshot", False) is True:
            return True
        metadata = getattr(msg, "metadata", None)
        if isinstance(metadata, dict) and metadata.get("is_snapshot") is True:
            return True
        content = str(getattr(msg, "content", "") or "")

    # 2. 手记特征前缀
    if "# 任务手记" in content or "【任务手记快照】" in content or "[TASK_NOTE_SNAPSHOT]" in content:
        return True
    return False


def _has_tool_calls(msg: Any) -> bool:
    """判断 assistant 消息是否发起了工具调用。"""
    if isinstance(msg, dict):
        tcs = msg.get("tool_calls")
        return bool(tcs)
    tcs = getattr(msg, "tool_calls", None)
    if tcs:
        return True
    # 检查 content 中的 ToolUsePart
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for part in content:
            if type(part).__name__ == "ToolUsePart" or hasattr(part, "tool_use_id"):
                return True
    return False


def _is_tool_result(msg: Any) -> bool:
    """判断消息是否是工具返回的结果。"""
    role = _get_msg_role(msg)
    if role in ("tool", "function"):
        return True
    if isinstance(msg, dict) and msg.get("tool_call_id"):
        return True
    if hasattr(msg, "tool_call_id") and getattr(msg, "tool_call_id"):
        return True
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for part in content:
            if type(part).__name__ == "ToolResultPart" or hasattr(part, "tool_use_ref"):
                return True
    return False


class TaskNoteUnitSlicer:
    """合法断点切片器。"""

    def __init__(self, trigger_threshold_tokens: int = DEFAULT_OBSERVER_TRIGGER_TOKENS) -> None:
        self.trigger_threshold_tokens = trigger_threshold_tokens

    def slice_messages(
        self,
        messages: Sequence[Any],
        *,
        start_after_message_id: str | None = None,
        end_message_id: str | None = None,
        snapshot_message_id: str | None = None,
        inherited_until_message_id: str | None = None,
    ) -> SliceResult:
        """对消息序列进行原子切片，并统计增量 Token 及触发条件。"""
        if not messages:
            return SliceResult(
                all_units=[],
                inherited_units=[],
                incremental_units=[],
                incremental_tokens=0,
                should_trigger=False,
                max_seq=0,
                watermark_covered_until_seq=0,
            )

        # 1. 划分 inherited_units（若指定 inherited_until_message_id）
        inherited_units: list[TaskNoteUnit] = []
        if inherited_until_message_id:
            inh_end_idx = -1
            for idx, msg in enumerate(messages):
                if _get_msg_id(msg, idx) == inherited_until_message_id:
                    inh_end_idx = idx
                    break
            if inh_end_idx != -1:
                inh_items = [(messages[i], i) for i in range(inh_end_idx + 1)]
                inherited_units = self._slice_segment(inh_items, snapshot_message_id=None)

        # 2. 定位增量起点 start_idx 与终点 end_idx
        start_idx = 0
        if start_after_message_id:
            found_start = False
            for idx, msg in enumerate(messages):
                if _get_msg_id(msg, idx) == start_after_message_id:
                    # 若起点正好是快照消息且该快照与 user 消息合并（content > 1），
                    # 该快照消息本身需要保留在切片范围内以便提取 content[1:] 的 user_turn
                    if snapshot_message_id and _get_msg_id(msg, idx) == snapshot_message_id:
                        content = getattr(msg, "content", None)
                        if content is None and isinstance(msg, dict):
                            content = msg.get("content")
                        if isinstance(content, list) and len(content) > 1:
                            start_idx = idx
                            found_start = True
                            break
                    start_idx = idx + 1
                    found_start = True
                    break
            if not found_start:
                # 容灾：退回到最后一条首个 TextPart 以 "# 任务手记快照" 开头的消息
                last_snap_idx = -1
                for idx, msg in enumerate(messages):
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

        end_idx = len(messages)
        if end_message_id:
            for idx, msg in enumerate(messages):
                if _get_msg_id(msg, idx) == end_message_id:
                    end_idx = idx + 1
                    break

        # 3. 切片增量段
        if start_idx < end_idx:
            inc_items = [(messages[i], i) for i in range(start_idx, end_idx)]
            incremental_units = self._slice_segment(inc_items, snapshot_message_id=snapshot_message_id)
        else:
            incremental_units = []

        all_units = inherited_units + incremental_units
        incremental_tokens = sum(u.token_count for u in incremental_units if not u.is_snapshot)
        should_trigger = incremental_tokens >= self.trigger_threshold_tokens
        max_seq = max([u.seq for u in incremental_units], default=0)

        logger.debug(
            "TaskNoteUnitSlicer 完成切片: total_units=%d, incremental_units=%d, incremental_tokens=%d, trigger=%s",
            len(all_units),
            len(incremental_units),
            incremental_tokens,
            should_trigger,
        )

        return SliceResult(
            all_units=all_units,
            inherited_units=inherited_units,
            incremental_units=incremental_units,
            incremental_tokens=incremental_tokens,
            should_trigger=should_trigger,
            max_seq=max_seq,
            watermark_covered_until_seq=0,
        )

    def _slice_segment(
        self,
        items: list[tuple[Any, int]],
        *,
        snapshot_message_id: str | None = None,
    ) -> list[TaskNoteUnit]:
        """将带有原列表绝对下标的消息序列切分为一组 TaskNoteUnit，seq 从 1 开始独立编号。"""
        units: list[TaskNoteUnit] = []
        seq_counter = 1
        current_batch: list[tuple[Any, int]] = []

        def commit_unit(unit_type: str, is_snapshot: bool = False) -> None:
            nonlocal seq_counter, current_batch
            if not current_batch:
                return
            last_msg, last_abs_idx = current_batch[-1]
            end_mid = _get_msg_id(last_msg, last_abs_idx)
            batch_msgs = [m for m, _ in current_batch]
            unit_tokens = sum(estimate_message_tokens(m) for m in batch_msgs)
            unit = TaskNoteUnit(
                seq=seq_counter,
                unit_type=unit_type,
                messages=batch_msgs,
                token_count=unit_tokens,
                end_message_id=end_mid,
                is_snapshot=is_snapshot or any(_is_snapshot_msg(m) for m in batch_msgs),
            )
            units.append(unit)
            seq_counter += 1
            current_batch = []

        i = 0
        n = len(items)
        while i < n:
            msg, abs_idx = items[i]
            msg_id = _get_msg_id(msg, abs_idx)
            role = _get_msg_role(msg)
            is_snap = _is_snapshot_msg(msg)

            # 遇到 snapshot_message_id 那条消息：content 只有一项时跳过；否则生成 user_turn unit 保留 content[1:]
            if snapshot_message_id and msg_id == snapshot_message_id:
                if current_batch:
                    commit_unit(unit_type="mixed_execution")
                content = getattr(msg, "content", None)
                if content is None and isinstance(msg, dict):
                    content = msg.get("content")

                if isinstance(content, list) and len(content) > 1:
                    tail_content = list(content[1:])
                    if isinstance(msg, dict):
                        tail_msg = dict(msg)
                        tail_msg["role"] = "user"
                        tail_msg["content"] = tail_content
                    else:
                        try:
                            from agent_runtime.conversation.uni_message import UniMessage
                            tail_msg = UniMessage(
                                message_id=msg_id,
                                role="user",
                                content=tail_content,
                                created_at=getattr(msg, "created_at", None),
                            )
                        except Exception:
                            import copy
                            tail_msg = copy.copy(msg)
                            setattr(tail_msg, "role", "user")
                            setattr(tail_msg, "content", tail_content)
                    current_batch.append((tail_msg, abs_idx))
                    commit_unit(unit_type="user_turn", is_snapshot=False)
                i += 1
                continue

            # 1. 普通手记快照消息特征
            if is_snap:
                if current_batch:
                    commit_unit(unit_type="mixed_execution")
                current_batch.append((msg, abs_idx))
                commit_unit(unit_type="snapshot", is_snapshot=True)
                i += 1
                continue

            # 2. 系统消息
            if role == "system":
                if current_batch:
                    commit_unit(unit_type="mixed_execution")
                current_batch.append((msg, abs_idx))
                commit_unit(unit_type="system")
                i += 1
                continue

            # 3. 用户消息
            if role == "user":
                if current_batch:
                    commit_unit(unit_type="mixed_execution")
                current_batch.append((msg, abs_idx))
                commit_unit(unit_type="user_turn")
                i += 1
                continue

            # 4. 助手消息
            if role == "assistant":
                if not _has_tool_calls(msg):
                    if current_batch:
                        commit_unit(unit_type="mixed_execution")
                    current_batch.append((msg, abs_idx))
                    commit_unit(unit_type="direct_response")
                    i += 1
                    continue
                else:
                    if current_batch:
                        commit_unit(unit_type="mixed_execution")
                    current_batch.append((msg, abs_idx))
                    i += 1
                    while i < n and _is_tool_result(items[i][0]):
                        current_batch.append((items[i][0], items[i][1]))
                        i += 1
                    commit_unit(unit_type="tool_interaction")
                    continue

            # 5. 孤立 tool 结果或其他未知消息
            current_batch.append((msg, abs_idx))
            if i + 1 >= n or not _is_tool_result(items[i + 1][0]):
                commit_unit(unit_type="tool_interaction")
            i += 1

        if current_batch:
            commit_unit(unit_type="trailing_execution")

        return units

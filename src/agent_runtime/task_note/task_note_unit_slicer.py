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


def estimate_message_tokens(msg: Any) -> int:
    """估算单条消息消耗的 Token 数量（兼容 UniMessage 对象与字典）。"""
    if msg is None:
        return 0

    # 1. 尝试使用 UniMessage 对象的特定属性与 gate 估算逻辑
    try:
        from agent_runtime.context_compression.gate import _estimate_message_tokens
        from agent_runtime.conversation.uni_message import UniMessage
        if isinstance(msg, UniMessage):
            return _estimate_message_tokens(msg)
    except Exception:
        pass

    # 2. 通用对象或字典提取
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
                # 遍历 UniMessage parts
                parts_str = []
                for p in content:
                    if hasattr(p, "text"):
                        parts_str.append(str(p.text))
                    elif hasattr(p, "output"):
                        parts_str.append(str(p.output))
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

    total_chars = len(role) + len(content_text) + len(tool_text)
    estimated = _MESSAGE_OVERHEAD_TOKENS + max(1, (total_chars + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)
    return estimated


def _get_msg_id(msg: Any, fallback_index: int) -> str:
    """获取消息唯一标识 ID。"""
    if isinstance(msg, dict):
        return str(msg.get("id") or f"msg-{fallback_index}")
    mid = getattr(msg, "id", None)
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
        watermark_covered_until_seq: int = 0,
        is_subagent: bool = False,
        inherited_processed: bool = False,
        fork_message_id: str | None = None,
    ) -> SliceResult:
        """对消息序列进行原子切片，并统计增量 Token 及触发条件。"""
        if not messages:
            return SliceResult(
                all_units=[],
                inherited_units=[],
                incremental_units=[],
                incremental_tokens=0,
                should_trigger=False,
                max_seq=watermark_covered_until_seq,
                watermark_covered_until_seq=watermark_covered_until_seq,
            )

        all_units: list[TaskNoteUnit] = []
        current_batch: list[Any] = []
        seq_counter = 1

        def commit_unit(unit_type: str, is_snapshot: bool = False) -> None:
            nonlocal seq_counter, current_batch
            if not current_batch:
                return
            end_mid = _get_msg_id(current_batch[-1], len(current_batch) - 1)
            unit_tokens = sum(estimate_message_tokens(m) for m in current_batch)
            unit = TaskNoteUnit(
                seq=seq_counter,
                unit_type=unit_type,
                messages=list(current_batch),
                token_count=unit_tokens,
                end_message_id=end_mid,
                is_snapshot=is_snapshot or any(_is_snapshot_msg(m) for m in current_batch),
            )
            all_units.append(unit)
            seq_counter += 1
            current_batch = []

        i = 0
        n = len(messages)
        while i < n:
            msg = messages[i]
            role = _get_msg_role(msg)
            is_snap = _is_snapshot_msg(msg)

            # 1. 如果是手记快照消息，单独形成一个快照单元
            if is_snap:
                if current_batch:
                    commit_unit(unit_type="mixed_execution")
                current_batch.append(msg)
                commit_unit(unit_type="snapshot", is_snapshot=True)
                i += 1
                continue

            # 2. 如果是 system 消息，单独形成系统单元
            if role == "system":
                if current_batch:
                    commit_unit(unit_type="mixed_execution")
                current_batch.append(msg)
                commit_unit(unit_type="system")
                i += 1
                continue

            # 3. 如果是 user 消息：
            if role == "user":
                if current_batch:
                    commit_unit(unit_type="mixed_execution")
                current_batch.append(msg)
                # 用户消息自成一个断点
                commit_unit(unit_type="user_turn")
                i += 1
                continue

            # 4. 如果是 assistant 消息：
            if role == "assistant":
                if not _has_tool_calls(msg):
                    # 纯文本 assistant 消息（无工具调用）：直接自成回复断点
                    current_batch.append(msg)
                    commit_unit(unit_type="direct_response")
                    i += 1
                    continue
                else:
                    # assistant 包含工具调用：必须与随后的所有 tool 结果打包在同一 Unit 中
                    current_batch.append(msg)
                    i += 1
                    # 贪婪吸收紧随其后的所有 tool 结果
                    while i < n and _is_tool_result(messages[i]):
                        current_batch.append(messages[i])
                        i += 1
                    # 一批 tool 结果全部返回齐备后，形成一个合法断点
                    commit_unit(unit_type="tool_interaction")
                    continue

            # 5. 如果是孤立的 tool 结果或其他未知消息
            current_batch.append(msg)
            # 检查下一条消息是否还是 tool 结果，若不是则闭合该断点
            if i + 1 >= n or not _is_tool_result(messages[i + 1]):
                commit_unit(unit_type="tool_interaction")
            i += 1

        # 若末尾残留未提交的消息，收口提交
        if current_batch:
            commit_unit(unit_type="trailing_execution")

        # 划分 inherited_units（子 Agent 首次观察）
        inherited_units: list[TaskNoteUnit] = []
        if is_subagent and not inherited_processed:
            if fork_message_id:
                # 寻找包含 fork_message_id 的 Unit
                fork_unit_idx = -1
                for idx, u in enumerate(all_units):
                    for m in u.messages:
                        if _get_msg_id(m, 0) == fork_message_id:
                            fork_unit_idx = idx
                            break
                    if fork_unit_idx != -1:
                        break
                if fork_unit_idx != -1:
                    inherited_units = all_units[: fork_unit_idx + 1]
            else:
                # 若未指定 fork_message_id 但为首次子 Agent，将第一个 user 之前的 Unit 视为继承
                inherited_units = [u for u in all_units if u.seq <= watermark_covered_until_seq]

        # 划分 incremental_units (seq > watermark)
        incremental_units = [u for u in all_units if u.seq > watermark_covered_until_seq]

        # 统计增量 Token（严格排除快照手记 is_snapshot）
        incremental_tokens = sum(
            u.token_count for u in incremental_units if not u.is_snapshot
        )

        should_trigger = incremental_tokens >= self.trigger_threshold_tokens
        max_seq = max([u.seq for u in incremental_units], default=watermark_covered_until_seq)

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
            watermark_covered_until_seq=watermark_covered_until_seq,
        )

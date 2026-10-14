# -*- coding: utf-8 -*-
"""会话级 Task Note 增量跟踪状态协调器 (TaskNoteTrackingCoordinator)。

核心定位：
以 ConversationState.task_note_tracking（SQLite JSON + 版本号 CAS 乐观锁）作为跟踪状态的唯一权威。
提供 U1（每轮检查触发）、U2（门禁替换前缀）、U3（观察完成推进水位）三个生命周期关键更新点与并发安全管控。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from agent_runtime.conversation_message.conversation_repository import (
    ConversationRepository,
)
from agent_runtime.conversation_message.message.models import (
    Conversation,
    UniMessage,
)
from agent_runtime.task_note.task_note_models import (
    ObservingRun,
    TaskNoteTracking,
)
from agent_runtime.task_note.task_note_unit_slicer import (
    DEFAULT_OBSERVER_TRIGGER_TOKENS,
    estimate_message_tokens,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ObserveTicket:
    """观察任务启动凭证。"""

    run_id: str
    task_id: str
    agent_instance_id: str
    conversation_id: str
    messages: list[Any]
    start_after_message_id: str | None
    end_message_id: str | None
    inherited_until_message_id: str | None
    snapshot_message_id: str | None
    is_subagent: bool


def _get_msg_id(msg: Any) -> str | None:
    if msg is None:
        return None
    if isinstance(msg, dict):
        return str(msg.get("message_id") or msg.get("id") or "") or None
    mid = getattr(msg, "message_id", None) or getattr(msg, "id", None)
    return str(mid) if mid else None


def _is_snapshot_header(msg: Any) -> bool:
    """检查消息首个文本块是否为手记快照标记。"""
    if msg is None:
        return False
    content = getattr(msg, "content", None) if not isinstance(msg, dict) else msg.get("content")
    if isinstance(content, list) and content:
        first_part = content[0]
        text = getattr(first_part, "text", "") if not isinstance(first_part, dict) else first_part.get("text", "")
        return str(text or "").startswith("# 任务手记快照")
    if isinstance(content, str):
        return content.startswith("# 任务手记快照")
    return False


class TaskNoteTrackingCoordinator:
    """Task Note 增量跟踪状态协调器。"""

    def __init__(
        self,
        *,
        conversation_repository: ConversationRepository,
        trigger_threshold_tokens: int = DEFAULT_OBSERVER_TRIGGER_TOKENS,
    ) -> None:
        self.repo = conversation_repository
        self.trigger_threshold_tokens = trigger_threshold_tokens
        self._locks: dict[str, asyncio.Lock] = {}
        self._done: dict[str, asyncio.Event] = {}

    def _get_lock(self, instance_id: str) -> asyncio.Lock:
        if instance_id not in self._locks:
            self._locks[instance_id] = asyncio.Lock()
        return self._locks[instance_id]

    async def init_child(self, conversation_id: str, fork_message_id: str) -> None:
        """子 Agent 创建时初始化 tracking 状态 (2.3 规范)。"""
        tracking = TaskNoteTracking(
            version=0,
            prefix_end_message_id=fork_message_id,
            snapshot_message_id=None,
            covered_until_message_id=None,
            note_version=0,
            inherited_processed=False,
            observing=None,
        )
        ok = await self.repo.cas_task_note_tracking(conversation_id, expected_version=0, tracking=tracking.to_dict())
        if not ok:
            logger.warning("子会话 %s 初始化 tracking 失败或已有初始状态", conversation_id)

    async def get(self, conversation_id: str, *, is_subagent: bool = False) -> TaskNoteTracking:
        """获取当前跟踪状态对象。"""
        data, _ = await self.repo.get_task_note_tracking(conversation_id)
        return TaskNoteTracking.from_dict(data, is_subagent=is_subagent)

    async def on_round(
        self,
        *,
        task_id: str,
        instance_id: str,
        conversation: Conversation,
        is_subagent: bool,
        force: bool = False,
    ) -> ObserveTicket | None:
        """U1 阶段：在 LLM 调用返回后或工具批次后检查增量并申请观察 Ticket。"""
        lock = self._get_lock(instance_id)
        async with lock:
            cid = conversation.id
            data, version = await self.repo.get_task_note_tracking(cid)
            tracking = TaskNoteTracking.from_dict(data, is_subagent=is_subagent)

            # 1. 检查是否存在未完成观察
            if tracking.observing is not None:
                # 若 run_id 不在 _done 字典中，说明是重启前遗留的失效 observing，清理置 null
                if tracking.observing.run_id not in self._done:
                    logger.info("检测到重启遗留 observing (%s)，执行清理重置", tracking.observing.run_id)
                    tracking.observing = None
                    ok = await self.repo.cas_task_note_tracking(cid, version, tracking.to_dict())
                    if ok:
                        data, version = await self.repo.get_task_note_tracking(cid)
                        tracking = TaskNoteTracking.from_dict(data, is_subagent=is_subagent)
                    else:
                        logger.warning("清理遗留 observing CAS 失败")
                else:
                    logger.debug("实例 %s 已有观察者正在运行中 (%s)，跳过本次触发", instance_id, tracking.observing.run_id)
                    return None

            # 2. 确定增量起点 (2.1 规范)
            start_after_mid = tracking.covered_until_message_id
            if not start_after_mid:
                start_after_mid = tracking.prefix_end_message_id

            messages = list(conversation.messages)
            if not messages:
                return None

            # 检查起点 ID 是否在当前消息列表中存在
            msg_ids = [_get_msg_id(m) for m in messages]
            if start_after_mid and start_after_mid not in msg_ids:
                # 起点 ID 丢失（替换中途崩溃等异常情况），回退到最后一条快照标记消息
                fallback_mid = None
                for m in reversed(messages):
                    if _is_snapshot_header(m):
                        fallback_mid = _get_msg_id(m)
                        break
                logger.warning(
                    "起点消息 %s 不在当前消息序列中，回退至快照消息 %s",
                    start_after_mid,
                    fallback_mid,
                )
                start_after_mid = fallback_mid

            # 3. 定位切片起点下标
            start_idx = 0
            if start_after_mid:
                for idx, mid in enumerate(msg_ids):
                    if mid == start_after_mid:
                        start_idx = idx + 1
                        break

            # 4. 统计起点之后的增量 Token
            incremental_tokens = 0
            for idx in range(start_idx, len(messages)):
                m = messages[idx]
                mid = msg_ids[idx]
                if mid and mid == tracking.snapshot_message_id:
                    # 快照消息与 user 合并的情况：只计 content[1:]
                    content = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
                    if isinstance(content, list) and len(content) > 1:
                        from agent_runtime.conversation_message.message.models import UniMessage
                        sub_msg = UniMessage(
                            role=getattr(m, "role", "user"),
                            content=content[1:],
                            message_id=mid,
                        )
                        incremental_tokens += estimate_message_tokens(sub_msg)
                else:
                    incremental_tokens += estimate_message_tokens(m)

            # 首轮全量阶段：若尚无任何手记记录 (note_version == 0) 且未设置起点，
            # 优先结合真实 LLM 上下文用量 (context_window_used_tokens)，避免因忽略系统提示词与工具 Schema 导致严重低估
            if start_after_mid is None and tracking.note_version == 0:
                usage = getattr(getattr(conversation, "state", None), "last_context_usage", None) or {}
                used_tokens = usage.get("context_window_used_tokens")
                if used_tokens is not None and isinstance(used_tokens, int):
                    incremental_tokens = max(incremental_tokens, used_tokens)

            if not force and incremental_tokens < self.trigger_threshold_tokens:
                logger.debug(
                    "实例 %s 增量 Token (%d) 未达阈值 (%d)，无需观察",
                    instance_id,
                    incremental_tokens,
                    self.trigger_threshold_tokens,
                )
                return None

            # 5. 满足阈值或 force=True，生成 run_id 并写入 observing
            end_msg_id = msg_ids[-1]
            if not end_msg_id:
                return None

            run_id = f"obs_{uuid.uuid4().hex}"
            started_at = datetime.now(timezone.utc).isoformat()
            tracking.observing = ObservingRun(
                run_id=run_id,
                start_after_message_id=start_after_mid,
                end_message_id=end_msg_id,
                started_at=started_at,
            )

            # CAS 写入
            ok = await self.repo.cas_task_note_tracking(cid, version, tracking.to_dict())
            if not ok:
                # 重试一次
                data, version = await self.repo.get_task_note_tracking(cid)
                tracking = TaskNoteTracking.from_dict(data, is_subagent=is_subagent)
                if tracking.observing is not None:
                    return None
                tracking.observing = ObservingRun(
                    run_id=run_id,
                    start_after_message_id=start_after_mid,
                    end_message_id=end_msg_id,
                    started_at=started_at,
                )
                ok = await self.repo.cas_task_note_tracking(cid, version, tracking.to_dict())
                if not ok:
                    logger.error("写入 observing 两次 CAS 均失败 (cid=%s, run_id=%s)", cid, run_id)
                    return None

            # 登记完成事件
            self._done[run_id] = asyncio.Event()

            inherited_until_mid = None
            if is_subagent and not tracking.inherited_processed:
                inherited_until_mid = tracking.prefix_end_message_id

            logger.info("已成功为实例 %s 签发观察 Ticket (run_id=%s, tokens=%d)", instance_id, run_id, incremental_tokens)
            return ObserveTicket(
                run_id=run_id,
                task_id=task_id,
                agent_instance_id=instance_id,
                conversation_id=cid,
                messages=list(messages),
                start_after_message_id=start_after_mid,
                end_message_id=end_msg_id,
                inherited_until_message_id=inherited_until_mid,
                snapshot_message_id=tracking.snapshot_message_id,
                is_subagent=is_subagent,
            )

    async def complete(
        self,
        *,
        instance_id: str,
        conversation_id: str,
        run_id: str,
        success: bool,
        covered_until_message_id: str | None = None,
    ) -> int | None:
        """U3 阶段：观察结束时收敛状态并推进水位 (唯一推进点)。"""
        lock = self._get_lock(instance_id)
        new_note_version: int | None = None
        try:
            async with lock:
                data, version = await self.repo.get_task_note_tracking(conversation_id)
                tracking = TaskNoteTracking.from_dict(data)

                if tracking.observing is None or tracking.observing.run_id != run_id:
                    logger.warning(
                        "complete 调用 run_id 不匹配或已过期 (当前=%s, 调用=%s)",
                        tracking.observing.run_id if tracking.observing else None,
                        run_id,
                    )
                    return None

                if success:
                    tracking.covered_until_message_id = covered_until_message_id
                    tracking.note_version += 1
                    tracking.observing = None
                    tracking.inherited_processed = True
                    ok = await self.repo.cas_task_note_tracking(conversation_id, version, tracking.to_dict())
                    if ok:
                        new_note_version = tracking.note_version
                        logger.info(
                            "观察成功完成并推进水位: instance=%s, version=%d, covered_mid=%s",
                            instance_id,
                            new_note_version,
                            covered_until_message_id,
                        )
                    else:
                        logger.error("complete 成功推进 CAS 失败 (cid=%s, run_id=%s)", conversation_id, run_id)
                else:
                    tracking.observing = None
                    await self.repo.cas_task_note_tracking(conversation_id, version, tracking.to_dict())
                    logger.warning("观察执行失败，已清理 observing (instance=%s, run_id=%s)", instance_id, run_id)

            return new_note_version
        finally:
            # 无论成功或失败，唤醒并清理完成事件
            event = self._done.pop(run_id, None)
            if event is not None:
                event.set()

    async def await_running(self, *, instance_id: str, conversation_id: str) -> None:
        """U2 阶段：门禁替换前等待当前活跃观察完成，不设超时。"""
        data, version = await self.repo.get_task_note_tracking(conversation_id)
        tracking = TaskNoteTracking.from_dict(data)
        if tracking.observing is None:
            return

        run_id = tracking.observing.run_id
        if run_id in self._done:
            event = self._done[run_id]
            logger.info("等待实例 %s 运行中的观察者结束 (run_id=%s)...", instance_id, run_id)
            await event.wait()
            logger.info("实例 %s 运行中的观察者已结束 (run_id=%s)", instance_id, run_id)
        else:
            # 重启遗留的废弃 run，清理置 null
            lock = self._get_lock(instance_id)
            async with lock:
                data, version = await self.repo.get_task_note_tracking(conversation_id)
                tracking = TaskNoteTracking.from_dict(data)
                if tracking.observing and tracking.observing.run_id == run_id:
                    tracking.observing = None
                    await self.repo.cas_task_note_tracking(conversation_id, version, tracking.to_dict())

    async def apply_swap(
        self,
        *,
        instance_id: str,
        conversation_id: str,
        snapshot_message_id: str,
    ) -> bool:
        """U2 阶段：前缀替换成功后更新跟踪锚点。"""
        lock = self._get_lock(instance_id)
        async with lock:
            data, version = await self.repo.get_task_note_tracking(conversation_id)
            tracking = TaskNoteTracking.from_dict(data)
            tracking.prefix_end_message_id = snapshot_message_id
            tracking.snapshot_message_id = snapshot_message_id
            tracking.covered_until_message_id = None
            ok = await self.repo.cas_task_note_tracking(conversation_id, version, tracking.to_dict())
            if ok:
                logger.info(
                    "已成功应用前缀替换 tracking 锚点: instance=%s, snapshot_mid=%s",
                    instance_id,
                    snapshot_message_id,
                )
            return ok

    def forget_instance(self, instance_id: str) -> None:
        """实例结束或 GC 时注销内存锁。"""
        self._locks.pop(instance_id, None)


# ── 全局单例管理 ─────────────────────────────────────────────────────

_global_coordinator: TaskNoteTrackingCoordinator | None = None


def get_task_note_tracking_coordinator() -> TaskNoteTrackingCoordinator:
    """获取全局 TaskNoteTrackingCoordinator 单例。"""
    global _global_coordinator
    if _global_coordinator is None:
        raise RuntimeError("TaskNoteTrackingCoordinator 尚未初始化，请先调用 set_task_note_tracking_coordinator")
    return _global_coordinator


def set_task_note_tracking_coordinator(coordinator: TaskNoteTrackingCoordinator | None) -> None:
    """设置全局 TaskNoteTrackingCoordinator 单例。"""
    global _global_coordinator
    _global_coordinator = coordinator

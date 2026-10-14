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
from agent_runtime.task_note.task_note_range import (
    estimate_range_tokens,
    get_msg_id,
    is_snapshot_header,
    resolve_range_start,
)
from agent_runtime.task_note.task_note_unit_slicer import (
    DEFAULT_OBSERVER_TRIGGER_TOKENS,
)

# U3 成功写回时 CAS 冲突的最大写入次数
_COMPLETE_CAS_MAX_ATTEMPTS = 3
# 触发阈值不超过压缩门禁阈值的比例
_TRIGGER_TO_GATE_RATIO = 0.5


class TaskNoteTrackingInitError(RuntimeError):
    """子 Agent tracking 初始化失败（重试后仍失败）。"""

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


# 兼容旧引用
_get_msg_id = get_msg_id
_is_snapshot_header = is_snapshot_header


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

    def effective_trigger_threshold(self, gate_threshold: int | None = None) -> int:
        """触发阈值 T = min(observer_trigger_tokens, 门禁阈值 × 0.5)；门禁阈值未知时取配置值。"""
        threshold = self.trigger_threshold_tokens
        if gate_threshold is not None and gate_threshold > 0:
            threshold = min(threshold, max(1, int(gate_threshold * _TRIGGER_TO_GATE_RATIO)))
        return threshold

    async def init_child(self, conversation_id: str, fork_message_id: str) -> None:
        """子 Agent 创建时初始化 tracking：失败重试 1 次，仍失败抛 TaskNoteTrackingInitError。"""
        tracking = TaskNoteTracking(
            version=0,
            prefix_end_message_id=fork_message_id,
            snapshot_message_id=None,
            covered_until_message_id=None,
            note_version=0,
            inherited_processed=False,
            observing=None,
        )
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                ok = await self.repo.cas_task_note_tracking(
                    conversation_id, expected_version=0, tracking=tracking.to_dict()
                )
            except Exception as exc:  # noqa: BLE001 - 存储异常按写入失败重试
                ok = False
                last_exc = exc
            if ok:
                return
            logger.warning(
                "子会话 %s 初始化 tracking 失败 (attempt=%d)", conversation_id, attempt + 1
            )
        raise TaskNoteTrackingInitError(
            f"子 Agent 会话 tracking 初始化失败: conversation_id={conversation_id}"
        ) from last_exc

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
        gate_threshold: int | None = None,
    ) -> ObserveTicket | None:
        """U1 阶段：在 LLM 调用返回后或工具批次后检查起算范围增量并申请观察 Ticket。"""
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

            messages = list(conversation.messages)
            if not messages:
                return None
            msg_ids = [get_msg_id(m) for m in messages]

            # 2. 解析起算范围：起点中合并的原文 + 起点之后到当前末条
            range_start = resolve_range_start(
                messages,
                covered_until_message_id=tracking.covered_until_message_id,
                prefix_end_message_id=tracking.prefix_end_message_id,
            )
            start_after_mid = range_start.start_message_id

            # 3. 每轮全量重算增量（只用估算，不用 usage，不计系统提示词与工具 Schema）
            incremental_tokens = estimate_range_tokens(messages, range_start)
            threshold = self.effective_trigger_threshold(gate_threshold)

            if not force and incremental_tokens < threshold:
                logger.debug(
                    "实例 %s 增量 Token (%d) 未达阈值 (%d)，无需观察",
                    instance_id,
                    incremental_tokens,
                    threshold,
                )
                return None

            # 起点是快照消息时，切片器按该消息提取合并原文
            ticket_snapshot_mid = tracking.snapshot_message_id
            if range_start.start_is_snapshot and start_after_mid:
                ticket_snapshot_mid = start_after_mid

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
                snapshot_message_id=ticket_snapshot_mid,
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
                for attempt in range(_COMPLETE_CAS_MAX_ATTEMPTS):
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
                        tracking.inherited_processed = True
                    tracking.observing = None
                    ok = await self.repo.cas_task_note_tracking(conversation_id, version, tracking.to_dict())
                    if ok:
                        if success:
                            new_note_version = tracking.note_version
                            logger.info(
                                "观察成功完成并推进水位: instance=%s, version=%d, covered_mid=%s",
                                instance_id,
                                new_note_version,
                                covered_until_message_id,
                            )
                        else:
                            logger.warning(
                                "观察执行失败，已清理 observing (instance=%s, run_id=%s)", instance_id, run_id
                            )
                        return new_note_version
                    logger.warning(
                        "complete CAS 冲突，重读重试 (cid=%s, run_id=%s, attempt=%d)",
                        conversation_id,
                        run_id,
                        attempt + 1,
                    )
                logger.error(
                    "complete CAS 连续 %d 次失败 (cid=%s, run_id=%s)",
                    _COMPLETE_CAS_MAX_ATTEMPTS,
                    conversation_id,
                    run_id,
                )
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

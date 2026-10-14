# -*- coding: utf-8 -*-
"""LLM semantic runtime-event bounded publish admission and coalescing."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import weakref
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from agent_runtime.infrastructure.runtime_task_registry import (
    RuntimeTaskRegistry,
    get_runtime_task_registry,
)

logger = logging.getLogger(__name__)

_CONTENT_DELTA_EVENT_TYPE = "llm.content.delta"


def _validate_limit_pair(label: str, maximum: int, reserved: int) -> None:
    if maximum <= 0:
        raise ValueError(f"{label} maximum must be positive.")
    if reserved < 0 or reserved >= maximum:
        raise ValueError(f"{label} reserved capacity must be in [0, maximum).")


@dataclass(frozen=True, slots=True)
class LlmRuntimeEventPublishAdmissionLimits:
    """Pending communication admission limits for LLM semantic events."""

    request_max_pending_count: int = 64
    request_reserved_control_count: int = 16
    request_max_pending_bytes: int = 512 * 1024
    request_reserved_control_bytes: int = 128 * 1024
    global_max_pending_count: int = 1024
    global_reserved_control_count: int = 128
    global_max_pending_bytes: int = 16 * 1024 * 1024
    global_reserved_control_bytes: int = 2 * 1024 * 1024
    # Oversized payloads cannot be charged against the byte budget, so cap their
    # simultaneous inline publishes separately across every request on one loop.
    global_max_oversized_inline_count: int = 8
    global_reserved_control_oversized_inline_count: int = 2

    def __post_init__(self) -> None:
        _validate_limit_pair(
            "request pending count",
            self.request_max_pending_count,
            self.request_reserved_control_count,
        )
        _validate_limit_pair(
            "request pending bytes",
            self.request_max_pending_bytes,
            self.request_reserved_control_bytes,
        )
        _validate_limit_pair(
            "global pending count",
            self.global_max_pending_count,
            self.global_reserved_control_count,
        )
        _validate_limit_pair(
            "global pending bytes",
            self.global_max_pending_bytes,
            self.global_reserved_control_bytes,
        )
        _validate_limit_pair(
            "global oversized inline count",
            self.global_max_oversized_inline_count,
            self.global_reserved_control_oversized_inline_count,
        )

    @property
    def request_content_pending_count(self) -> int:
        return self.request_max_pending_count - self.request_reserved_control_count

    @property
    def request_content_pending_bytes(self) -> int:
        return self.request_max_pending_bytes - self.request_reserved_control_bytes

    @property
    def global_content_pending_count(self) -> int:
        return self.global_max_pending_count - self.global_reserved_control_count

    @property
    def global_content_pending_bytes(self) -> int:
        return self.global_max_pending_bytes - self.global_reserved_control_bytes

    @property
    def global_content_oversized_inline_count(self) -> int:
        return (
            self.global_max_oversized_inline_count
            - self.global_reserved_control_oversized_inline_count
        )


DEFAULT_LLM_RUNTIME_EVENT_PUBLISH_ADMISSION_LIMITS = LlmRuntimeEventPublishAdmissionLimits()

PublishRuntimeEvent = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class _PendingRuntimeEvent:
    event_type: str
    payload: dict[str, Any]
    payload_size: int
    coalesced_event_count: int = 1
    content_delta_parts: list[str] | None = None

    def materialize_payload(self) -> dict[str, Any]:
        if self.content_delta_parts is None or len(self.content_delta_parts) == 1:
            return self.payload
        return {**self.payload, "delta": "".join(self.content_delta_parts)}


@dataclass(frozen=True, slots=True)
class _AdmissionPlan:
    mode: str
    target: _PendingRuntimeEvent | None
    payload: dict[str, Any]
    count_delta: int
    bytes_delta: int
    resulting_size: int


class _GlobalRuntimeEventPublishBudget:
    """One event-loop-wide budget shared by every active LLM request."""

    def __init__(self, limits: LlmRuntimeEventPublishAdmissionLimits) -> None:
        self._limits = limits
        self._condition = asyncio.Condition()
        self._pending_count = 0
        self._pending_bytes = 0
        self._peak_pending_count = 0
        self._peak_pending_bytes = 0
        self._wait_count = 0
        self._admitted_message_count = 0
        self._admitted_bytes = 0

    async def acquire(
        self,
        *,
        count_delta: int,
        bytes_delta: int,
        is_control: bool,
    ) -> tuple[bool, dict[str, Any]]:
        waited = False
        async with self._condition:
            while not self._fits(count_delta=count_delta, bytes_delta=bytes_delta, is_control=is_control):
                if not waited:
                    waited = True
                    self._wait_count += 1
                await self._condition.wait()
            self._pending_count += count_delta
            self._pending_bytes += bytes_delta
            self._peak_pending_count = max(self._peak_pending_count, self._pending_count)
            self._peak_pending_bytes = max(self._peak_pending_bytes, self._pending_bytes)
            self._admitted_message_count += count_delta
            self._admitted_bytes += bytes_delta
            return waited, self.snapshot()

    async def release(self, *, count_delta: int, bytes_delta: int) -> dict[str, Any]:
        async with self._condition:
            self._pending_count = max(0, self._pending_count - count_delta)
            self._pending_bytes = max(0, self._pending_bytes - bytes_delta)
            snapshot = self.snapshot()
            self._condition.notify_all()
            return snapshot

    def snapshot(self) -> dict[str, Any]:
        return {
            "pending_count": self._pending_count,
            "pending_bytes": self._pending_bytes,
            "peak_pending_count": self._peak_pending_count,
            "peak_pending_bytes": self._peak_pending_bytes,
            "wait_count": self._wait_count,
            "admitted_message_count": self._admitted_message_count,
            "admitted_bytes": self._admitted_bytes,
            "max_pending_count": self._limits.global_max_pending_count,
            "reserved_control_count": self._limits.global_reserved_control_count,
            "max_pending_bytes": self._limits.global_max_pending_bytes,
            "reserved_control_bytes": self._limits.global_reserved_control_bytes,
        }

    def _fits(self, *, count_delta: int, bytes_delta: int, is_control: bool) -> bool:
        count_limit = (
            self._limits.global_max_pending_count
            if is_control
            else self._limits.global_content_pending_count
        )
        byte_limit = (
            self._limits.global_max_pending_bytes
            if is_control
            else self._limits.global_content_pending_bytes
        )
        return (
            self._pending_count + count_delta <= count_limit
            and self._pending_bytes + bytes_delta <= byte_limit
        )


class _GlobalOversizedRuntimeEventPublishAdmission:
    """One event-loop-wide token gate for payloads larger than byte admission allows."""

    def __init__(self, limits: LlmRuntimeEventPublishAdmissionLimits) -> None:
        self._limits = limits
        self._condition = asyncio.Condition()
        self._active_count = 0
        self._active_content_count = 0
        self._active_control_count = 0
        self._peak_active_count = 0
        self._wait_count = 0
        self._admitted_count = 0

    async def acquire(self, *, is_control: bool) -> tuple[bool, dict[str, Any]]:
        waited = False
        async with self._condition:
            while not self._fits(is_control=is_control):
                if not waited:
                    waited = True
                    self._wait_count += 1
                await self._condition.wait()
            self._active_count += 1
            if is_control:
                self._active_control_count += 1
            else:
                self._active_content_count += 1
            self._peak_active_count = max(self._peak_active_count, self._active_count)
            self._admitted_count += 1
            return waited, self.snapshot()

    async def release(self, *, is_control: bool) -> dict[str, Any]:
        async with self._condition:
            self._active_count = max(0, self._active_count - 1)
            if is_control:
                self._active_control_count = max(0, self._active_control_count - 1)
            else:
                self._active_content_count = max(0, self._active_content_count - 1)
            snapshot = self.snapshot()
            self._condition.notify_all()
            return snapshot

    def snapshot(self) -> dict[str, Any]:
        return {
            "active_count": self._active_count,
            "active_content_count": self._active_content_count,
            "active_control_count": self._active_control_count,
            "peak_active_count": self._peak_active_count,
            "wait_count": self._wait_count,
            "admitted_count": self._admitted_count,
            "max_active_count": self._limits.global_max_oversized_inline_count,
            "reserved_control_count": self._limits.global_reserved_control_oversized_inline_count,
        }

    def _fits(self, *, is_control: bool) -> bool:
        limit = (
            self._limits.global_max_oversized_inline_count
            if is_control
            else self._limits.global_content_oversized_inline_count
        )
        return self._active_count < limit


_GLOBAL_BUDGETS_LOCK = threading.RLock()
_GLOBAL_BUDGETS: weakref.WeakSet[_GlobalRuntimeEventPublishBudget] = weakref.WeakSet()
_GLOBAL_OVERSIZED_INLINE_ADMISSIONS: weakref.WeakSet[_GlobalOversizedRuntimeEventPublishAdmission] = weakref.WeakSet()
_LOOP_BUDGETS_ATTRIBUTE = "_agent_runtime_llm_publish_budgets"
_LOOP_OVERSIZED_INLINE_ADMISSIONS_ATTRIBUTE = "_agent_runtime_llm_publish_oversized_inline_admissions"


def _global_budget_for_running_loop(
    limits: LlmRuntimeEventPublishAdmissionLimits,
) -> _GlobalRuntimeEventPublishBudget:
    loop = asyncio.get_running_loop()
    with _GLOBAL_BUDGETS_LOCK:
        budgets_by_limits = getattr(loop, _LOOP_BUDGETS_ATTRIBUTE, None)
        if budgets_by_limits is None:
            budgets_by_limits = {}
            setattr(loop, _LOOP_BUDGETS_ATTRIBUTE, budgets_by_limits)
        budget = budgets_by_limits.get(limits)
        if budget is None:
            budget = _GlobalRuntimeEventPublishBudget(limits)
            budgets_by_limits[limits] = budget
            _GLOBAL_BUDGETS.add(budget)
        return budget


def _global_oversized_inline_admission_for_running_loop(
    limits: LlmRuntimeEventPublishAdmissionLimits,
) -> _GlobalOversizedRuntimeEventPublishAdmission:
    loop = asyncio.get_running_loop()
    with _GLOBAL_BUDGETS_LOCK:
        admissions_by_limits = getattr(loop, _LOOP_OVERSIZED_INLINE_ADMISSIONS_ATTRIBUTE, None)
        if admissions_by_limits is None:
            admissions_by_limits = {}
            setattr(loop, _LOOP_OVERSIZED_INLINE_ADMISSIONS_ATTRIBUTE, admissions_by_limits)
        admission = admissions_by_limits.get(limits)
        if admission is None:
            admission = _GlobalOversizedRuntimeEventPublishAdmission(limits)
            admissions_by_limits[limits] = admission
            _GLOBAL_OVERSIZED_INLINE_ADMISSIONS.add(admission)
        return admission


def llm_runtime_event_publish_admission_snapshot() -> dict[str, Any]:
    """Return current process observations for all live event-loop budgets."""
    with _GLOBAL_BUDGETS_LOCK:
        snapshots = [budget.snapshot() for budget in _GLOBAL_BUDGETS]
        oversized_inline_snapshots = [admission.snapshot() for admission in _GLOBAL_OVERSIZED_INLINE_ADMISSIONS]
    return {
        "event_loop_count": len(snapshots),
        "pending_count": sum(int(item["pending_count"]) for item in snapshots),
        "pending_bytes": sum(int(item["pending_bytes"]) for item in snapshots),
        "peak_pending_count": sum(int(item["peak_pending_count"]) for item in snapshots),
        "peak_pending_bytes": sum(int(item["peak_pending_bytes"]) for item in snapshots),
        "wait_count": sum(int(item["wait_count"]) for item in snapshots),
        "budgets": snapshots,
        "oversized_inline": {
            "event_loop_count": len(oversized_inline_snapshots),
            "active_count": sum(int(item["active_count"]) for item in oversized_inline_snapshots),
            "peak_active_count": sum(int(item["peak_active_count"]) for item in oversized_inline_snapshots),
            "wait_count": sum(int(item["wait_count"]) for item in oversized_inline_snapshots),
            "admissions": oversized_inline_snapshots,
        },
    }


class LlmRuntimeEventPublishQueue:
    """Per-request single-drain queue with a latest pending content aggregate."""

    def __init__(
        self,
        *,
        publish: PublishRuntimeEvent,
        conversation_id: str,
        llm_request_id: str,
        limits: LlmRuntimeEventPublishAdmissionLimits = DEFAULT_LLM_RUNTIME_EVENT_PUBLISH_ADMISSION_LIMITS,
        global_budget: _GlobalRuntimeEventPublishBudget | None = None,
        task_registry: RuntimeTaskRegistry | None = None,
    ) -> None:
        self._publish = publish
        self._conversation_id = conversation_id
        self._llm_request_id = llm_request_id
        self._limits = limits
        self._global_budget = global_budget or _global_budget_for_running_loop(limits)
        self._global_oversized_inline_admission = _global_oversized_inline_admission_for_running_loop(limits)
        self._task_registry = task_registry or get_runtime_task_registry()
        self._condition = asyncio.Condition()
        self._queue: deque[_PendingRuntimeEvent] = deque()
        self._pending_count = 0
        self._pending_bytes = 0
        self._peak_pending_count = 0
        self._peak_pending_bytes = 0
        self._submitted_event_count = 0
        self._coalesced_content_event_count = 0
        self._published_message_count = 0
        self._failed_message_count = 0
        self._local_wait_count = 0
        self._global_wait_count = 0
        self._global_oversized_inline_wait_count = 0
        self._oversized_inline_count = 0
        self._inline_publish_active = False
        self._closed = False
        self._drain_task: asyncio.Task[None] | None = None
        self._closed_event = asyncio.Event()

    async def submit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Admit one semantic event without dropping control or terminal messages."""
        actual_payload = dict(payload)
        payload_size = _payload_size_bytes(actual_payload)
        is_control = event_type != _CONTENT_DELTA_EVENT_TYPE
        if payload_size > self._inline_threshold_bytes(is_control=is_control):
            await self._publish_oversized_inline(
                event_type,
                actual_payload,
                payload_size,
                is_control=is_control,
            )
            return
        await self._submit_bounded(
            event_type=event_type,
            payload=actual_payload,
            is_control=is_control,
        )

    async def close(self) -> None:
        """Stop admission and wait until every admitted message leaves the drain."""
        async with self._condition:
            self._closed = True
            self._condition.notify_all()
            if self._drain_task is None and self._pending_count == 0 and not self._inline_publish_active:
                self._closed_event.set()
        await self._closed_event.wait()

    async def wait_closed(self) -> None:
        await self._closed_event.wait()

    def snapshot(self) -> dict[str, Any]:
        return {
            "conversation_id": self._conversation_id,
            "llm_request_id": self._llm_request_id,
            "closed": self._closed,
            "pending_count": self._pending_count,
            "pending_bytes": self._pending_bytes,
            "queued_message_count": len(self._queue),
            "peak_pending_count": self._peak_pending_count,
            "peak_pending_bytes": self._peak_pending_bytes,
            "submitted_event_count": self._submitted_event_count,
            "coalesced_content_event_count": self._coalesced_content_event_count,
            "published_message_count": self._published_message_count,
            "failed_message_count": self._failed_message_count,
            "local_wait_count": self._local_wait_count,
            "global_wait_count": self._global_wait_count,
            "global_oversized_inline_wait_count": self._global_oversized_inline_wait_count,
            "oversized_inline_count": self._oversized_inline_count,
            "request_max_pending_count": self._limits.request_max_pending_count,
            "request_reserved_control_count": self._limits.request_reserved_control_count,
            "request_max_pending_bytes": self._limits.request_max_pending_bytes,
            "request_reserved_control_bytes": self._limits.request_reserved_control_bytes,
            "global": self._global_budget.snapshot(),
            "global_oversized_inline": self._global_oversized_inline_admission.snapshot(),
        }

    async def _submit_bounded(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        is_control: bool,
    ) -> None:
        local_wait_logged = False
        while True:
            async with self._condition:
                self._raise_if_closed()
                while self._inline_publish_active:
                    if not local_wait_logged:
                        local_wait_logged = True
                        self._record_local_wait(event_type, phase="inline_publish_active")
                    await self._condition.wait()
                    self._raise_if_closed()
                plan = self._build_plan(event_type, payload)
                if not self._fits_local(plan, is_control=is_control):
                    if not local_wait_logged:
                        local_wait_logged = True
                        self._record_local_wait(event_type, phase="request_capacity")
                    await self._condition.wait()
                    continue
            global_waited, _global_snapshot = await self._global_budget.acquire(
                count_delta=plan.count_delta,
                bytes_delta=plan.bytes_delta,
                is_control=is_control,
            )
            if global_waited:
                self._global_wait_count += 1
                self._log_admission_wait(event_type, phase="global_capacity")
            applied = False
            closed = False
            try:
                async with self._condition:
                    closed = self._closed
                    if not closed and not self._inline_publish_active:
                        current_plan = self._build_plan(event_type, payload)
                        if self._plans_equivalent(plan, current_plan) and self._fits_local(
                            current_plan,
                            is_control=is_control,
                        ):
                            self._apply_plan(event_type, current_plan)
                            self._ensure_drain_task_locked()
                            self._condition.notify_all()
                            applied = True
            finally:
                if not applied:
                    await self._global_budget.release(
                        count_delta=plan.count_delta,
                        bytes_delta=plan.bytes_delta,
                    )
            if applied:
                return
            if closed:
                self._raise_if_closed()

    async def _publish_oversized_inline(
        self,
        event_type: str,
        payload: dict[str, Any],
        payload_size: int,
        *,
        is_control: bool,
    ) -> None:
        logger.warning(
            "LLM runtime event exceeds pending byte budget; publishing through bounded inline admission: "
            "conversation_id=%s llm_request_id=%s event_type=%s payload_bytes=%s",
            self._conversation_id,
            self._llm_request_id,
            event_type,
            payload_size,
        )
        async with self._condition:
            self._raise_if_closed()
            while self._pending_count > 0 or self._inline_publish_active:
                self._record_local_wait(event_type, phase="oversized_inline_barrier")
                await self._condition.wait()
                self._raise_if_closed()
            self._inline_publish_active = True
            self._submitted_event_count += 1
            self._oversized_inline_count += 1
        admission_acquired = False
        try:
            global_waited, _global_snapshot = await self._global_oversized_inline_admission.acquire(
                is_control=is_control,
            )
            admission_acquired = True
            if global_waited:
                self._global_oversized_inline_wait_count += 1
                self._log_admission_wait(event_type, phase="global_oversized_inline_capacity")
            await self._publish(event_type, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed_message_count += 1
            logger.error(
                "Oversized LLM runtime event inline publish failed: "
                "conversation_id=%s llm_request_id=%s event_type=%s error_type=%s",
                self._conversation_id,
                self._llm_request_id,
                event_type,
                exc.__class__.__name__,
            )
        else:
            self._published_message_count += 1
        finally:
            if admission_acquired:
                await self._global_oversized_inline_admission.release(is_control=is_control)
            async with self._condition:
                self._inline_publish_active = False
                self._condition.notify_all()
                if self._closed and self._drain_task is None and self._pending_count == 0:
                    self._closed_event.set()

    def _build_plan(self, event_type: str, payload: dict[str, Any]) -> _AdmissionPlan:
        incoming_delta = payload.get("delta")
        if event_type == _CONTENT_DELTA_EVENT_TYPE and isinstance(incoming_delta, str) and self._queue:
            tail = self._queue[-1]
            if (
                tail.event_type == _CONTENT_DELTA_EVENT_TYPE
                and tail.content_delta_parts is not None
                and _content_delta_key(tail.payload) == _content_delta_key(payload)
            ):
                bytes_delta = _json_string_content_size_bytes(incoming_delta)
                return _AdmissionPlan(
                    mode="merge",
                    target=tail,
                    payload=payload,
                    count_delta=0,
                    bytes_delta=bytes_delta,
                    resulting_size=tail.payload_size + bytes_delta,
                )
        payload_size = _payload_size_bytes(payload)
        return _AdmissionPlan(
            mode="append",
            target=None,
            payload=payload,
            count_delta=1,
            bytes_delta=payload_size,
            resulting_size=payload_size,
        )

    def _fits_local(self, plan: _AdmissionPlan, *, is_control: bool) -> bool:
        count_limit = (
            self._limits.request_max_pending_count
            if is_control
            else self._limits.request_content_pending_count
        )
        byte_limit = (
            self._limits.request_max_pending_bytes
            if is_control
            else self._limits.request_content_pending_bytes
        )
        return (
            self._pending_count + plan.count_delta <= count_limit
            and self._pending_bytes + plan.bytes_delta <= byte_limit
        )

    def _apply_plan(self, event_type: str, plan: _AdmissionPlan) -> None:
        self._submitted_event_count += 1
        if plan.mode == "merge":
            target = plan.target
            incoming_delta = plan.payload.get("delta")
            if target is None or target.content_delta_parts is None or not isinstance(incoming_delta, str):
                raise RuntimeError("Content merge plan is missing a compatible target.")
            target.content_delta_parts.append(incoming_delta)
            target.payload_size = plan.resulting_size
            target.coalesced_event_count += 1
            self._coalesced_content_event_count += 1
        else:
            content_delta = plan.payload.get("delta")
            self._queue.append(
                _PendingRuntimeEvent(
                    event_type=event_type,
                    payload=plan.payload,
                    payload_size=plan.resulting_size,
                    content_delta_parts=(
                        [content_delta]
                        if event_type == _CONTENT_DELTA_EVENT_TYPE and isinstance(content_delta, str)
                        else None
                    ),
                )
            )
            self._pending_count += 1
        self._pending_bytes += plan.bytes_delta
        self._peak_pending_count = max(self._peak_pending_count, self._pending_count)
        self._peak_pending_bytes = max(self._peak_pending_bytes, self._pending_bytes)

    def _ensure_drain_task_locked(self) -> None:
        if self._drain_task is not None:
            return
        self._drain_task = self._task_registry.create_task(
            self._drain(),
            task_name=f"llm-runtime-event-drain-{_safe_label(self._llm_request_id)}",
            owner_type="llm_request_service",
            owner_id=self._llm_request_id,
            task_class="runtime_event_publish_drain",
            diagnostic_context={
                "phase": "runtime_event_publish_drain.scheduled",
                **self.snapshot(),
            },
        )

    async def _drain(self) -> None:
        current_task = asyncio.current_task()
        try:
            while True:
                async with self._condition:
                    while not self._queue:
                        if self._closed:
                            return
                        await self._condition.wait()
                    pending = self._queue.popleft()
                failed = False
                try:
                    await self._publish(pending.event_type, pending.materialize_payload())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed = True
                    self._failed_message_count += 1
                    logger.error(
                        "LLM runtime event drain publish failed: "
                        "conversation_id=%s llm_request_id=%s event_type=%s "
                        "coalesced_event_count=%s error_type=%s",
                        self._conversation_id,
                        self._llm_request_id,
                        pending.event_type,
                        pending.coalesced_event_count,
                        exc.__class__.__name__,
                    )
                else:
                    self._published_message_count += 1
                finally:
                    async with self._condition:
                        self._pending_count = max(0, self._pending_count - 1)
                        self._pending_bytes = max(0, self._pending_bytes - pending.payload_size)
                        self._condition.notify_all()
                    await self._global_budget.release(
                        count_delta=1,
                        bytes_delta=pending.payload_size,
                    )
                    if current_task is not None:
                        self._task_registry.mark_progress(
                            current_task,
                            diagnostic_context={
                                "phase": (
                                    "runtime_event_publish_drain.failed"
                                    if failed
                                    else "runtime_event_publish_drain.published"
                                ),
                                "last_event_type": pending.event_type,
                                "last_coalesced_event_count": pending.coalesced_event_count,
                                **self.snapshot(),
                            },
                        )
        finally:
            await self._release_queued_after_drain_exit()
            self._closed_event.set()

    async def _release_queued_after_drain_exit(self) -> None:
        async with self._condition:
            remaining = list(self._queue)
            self._queue.clear()
            for pending in remaining:
                self._pending_count = max(0, self._pending_count - 1)
                self._pending_bytes = max(0, self._pending_bytes - pending.payload_size)
            self._condition.notify_all()
        for pending in remaining:
            await self._global_budget.release(
                count_delta=1,
                bytes_delta=pending.payload_size,
            )
        if remaining:
            logger.warning(
                "LLM runtime event drain exited with queued messages released: "
                "conversation_id=%s llm_request_id=%s released_count=%s",
                self._conversation_id,
                self._llm_request_id,
                len(remaining),
            )

    def _plans_equivalent(self, left: _AdmissionPlan, right: _AdmissionPlan) -> bool:
        return (
            left.mode == right.mode
            and left.target is right.target
            and left.count_delta == right.count_delta
            and left.bytes_delta == right.bytes_delta
            and left.resulting_size == right.resulting_size
        )

    def _inline_threshold_bytes(self, *, is_control: bool) -> int:
        request_limit = (
            self._limits.request_max_pending_bytes
            if is_control
            else self._limits.request_content_pending_bytes
        )
        global_limit = (
            self._limits.global_max_pending_bytes
            if is_control
            else self._limits.global_content_pending_bytes
        )
        return min(request_limit, global_limit)

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise RuntimeError(f"LLM runtime event publish queue is closed: {self._llm_request_id}")

    def _record_local_wait(self, event_type: str, *, phase: str) -> None:
        self._local_wait_count += 1
        self._log_admission_wait(event_type, phase=phase)

    def _log_admission_wait(self, event_type: str, *, phase: str) -> None:
        snapshot = self.snapshot()
        logger.warning(
            "LLM runtime event publish admission waiting: "
            "conversation_id=%s llm_request_id=%s event_type=%s phase=%s "
            "pending_count=%s pending_bytes=%s global_pending_count=%s global_pending_bytes=%s",
            self._conversation_id,
            self._llm_request_id,
            event_type,
            phase,
            snapshot["pending_count"],
            snapshot["pending_bytes"],
            snapshot["global"]["pending_count"],
            snapshot["global"]["pending_bytes"],
        )


def _payload_size_bytes(payload: dict[str, Any]) -> int:
    try:
        serialized = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:
        serialized = repr(payload)
    return len(serialized.encode("utf-8", errors="replace"))


def _json_string_content_size_bytes(value: str) -> int:
    serialized = json.dumps(value, ensure_ascii=False)
    return len(serialized[1:-1].encode("utf-8", errors="replace"))


def _content_delta_key(payload: dict[str, Any]) -> tuple[Any, ...]:
    return (
        payload.get("llm_call_id"),
        payload.get("llm_request_id"),
        payload.get("candidate_index"),
        payload.get("part_id"),
        payload.get("content_index"),
        payload.get("provider_event_type"),
    )


def _safe_label(value: str, *, max_length: int = 80) -> str:
    safe = "".join(char if char.isalnum() or char in {".", "_", "-"} else "-" for char in value)
    return (safe.strip("-._") or "unknown")[:max_length]

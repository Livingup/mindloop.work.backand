# -*- coding: utf-8 -*-
"""AgentInstance 执行循环滚动诊断日志。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from agent_runtime.infrastructure.asyncio_diagnostics import monitored_to_thread

logger = logging.getLogger(__name__)

_DEFAULT_RETAINED_FRAME_COUNT = 3
_DEFAULT_QUEUE_MAX_SIZE = 4096
_DEFAULT_RETAINED_INSTANCE_EVENT_COUNT = 128
_PREVIEW_MAX_STRING_LENGTH = 1200
_PREVIEW_MAX_LIST_ITEMS = 24
_PREVIEW_MAX_DICT_ITEMS = 48
_PREVIEW_MAX_DEPTH = 4


@dataclass(slots=True)
class _ExecutionLogStep:
    """单个诊断步骤。"""

    sequence: int
    timestamp: str
    elapsed_ms: int | None
    stage: str
    status: str
    summary: str
    details: Any = field(default_factory=dict)
    expected_next: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "elapsed_ms": self.elapsed_ms,
            "stage": self.stage,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
        }
        if self.expected_next:
            data["expected_next"] = self.expected_next
        return data


@dataclass(slots=True)
class _ExecutionLogFrame:
    """一个 AgentInstance 执行循环 frame。"""

    frame_id: str
    loop_index: int
    task_id: str
    instance_id: str
    stream_id: str | None
    started_at: str
    monotonic_started_at: float
    metadata: dict[str, Any] = field(default_factory=dict)
    steps: list[_ExecutionLogStep] = field(default_factory=list)
    ended_at: str | None = None
    duration_ms: int | None = None
    outcome: str | None = None
    final_summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "loop_index": self.loop_index,
            "task_id": self.task_id,
            "instance_id": self.instance_id,
            "stream_id": self.stream_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "outcome": self.outcome,
            "final_summary": self.final_summary,
            "metadata": self.metadata,
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass(slots=True)
class _ExecutionLogInstanceState:
    """单个 AgentInstance 的滚动日志内存状态。"""

    instance_id: str
    frames: OrderedDict[str, _ExecutionLogFrame] = field(default_factory=OrderedDict)
    active_frame_id: str | None = None
    instance_events: list[_ExecutionLogStep] = field(default_factory=list)
    dropped_step_count: int = 0
    last_write_error: str | None = None
    last_write_error_at: str | None = None


class AgentExecutionLoopLogger:
    """非阻塞的 AgentInstance 执行循环滚动诊断日志。

    热路径只更新内存并尝试向有界队列投递 flush 信号；文件写入由后台任务通过线程池执行。
    即使队列满、日志目录不可写或后台写入失败，Agent 业务执行也不会等待或失败。
    """

    def __init__(
        self,
        trace_dir_accessor: Callable[[], str | Path],
        *,
        retained_frame_count: int = _DEFAULT_RETAINED_FRAME_COUNT,
        queue_max_size: int = _DEFAULT_QUEUE_MAX_SIZE,
    ) -> None:
        self._trace_dir_accessor = trace_dir_accessor
        self._retained_frame_count = max(int(retained_frame_count), 1)
        self._queue_max_size = max(int(queue_max_size), 1)
        self._states: dict[str, _ExecutionLogInstanceState] = {}
        self._lock = RLock()
        self._sequence = 0
        self._flush_queue: asyncio.Queue[str] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._started = False
        self._dropped_flush_signal_count = 0

    def start(self) -> None:
        """在当前事件循环启动后台 flush worker。重复调用安全。"""
        if self._started and self._worker_task is not None and not self._worker_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Agent 执行循环诊断日志无法启动：当前没有运行中的 asyncio event loop。")
            return
        self._flush_queue = asyncio.Queue(maxsize=self._queue_max_size)
        self._worker_task = loop.create_task(self._flush_worker(), name="agent-execution-loop-log-writer")
        self._started = True

    async def stop(self) -> None:
        """停止后台 flush worker。关闭时尽力写出内存中的最新状态。"""
        task = self._worker_task
        self._worker_task = None
        self._started = False
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Agent 执行循环诊断日志后台任务停止失败。")
        await self.flush_all()

    async def flush_all(self) -> None:
        """尽力把所有实例的最新滚动日志写入文件。用于服务关闭，不参与热路径。"""
        with self._lock:
            instance_ids = tuple(self._states)
        for instance_id in instance_ids:
            await self._write_instance_snapshot(instance_id)

    def begin_frame(
        self,
        *,
        task_id: str,
        instance_id: str,
        loop_index: int,
        conversation_id: str | None,
        stream_id: str | None,
        details: dict[str, Any] | None = None,
        expected_next: str | None = None,
    ) -> str:
        """开始记录一个 frame，并只保留该实例最近 N 个 frame。"""
        frame_id = f"{instance_id}:frame:{loop_index}"
        now = _utc_now_iso()
        started_at = time.perf_counter()
        with self._lock:
            state = self._state_for(instance_id)
            frame_metadata = _initial_frame_metadata(conversation_id=conversation_id)
            if frame_id in state.frames:
                frame = state.frames[frame_id]
                frame.stream_id = stream_id
                frame.started_at = now
                frame.monotonic_started_at = started_at
                frame.metadata = frame_metadata
                frame.ended_at = None
                frame.duration_ms = None
                frame.outcome = None
                frame.final_summary = None
                frame.steps.clear()
                state.frames.move_to_end(frame_id)
            else:
                frame = _ExecutionLogFrame(
                    frame_id=frame_id,
                    loop_index=loop_index,
                    task_id=task_id,
                    instance_id=instance_id,
                    stream_id=stream_id,
                    started_at=now,
                    monotonic_started_at=started_at,
                    metadata=frame_metadata,
                )
                state.frames[frame_id] = frame
            state.active_frame_id = frame_id
            self._trim_frames_locked(state)
        self.record_step(
            instance_id=instance_id,
            stage="frame.start",
            status="started",
            summary="AgentInstance 执行循环 frame 开始。",
            details=details or {},
            expected_next=expected_next,
            frame_id=frame_id,
        )
        return frame_id

    def end_frame(
        self,
        *,
        instance_id: str,
        frame_id: str | None = None,
        outcome: str,
        summary: str,
        details: dict[str, Any] | None = None,
        expected_next: str | None = None,
    ) -> None:
        """结束当前 frame 并触发异步 flush。"""
        target_frame_id = frame_id
        with self._lock:
            state = self._states.get(instance_id)
            if state is not None and target_frame_id is None:
                target_frame_id = state.active_frame_id
        self.record_step(
            instance_id=instance_id,
            frame_id=target_frame_id,
            stage="frame.end",
            status=outcome,
            summary=summary,
            details=details or {},
            expected_next=expected_next,
        )
        with self._lock:
            state = self._states.get(instance_id)
            frame = self._frame_for_locked(state, target_frame_id) if state is not None else None
            if frame is None:
                return
            frame.ended_at = _utc_now_iso()
            frame.duration_ms = int(max(time.perf_counter() - frame.monotonic_started_at, 0) * 1000)
            frame.outcome = outcome
            frame.final_summary = summary
            if state.active_frame_id == frame.frame_id:
                state.active_frame_id = None
        self.request_flush(instance_id)

    def record_step(
        self,
        *,
        instance_id: str,
        stage: str,
        status: str,
        summary: str,
        details: dict[str, Any] | None = None,
        expected_next: str | None = None,
        frame_id: str | None = None,
    ) -> None:
        """记录一个诊断步骤。失败不抛出异常。"""
        try:
            sanitized_details = _sanitize_for_log(details or {})
            with self._lock:
                state = self._state_for(instance_id)
                frame = self._frame_for_locked(state, frame_id or state.active_frame_id)
                if frame is None:
                    state.dropped_step_count += 1
                    return
                self._sequence += 1
                frame.steps.append(
                    _ExecutionLogStep(
                        sequence=self._sequence,
                        timestamp=_utc_now_iso(),
                        elapsed_ms=int(max(time.perf_counter() - frame.monotonic_started_at, 0) * 1000),
                        stage=stage,
                        status=status,
                        summary=summary,
                        details=sanitized_details,
                        expected_next=expected_next,
                    )
                )
            self.request_flush(instance_id)
        except Exception:  # noqa: BLE001 - 诊断日志不能影响业务执行
            logger.exception("记录 Agent 执行循环诊断步骤失败: instance_id=%s stage=%s", instance_id, stage)

    def update_frame_metadata(
        self,
        *,
        instance_id: str,
        frame_id: str | None = None,
        updates: dict[str, Any] | None = None,
    ) -> None:
        """更新当前 frame 的诊断元数据。失败不影响业务执行。"""
        if not updates:
            return
        try:
            sanitized_updates = _sanitize_for_log(updates)
            with self._lock:
                state = self._state_for(instance_id)
                frame = self._frame_for_locked(state, frame_id or state.active_frame_id)
                if frame is None:
                    state.dropped_step_count += 1
                    return
                frame.metadata.update(sanitized_updates)
            self.request_flush(instance_id)
        except Exception:  # noqa: BLE001 - 诊断日志不能影响业务执行
            logger.exception("更新 Agent 执行循环 frame 元数据失败: instance_id=%s", instance_id)

    def record_llm_attempt(
        self,
        *,
        instance_id: str,
        frame_id: str | None = None,
        attempt: dict[str, Any],
    ) -> None:
        """把一次 LLM request attempt 关联到当前执行循环 frame。"""
        try:
            sanitized_attempt = _sanitize_for_log(_llm_attempt_metadata(attempt))
            llm_request_id = sanitized_attempt.get("llm_request_id")
            llm_call_id = sanitized_attempt.get("llm_call_id")
            with self._lock:
                state = self._state_for(instance_id)
                frame = self._frame_for_locked(state, frame_id or state.active_frame_id)
                if frame is None:
                    state.dropped_step_count += 1
                    return
                metadata = _ensure_frame_metadata(frame.metadata)
                if llm_call_id:
                    metadata["llm_call_id"] = llm_call_id
                if llm_request_id:
                    metadata["active_llm_request_id"] = llm_request_id
                    request_ids = metadata.setdefault("llm_request_ids", [])
                    if llm_request_id not in request_ids:
                        request_ids.append(llm_request_id)
                attempts = metadata.setdefault("llm_attempts", [])
                if not any(item.get("llm_request_id") == llm_request_id for item in attempts):
                    attempts.append(sanitized_attempt)
                else:
                    for index, item in enumerate(attempts):
                        if item.get("llm_request_id") == llm_request_id:
                            attempts[index] = {**item, **sanitized_attempt}
                            break
                frame.metadata = metadata
            self.request_flush(instance_id)
        except Exception:  # noqa: BLE001 - 诊断日志不能影响业务执行
            logger.exception("记录 Agent 执行循环 LLM attempt 元数据失败: instance_id=%s", instance_id)

    def record_conversation_rebind(
        self,
        *,
        instance_id: str,
        frame_id: str | None = None,
        reason: str,
        job_id: str | None,
        source_conversation_id: str | None,
        target_conversation_id: str | None,
    ) -> None:
        """把上下文压缩等 conversation rebind 写入当前 frame 元数据。"""
        try:
            rebind = _sanitize_for_log(
                {
                    "reason": reason,
                    "job_id": job_id,
                    "source_conversation_id": source_conversation_id,
                    "target_conversation_id": target_conversation_id,
                }
            )
            with self._lock:
                state = self._state_for(instance_id)
                frame = self._frame_for_locked(state, frame_id or state.active_frame_id)
                if frame is None:
                    state.dropped_step_count += 1
                    return
                metadata = _ensure_frame_metadata(frame.metadata)
                metadata["active_conversation_id"] = target_conversation_id
                metadata.setdefault("conversation_rebinds", []).append(rebind)
                frame.metadata = metadata
            self.request_flush(instance_id)
        except Exception:  # noqa: BLE001 - 诊断日志不能影响业务执行
            logger.exception("记录 Agent 执行循环 conversation rebind 元数据失败: instance_id=%s", instance_id)

    def record_instance_event(
        self,
        *,
        instance_id: str,
        stage: str,
        status: str,
        summary: str,
        details: dict[str, Any] | None = None,
        expected_next: str | None = None,
    ) -> None:
        """记录不属于某个 frame 的实例级事件，例如入队、启动和后台任务状态。"""
        try:
            sanitized_details = _sanitize_for_log(details or {})
            with self._lock:
                state = self._state_for(instance_id)
                self._sequence += 1
                state.instance_events.append(
                    _ExecutionLogStep(
                        sequence=self._sequence,
                        timestamp=_utc_now_iso(),
                        elapsed_ms=None,
                        stage=stage,
                        status=status,
                        summary=summary,
                        details=sanitized_details,
                        expected_next=expected_next,
                    )
                )
                if len(state.instance_events) > _DEFAULT_RETAINED_INSTANCE_EVENT_COUNT:
                    del state.instance_events[: len(state.instance_events) - _DEFAULT_RETAINED_INSTANCE_EVENT_COUNT]
            self.request_flush(instance_id)
        except Exception:  # noqa: BLE001 - 诊断日志不能影响业务执行
            logger.exception("记录 Agent 执行循环实例事件失败: instance_id=%s stage=%s", instance_id, stage)

    def request_flush(self, instance_id: str) -> None:
        """非阻塞请求后台写文件。队列不可用或已满时直接丢弃 flush 信号。"""
        queue = self._flush_queue
        if queue is None:
            return
        try:
            queue.put_nowait(instance_id)
        except asyncio.QueueFull:
            self._dropped_flush_signal_count += 1
        except RuntimeError:
            return

    def log_file_path_for_instance(self, instance_id: str) -> Path:
        """返回某个 AgentInstance 的滚动日志文件路径。"""
        return self._log_dir() / f"{_safe_filename(instance_id)}.log"

    def _state_for(self, instance_id: str) -> _ExecutionLogInstanceState:
        state = self._states.get(instance_id)
        if state is None:
            state = _ExecutionLogInstanceState(instance_id=instance_id)
            self._states[instance_id] = state
        return state

    def _frame_for_locked(
        self,
        state: _ExecutionLogInstanceState | None,
        frame_id: str | None,
    ) -> _ExecutionLogFrame | None:
        if state is None or frame_id is None:
            return None
        return state.frames.get(frame_id)

    def _trim_frames_locked(self, state: _ExecutionLogInstanceState) -> None:
        while len(state.frames) > self._retained_frame_count:
            state.frames.popitem(last=False)

    async def _flush_worker(self) -> None:
        queue = self._flush_queue
        if queue is None:
            return
        pending: set[str] = set()
        try:
            while True:
                instance_id = await queue.get()
                pending.add(instance_id)
                queue.task_done()
                while True:
                    try:
                        instance_id = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    pending.add(instance_id)
                    queue.task_done()
                for target_instance_id in tuple(pending):
                    await self._write_instance_snapshot(target_instance_id)
                pending.clear()
        except asyncio.CancelledError:
            for target_instance_id in tuple(pending):
                await self._write_instance_snapshot(target_instance_id)
            raise

    async def _write_instance_snapshot(self, instance_id: str) -> None:
        try:
            with self._lock:
                state = self._states.get(instance_id)
                if state is None:
                    return
                snapshot = self._snapshot_locked(state)
            path = self.log_file_path_for_instance(instance_id)
            await monitored_to_thread(
                _write_snapshot_file,
                path,
                snapshot,
                operation="agent.execution_loop_logger.write_snapshot",
                caller="AgentExecutionLoopLogger._write_instance_snapshot",
                log=logger,
                diagnostic_context={
                    "instance_id": instance_id,
                    "path": str(path),
                },
            )
        except Exception as exc:  # noqa: BLE001 - 日志写失败不能影响业务执行
            with self._lock:
                state = self._states.get(instance_id)
                if state is not None:
                    state.last_write_error = str(exc) or exc.__class__.__name__
                    state.last_write_error_at = _utc_now_iso()
            logger.exception("写入 Agent 执行循环诊断日志失败: instance_id=%s", instance_id)

    def _snapshot_locked(self, state: _ExecutionLogInstanceState) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "description": "Rolling AgentInstance execution-loop diagnostic log. Frames retain metadata for active conversation, LLM request attempts, and rebind lineage.",
            "generated_at": _utc_now_iso(),
            "instance_id": state.instance_id,
            "retained_frame_count": self._retained_frame_count,
            "actual_frame_count": len(state.frames),
            "active_frame_id": state.active_frame_id,
            "log_file_path": str(self.log_file_path_for_instance(state.instance_id)),
            "writer": {
                "started": self._started,
                "queue_max_size": self._queue_max_size,
                "queue_size": self._flush_queue.qsize() if self._flush_queue is not None else None,
                "dropped_flush_signal_count": self._dropped_flush_signal_count,
                "dropped_step_count": state.dropped_step_count,
                "last_write_error": state.last_write_error,
                "last_write_error_at": state.last_write_error_at,
            },
            "instance_events": [step.to_dict() for step in state.instance_events],
            "frames": [frame.to_dict() for frame in state.frames.values()],
        }

    def _log_dir(self) -> Path:
        return Path(self._trace_dir_accessor()).expanduser() / "agent-execution-loop"


class NoopAgentExecutionLoopLogger:
    """用于测试或未配置场景的空执行循环诊断日志。"""

    def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def flush_all(self) -> None:
        return None

    def begin_frame(self, **_: Any) -> str:
        return ""

    def end_frame(self, **_: Any) -> None:
        return None

    def record_step(self, **_: Any) -> None:
        return None

    def update_frame_metadata(self, **_: Any) -> None:
        return None

    def record_llm_attempt(self, **_: Any) -> None:
        return None

    def record_conversation_rebind(self, **_: Any) -> None:
        return None

    def record_instance_event(self, **_: Any) -> None:
        return None

    def request_flush(self, _instance_id: str, *_: Any, **__: Any) -> None:
        return None

    def log_file_path_for_instance(self, instance_id: str) -> Path:
        return Path(f"{_safe_filename(instance_id)}.log")


def _initial_frame_metadata(*, conversation_id: str | None) -> dict[str, Any]:
    return {
        "initial_conversation_id": conversation_id,
        "active_conversation_id": conversation_id,
        "llm_call_id": None,
        "active_llm_request_id": None,
        "llm_request_ids": [],
        "llm_attempts": [],
        "conversation_rebinds": [],
    }


def _ensure_frame_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    normalized = dict(metadata or {})
    normalized.setdefault("initial_conversation_id", None)
    normalized.setdefault("active_conversation_id", normalized.get("initial_conversation_id"))
    normalized.setdefault("llm_call_id", None)
    normalized.setdefault("active_llm_request_id", None)
    normalized.setdefault("llm_request_ids", [])
    normalized.setdefault("llm_attempts", [])
    normalized.setdefault("conversation_rebinds", [])
    return normalized


def _llm_attempt_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "llm_request_id": payload.get("llm_request_id"),
        "llm_call_id": payload.get("llm_call_id"),
        "candidate_index": payload.get("candidate_index"),
        "api_key_index": payload.get("api_key_index"),
        "retry_index": payload.get("retry_index"),
        "provider_id": payload.get("provider_id"),
        "binding_id": payload.get("binding_id"),
        "model_id": payload.get("model_id"),
        "model_alias": payload.get("model_alias"),
        "api_format": payload.get("api_format"),
        "api_key_label": payload.get("api_key_label"),
        "attempt_count": payload.get("attempt_count"),
        "api_key_count": payload.get("api_key_count"),
        "tool_count": payload.get("tool_count"),
    }


def _write_snapshot_file(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    temp_path.replace(path)


def _sanitize_for_log(value: Any, *, _depth: int = 0) -> Any:
    if _depth >= _PREVIEW_MAX_DEPTH:
        return _preview_scalar(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        sanitized = [_sanitize_for_log(item, _depth=_depth + 1) for item in items[:_PREVIEW_MAX_LIST_ITEMS]]
        if len(items) > _PREVIEW_MAX_LIST_ITEMS:
            sanitized.append(f"... truncated {len(items) - _PREVIEW_MAX_LIST_ITEMS} item(s)")
        return sanitized
    if isinstance(value, dict):
        sanitized_dict: dict[str, Any] = {}
        items = list(value.items())
        for key, item in items[:_PREVIEW_MAX_DICT_ITEMS]:
            sanitized_dict[str(key)] = _sanitize_for_log(item, _depth=_depth + 1)
        if len(items) > _PREVIEW_MAX_DICT_ITEMS:
            sanitized_dict["__truncated_items__"] = len(items) - _PREVIEW_MAX_DICT_ITEMS
        return sanitized_dict
    if hasattr(value, "model_dump"):
        try:
            return _sanitize_for_log(value.model_dump(mode="json"), _depth=_depth + 1)
        except Exception:
            return _preview_scalar(value)
    if hasattr(value, "__dict__"):
        return _preview_scalar(value)
    return _preview_scalar(value)


def _preview_scalar(value: Any) -> str:
    return _truncate(repr(value))


def _truncate(value: str) -> str:
    if len(value) <= _PREVIEW_MAX_STRING_LENGTH:
        return value
    return f"{value[:_PREVIEW_MAX_STRING_LENGTH]}... <truncated {len(value) - _PREVIEW_MAX_STRING_LENGTH} chars>"


def _safe_filename(value: str) -> str:
    safe = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            safe.append(char)
        else:
            safe.append("_")
    return "".join(safe) or "unknown-agent-instance"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

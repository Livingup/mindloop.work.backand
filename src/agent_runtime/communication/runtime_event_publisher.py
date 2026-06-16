# -*- coding: utf-8 -*-
"""运行时通信上下文与事件发布器。"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from agent_runtime.communication.north_gateway.communication_contracts import (
    CommunicationGateway,
    PublishCommunicationMessageRequest,
)

logger = logging.getLogger(__name__)
_RUNTIME_EVENT_PUBLISH_TIMEOUT_SECONDS = 5.0
_RUNTIME_EVENT_PUBLISH_SLOW_LOG_SECONDS = 1.0


def _duration_ms_since(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _current_task_name() -> str | None:
    task = asyncio.current_task()
    return task.get_name() if task is not None else None


def _log_runtime_publish_diagnostic(
    level: int,
    message: str,
    *,
    trace_id: str | None,
    stream_id: str,
    event_type: str,
    phase: str,
    task_name: str | None,
    duration_ms: int | None = None,
    timeout_seconds: float | None = None,
    timeout_overrun_ms: int | None = None,
    error_type: str | None = None,
    error: str | None = None,
) -> None:
    if not logger.isEnabledFor(level):
        return
    logger.log(
        level,
        "%s: trace_id=%s stream_id=%s event_type=%s service_id=%s operation=%s phase=%s task_name=%s "
        "duration_ms=%s timeout_seconds=%s timeout_overrun_ms=%s error_type=%s error=%s",
        message,
        trace_id,
        stream_id,
        event_type,
        None,
        "communication.runtime_event.publish",
        phase,
        task_name,
        duration_ms,
        timeout_seconds,
        timeout_overrun_ms,
        error_type,
        error,
    )


@dataclass(frozen=True, slots=True)
class RuntimeCommunicationContext:
    """运行过程中的通信关联上下文。

    该上下文只携带逻辑 stream_id 和运行追踪元数据，不携带具体 SSE/WebSocket 连接对象。
    Agent 实例、任务实例、LLMClient、工具调度器等运行对象可安全透传该上下文。
    """

    # 逻辑通信流 ID。所有实时事件通过该 ID 路由到订阅端。
    stream_id: str

    # 单次运行标识，用于把同一任务的多次唤醒或重试区分开。
    run_id: str = field(default_factory=lambda: f"run-{uuid4().hex}")

    # 当前发起事件的运行主体类型，例如 agent_instance、llm_client、tool_dispatcher。
    actor_type: str = "runtime"

    # 当前发起事件的运行主体 ID。
    actor_id: str | None = None

    # 可选链路追踪 ID。
    trace_id: str | None = None

    # 可选业务关联元数据，例如 task_id、agent_instance_id、conversation_id。
    metadata: dict[str, Any] = field(default_factory=dict)

    def with_actor(self, *, actor_type: str, actor_id: str | None = None) -> "RuntimeCommunicationContext":
        """派生一个仅替换当前事件主体的通信上下文。"""
        return RuntimeCommunicationContext(
            stream_id=self.stream_id,
            run_id=self.run_id,
            actor_type=actor_type,
            actor_id=actor_id,
            trace_id=self.trace_id,
            metadata=dict(self.metadata),
        )

    def with_metadata(self, **metadata: Any) -> "RuntimeCommunicationContext":
        """派生一个追加业务元数据的通信上下文。"""
        merged = dict(self.metadata)
        merged.update({key: value for key, value in metadata.items() if value is not None})
        return RuntimeCommunicationContext(
            stream_id=self.stream_id,
            run_id=self.run_id,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            trace_id=self.trace_id,
            metadata=merged,
        )


class RuntimeEventPublisher(Protocol):
    """运行时事件发布器协议。"""

    async def publish(
        self,
        context: RuntimeCommunicationContext | None,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        source: str | None = None,
        correlation_id: str | None = None,
        parent_message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """发布运行时事件。"""
        ...


class CommunicationRuntimeEventPublisher:
    """基于通用通信网关的运行时事件发布器。"""

    def __init__(self, communication_gateway: CommunicationGateway) -> None:
        # 通用通信北向网关。发布器不关心底层是 SSE、WebSocket 还是 CLI adapter。
        self._communication_gateway = communication_gateway

    async def publish(
        self,
        context: RuntimeCommunicationContext | None,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        source: str | None = None,
        correlation_id: str | None = None,
        parent_message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """发布运行时事件，失败时只记录日志，不中断 Agent 主执行流程。"""
        if context is None:
            return
        merged_metadata = dict(context.metadata)
        merged_metadata["run_id"] = context.run_id
        merged_metadata["actor_type"] = context.actor_type
        if context.trace_id is not None:
            merged_metadata["trace_id"] = context.trace_id
        if metadata:
            merged_metadata.update(metadata)
        started_at = time.perf_counter()
        task_name = _current_task_name()
        trace_id = str(context.trace_id or merged_metadata.get("trace_id") or "") or None
        _log_runtime_publish_diagnostic(
            logging.DEBUG,
            "Runtime communication event publish started",
            trace_id=trace_id,
            stream_id=context.stream_id,
            event_type=event_type,
            phase="publisher_wait_for.started",
            task_name=task_name,
            duration_ms=0,
            timeout_seconds=_RUNTIME_EVENT_PUBLISH_TIMEOUT_SECONDS,
        )
        try:
            await asyncio.wait_for(
                self._communication_gateway.publish_message(
                    PublishCommunicationMessageRequest(
                        stream_id=context.stream_id,
                        message_type=event_type,
                        payload=dict(payload or {}),
                        source=source or context.actor_type,
                        actor_id=context.actor_id,
                        correlation_id=correlation_id,
                        parent_message_id=parent_message_id,
                        metadata=merged_metadata,
                    )
                ),
                timeout=_RUNTIME_EVENT_PUBLISH_TIMEOUT_SECONDS,
            )
            duration_ms = _duration_ms_since(started_at)
            level = (
                logging.WARNING
                if duration_ms >= int(_RUNTIME_EVENT_PUBLISH_SLOW_LOG_SECONDS * 1000)
                else logging.DEBUG
            )
            _log_runtime_publish_diagnostic(
                level,
                "Runtime communication event publish completed",
                trace_id=trace_id,
                stream_id=context.stream_id,
                event_type=event_type,
                phase="publisher_wait_for.completed",
                task_name=task_name,
                duration_ms=duration_ms,
                timeout_seconds=_RUNTIME_EVENT_PUBLISH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            duration_ms = _duration_ms_since(started_at)
            timeout_ms = int(_RUNTIME_EVENT_PUBLISH_TIMEOUT_SECONDS * 1000)
            _log_runtime_publish_diagnostic(
                logging.ERROR,
                "Runtime communication event publish timed out",
                trace_id=trace_id,
                stream_id=context.stream_id,
                event_type=event_type,
                phase="publisher_wait_for.timeout",
                task_name=task_name,
                duration_ms=duration_ms,
                timeout_seconds=_RUNTIME_EVENT_PUBLISH_TIMEOUT_SECONDS,
                timeout_overrun_ms=max(0, duration_ms - timeout_ms),
                error_type="TimeoutError",
                error=f"wait_for exceeded {timeout_ms}ms",
            )
        except asyncio.CancelledError:
            _log_runtime_publish_diagnostic(
                logging.WARNING,
                "Runtime communication event publish cancelled",
                trace_id=trace_id,
                stream_id=context.stream_id,
                event_type=event_type,
                phase="publisher_wait_for.cancelled",
                task_name=task_name,
                duration_ms=_duration_ms_since(started_at),
                timeout_seconds=_RUNTIME_EVENT_PUBLISH_TIMEOUT_SECONDS,
                error_type="CancelledError",
            )
            raise
        except Exception as exc:  # noqa: BLE001 - 实时通知失败不能反向拖垮 Agent 执行
            logger.exception(
                "Runtime communication event publish failed: trace_id=%s stream_id=%s event_type=%s service_id=%s "
                "operation=%s phase=%s task_name=%s duration_ms=%s timeout_seconds=%s error_type=%s error=%s",
                trace_id,
                context.stream_id,
                event_type,
                None,
                "communication.runtime_event.publish",
                "publisher_wait_for.exception",
                task_name,
                _duration_ms_since(started_at),
                _RUNTIME_EVENT_PUBLISH_TIMEOUT_SECONDS,
                exc.__class__.__name__,
                exc,
            )


class NoopRuntimeEventPublisher:
    """空运行时事件发布器，用于未启用通信流的执行场景。"""

    async def publish(
        self,
        context: RuntimeCommunicationContext | None,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        source: str | None = None,
        correlation_id: str | None = None,
        parent_message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """忽略运行时事件。"""
        return None

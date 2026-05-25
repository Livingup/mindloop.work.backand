from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any
from uuid import uuid4

from agent_runtime.communication.runtime_event_publisher import RuntimeCommunicationContext, RuntimeEventPublisher
from agent_runtime.lacp.client import LacpClient
from agent_runtime.lacp.models import LacpExecutionObservation
from agent_runtime.lacp.registration_registry import LacpRegistrationRegistry

logger = logging.getLogger(__name__)
_COMPOSITE_PUBLISHER_TIMEOUT_SECONDS = 5.0

LACP_OBSERVATION_EVENT_TYPES = frozenset({
    "agent.task.started",
    "agent.task.paused",
    "agent.task.completed",
    "agent.task.failed",
    "agent.assistant.message.received",
    "tool.call.started",
    "tool.call.completed",
    "tool.call.failed",
    "tool.call.cancelled",
    "context_compression.started",
    "context_compression.completed",
    "context_compression.failed",
    "context_compression.cancelled",
    "context_compression.skipped",
    "context_compression.worker.started",
    "context_compression.worker.completed",
    "context_compression.worker.failed",
})


class CompositeRuntimeEventPublisher:
    def __init__(self, *publishers: RuntimeEventPublisher) -> None:
        self._publishers = tuple(publishers)

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
        for publisher in self._publishers:
            try:
                await asyncio.wait_for(
                    publisher.publish(
                        context,
                        event_type,
                        payload,
                        source=source,
                        correlation_id=correlation_id,
                        parent_message_id=parent_message_id,
                        metadata=metadata,
                    ),
                    timeout=_COMPOSITE_PUBLISHER_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "Runtime event publisher timed out: event_type=%s publisher=%s timeout_seconds=%s",
                    event_type,
                    publisher.__class__.__name__,
                    _COMPOSITE_PUBLISHER_TIMEOUT_SECONDS,
                )
            except Exception:
                logger.exception("Runtime event publisher failed: event_type=%s publisher=%s", event_type, publisher.__class__.__name__)


class LacpFilteringRuntimeEventPublisher:
    def __init__(
        self,
        *,
        lacp_client: LacpClient,
        registration_registry: LacpRegistrationRegistry,
        allowed_event_types: frozenset[str] = LACP_OBSERVATION_EVENT_TYPES,
    ) -> None:
        self._lacp_client = lacp_client
        self._registration_registry = registration_registry
        self._allowed_event_types = allowed_event_types
        self._sequences: defaultdict[str, int] = defaultdict(int)

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
        if context is None:
            return
        if event_type not in self._allowed_event_types:
            return
        payload = dict(payload or {})
        merged_metadata = dict(context.metadata)
        if metadata:
            merged_metadata.update(metadata)
        instance_id = _extract_instance_id(context, payload, merged_metadata)
        if not instance_id:
            return
        if not self._registration_registry.is_registered(instance_id):
            return
        execution_id = str(merged_metadata.get("task_id") or context.run_id)
        self._sequences[execution_id] += 1
        observation = LacpExecutionObservation(
            instance_id=instance_id,
            execution_id=execution_id,
            event_id=f"lacp-obs-{uuid4().hex}",
            sequence=self._sequences[execution_id],
            state=_state_for_event(event_type),
            event=_observation_event(event_type, payload, source=source, correlation_id=correlation_id),
            caller_instance_id=_optional_text(merged_metadata.get("caller_instance_id")),
        )
        await self._lacp_client.report_execution_observation(observation)


def _extract_instance_id(
    context: RuntimeCommunicationContext,
    payload: dict[str, Any],
    metadata: dict[str, Any],
) -> str | None:
    for value in (
        metadata.get("agent_instance_id"),
        payload.get("agent_instance_id"),
        payload.get("instance_id"),
        context.actor_id if context.actor_type == "agent_instance" else None,
    ):
        text = _optional_text(value)
        if text:
            return text
    return None


def _state_for_event(event_type: str) -> str:
    if event_type.endswith(".completed") or event_type == "agent.task.completed":
        return "completed_waiting_caller"
    if event_type.endswith(".paused") or event_type == "agent.task.paused":
        return "waiting_for_message"
    if event_type.endswith(".failed"):
        return "waiting_for_message"
    return "executing"


def _observation_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    source: str | None,
    correlation_id: str | None,
) -> dict[str, Any]:
    if event_type.startswith("tool.call."):
        status = event_type.rsplit(".", 1)[-1]
        tool_name = _optional_text(payload.get("tool_name")) or _optional_text(payload.get("name")) or "tool"
        return {
            "kind": "tool_call",
            "summary": f"Tool {tool_name} {status}",
            "tool": {
                "call_id": _optional_text(payload.get("tool_call_id")) or correlation_id,
                "name": tool_name,
                "status": status,
                "error_message": _optional_text(payload.get("error_message")),
            },
            "metadata": _metadata(event_type, payload, source),
        }
    if event_type.startswith("agent.task."):
        status = event_type.rsplit(".", 1)[-1]
        return {
            "kind": "state_changed",
            "summary": f"Agent task {status}",
            "detail": _optional_text(payload.get("failure_message")) or _optional_text(payload.get("reason")),
            "metadata": _metadata(event_type, payload, source),
        }
    return {
        "kind": "work_log",
        "summary": _summary_for_work_log(event_type, payload),
        "metadata": _metadata(event_type, payload, source),
    }


def _summary_for_work_log(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "agent.assistant.message.received":
        return "Assistant message received"
    if event_type.startswith("context_compression."):
        return event_type.replace("_", " ")
    return event_type


def _metadata(event_type: str, payload: dict[str, Any], source: str | None) -> dict[str, Any]:
    return {
        "source_event_type": event_type,
        "source": source,
        "payload": payload,
    }


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None

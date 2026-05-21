from __future__ import annotations

from typing import Any

from agent_runtime.communication.runtime_event_publisher import (
    RuntimeCommunicationContext,
    RuntimeEventPublisher,
)

CONTEXT_COMPRESSION_PAYLOAD_VERSION = "1.0"
CONTEXT_COMPRESSION_EVENT_PREFIX = "context_compression."
CONTEXT_COMPRESSION_STARTED = "context_compression.started"
CONTEXT_COMPRESSION_RANGE_PLANNED = "context_compression.range_planned"
CONTEXT_COMPRESSION_WORKER_QUEUED = "context_compression.worker.queued"
CONTEXT_COMPRESSION_WORKER_STARTED = "context_compression.worker.started"
CONTEXT_COMPRESSION_WORKER_PROGRESS = "context_compression.worker.progress"
CONTEXT_COMPRESSION_WORKER_COMPLETED = "context_compression.worker.completed"
CONTEXT_COMPRESSION_WORKER_FAILED = "context_compression.worker.failed"
CONTEXT_COMPRESSION_WORKER_CANCELLED = "context_compression.worker.cancelled"
CONTEXT_COMPRESSION_SECTION_STARTED = "context_compression.section.started"
CONTEXT_COMPRESSION_SECTION_COMPLETED = "context_compression.section.completed"
CONTEXT_COMPRESSION_ASSEMBLY_STARTED = "context_compression.assembly.started"
CONTEXT_COMPRESSION_ASSEMBLY_COMPLETED = "context_compression.assembly.completed"
CONTEXT_COMPRESSION_VERIFICATION_STARTED = "context_compression.verification.started"
CONTEXT_COMPRESSION_VERIFICATION_COMPLETED = "context_compression.verification.completed"
CONTEXT_COMPRESSION_CONVERSATION_REBIND_COMPLETED = "context_compression.conversation.rebind.completed"
CONTEXT_COMPRESSION_COMPLETED = "context_compression.completed"
CONTEXT_COMPRESSION_FAILED = "context_compression.failed"
CONTEXT_COMPRESSION_CANCELLED = "context_compression.cancelled"
CONTEXT_COMPRESSION_SKIPPED = "context_compression.skipped"

CONTEXT_COMPRESSION_EVENT_TYPES = frozenset({
    CONTEXT_COMPRESSION_STARTED,
    CONTEXT_COMPRESSION_RANGE_PLANNED,
    CONTEXT_COMPRESSION_WORKER_QUEUED,
    CONTEXT_COMPRESSION_WORKER_STARTED,
    CONTEXT_COMPRESSION_WORKER_PROGRESS,
    CONTEXT_COMPRESSION_WORKER_COMPLETED,
    CONTEXT_COMPRESSION_WORKER_FAILED,
    CONTEXT_COMPRESSION_WORKER_CANCELLED,
    CONTEXT_COMPRESSION_SECTION_STARTED,
    CONTEXT_COMPRESSION_SECTION_COMPLETED,
    CONTEXT_COMPRESSION_ASSEMBLY_STARTED,
    CONTEXT_COMPRESSION_ASSEMBLY_COMPLETED,
    CONTEXT_COMPRESSION_VERIFICATION_STARTED,
    CONTEXT_COMPRESSION_VERIFICATION_COMPLETED,
    CONTEXT_COMPRESSION_CONVERSATION_REBIND_COMPLETED,
    CONTEXT_COMPRESSION_COMPLETED,
    CONTEXT_COMPRESSION_FAILED,
    CONTEXT_COMPRESSION_CANCELLED,
    CONTEXT_COMPRESSION_SKIPPED,
})


class ContextCompressionEventPublisher:
    def __init__(self, event_publisher: RuntimeEventPublisher) -> None:
        self._event_publisher = event_publisher

    async def publish(
        self,
        context: RuntimeCommunicationContext | None,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        source: str = "context_compression_service",
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if event_type not in CONTEXT_COMPRESSION_EVENT_TYPES:
            raise ValueError(f"不支持的上下文压缩事件类型: {event_type}")
        actual_payload = dict(payload or {})
        actual_payload.setdefault("payload_version", CONTEXT_COMPRESSION_PAYLOAD_VERSION)
        job_id = actual_payload.get("job_id")
        await self._event_publisher.publish(
            context,
            event_type,
            actual_payload,
            source=source,
            correlation_id=correlation_id or (job_id if isinstance(job_id, str) and job_id else None),
            metadata=_event_metadata(actual_payload, metadata),
        )


def _event_metadata(payload: dict[str, Any], metadata: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "job_id",
        "task_id",
        "agent_instance_id",
        "root_conversation_id",
        "source_conversation_id",
        "target_conversation_id",
        "compression_index",
    ):
        value = payload.get(key)
        if value is not None:
            result[key] = value
    if metadata:
        result.update(metadata)
    return result

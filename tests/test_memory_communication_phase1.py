# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.communication.communication_errors import CommunicationGapUnrecoverableError
from agent_runtime.communication.communication_service import CommunicationService
from agent_runtime.communication.event_hub import InMemoryCommunicationEventHub
from agent_runtime.communication.message_owner_policy import (
    CommunicationMessageOwnerPolicy,
    classify_message_owner_policy,
    require_message_agent_instance_id,
)
from agent_runtime.communication.north_gateway.communication_gateway_impl import CommunicationGatewayImpl
from agent_runtime.communication.north_gateway.communication_rest_adapter import (
    _format_sse_ping,
    create_communication_router,
)
from agent_runtime.communication.runtime_event_publisher import (
    CommunicationRuntimeEventPublisher,
    RuntimeCommunicationContext,
)
from agent_runtime.frontend_debug.frontend_debug_service import FrontendDebugService
from agent_runtime.frontend_debug.north_gateway.frontend_debug_contracts import CreateFrontendDebugSessionRequest


OWNER_METADATA = {"agent_instance_id": "agent-instance-test"}


class _UnusedRepository:
    def create_stream(self, stream):  # noqa: ANN001
        raise AssertionError("memory mode must not create stream in sqlite repository")

    def save_stream(self, stream):  # noqa: ANN001
        raise AssertionError("memory mode must not save stream in sqlite repository")

    def get_stream(self, stream_id: str):
        raise AssertionError("memory mode must not get stream from sqlite repository")

    def append_message(self, message):  # noqa: ANN001
        raise AssertionError("memory mode must not append message to sqlite repository")

    def list_messages(self, stream_id: str, *, after_seq: int = 0, limit: int = 500):
        raise AssertionError("memory mode must not list messages from sqlite repository")

    def list_latest_messages(self, stream_id: str, *, limit: int = 500):
        raise AssertionError("memory mode must not list latest from sqlite repository")


def _service(**hub_kwargs) -> CommunicationService:
    hub = InMemoryCommunicationEventHub(enable_idle_reclaimer=False, **hub_kwargs)
    return CommunicationService(_UnusedRepository(), hub, storage_mode="memory")


@pytest.mark.asyncio
async def test_memory_publish_allocates_seq_without_sqlite() -> None:
    service = _service(memory_ring_max_events=100)
    service.create_stream(topic="agent.task", stream_id="stream-a")
    first = await service.publish_message(
        stream_id="stream-a",
        message_type="agent.lifecycle.message",
        payload={"n": 1},
        metadata=OWNER_METADATA,
    )
    second = await service.publish_message(
        stream_id="stream-a",
        message_type="terminal.output.delta",
        payload={"chunk": "x"},
        metadata=OWNER_METADATA,
    )
    assert first.seq == 1
    assert second.seq == 2
    listed = service.list_messages("stream-a", after_seq=0, limit=10)
    assert [item.seq for item in listed] == [1, 2]


@pytest.mark.asyncio
async def test_memory_ring_prefers_evicting_terminal_over_main() -> None:
    service = _service(
        memory_ring_max_events=5,
        memory_ring_main_min_events=2,
        memory_ring_terminal_max_events=3,
        memory_ring_max_bytes=10 * 1024 * 1024,
        global_memory_ring_max_bytes=64 * 1024 * 1024,
    )
    service.create_stream(topic="agent.task", stream_id="stream-b")
    await service.publish_message(
        stream_id="stream-b",
        message_type="agent.task.started",
        payload={},
        metadata=OWNER_METADATA,
    )
    await service.publish_message(
        stream_id="stream-b",
        message_type="llm.request.started",
        payload={},
        metadata=OWNER_METADATA,
    )
    for index in range(5):
        await service.publish_message(
            stream_id="stream-b",
            message_type="terminal.output.delta",
            payload={"chunk": f"line-{index}"},
            metadata=OWNER_METADATA,
        )
    messages = service.list_latest_messages("stream-b", limit=20)
    types = [message.type for message in messages]
    assert "agent.task.started" in types
    assert "llm.request.started" in types
    assert types.count("terminal.output.delta") <= 3
    assert len(messages) <= 5


def test_list_messages_raises_gap_when_after_seq_too_old() -> None:
    async def run() -> None:
        service = _service(
            memory_ring_max_events=3,
            memory_ring_main_min_events=0,
            memory_ring_terminal_max_events=3,
            memory_ring_max_bytes=10 * 1024 * 1024,
            global_memory_ring_max_bytes=64 * 1024 * 1024,
        )
        service.create_stream(topic="agent.task", stream_id="stream-c")
        for index in range(5):
            await service.publish_message(
                stream_id="stream-c",
                message_type="terminal.output.delta",
                payload={"chunk": str(index)},
                metadata=OWNER_METADATA,
            )
        with pytest.raises(CommunicationGapUnrecoverableError) as exc_info:
            service.list_messages("stream-c", after_seq=1, limit=10)
        assert exc_info.value.to_dict()["error_code"] == "communication_gap_unrecoverable"
        assert exc_info.value.ring_first_seq >= 3

    asyncio.run(run())


def test_rest_list_messages_returns_409_and_boot_header() -> None:
    async def seed(service: CommunicationService) -> None:
        service.create_stream(topic="agent.task", stream_id="stream-d")
        for index in range(5):
            await service.publish_message(
                stream_id="stream-d",
                message_type="terminal.output.delta",
                payload={"chunk": str(index)},
                metadata=OWNER_METADATA,
            )

    service = _service(
        memory_ring_max_events=3,
        memory_ring_main_min_events=0,
        memory_ring_terminal_max_events=3,
        memory_ring_max_bytes=10 * 1024 * 1024,
        global_memory_ring_max_bytes=64 * 1024 * 1024,
    )
    asyncio.run(seed(service))
    app = FastAPI()
    app.include_router(create_communication_router(CommunicationGatewayImpl(service)), prefix="/api/communication")
    client = TestClient(app)
    latest = client.get("/api/communication/streams/stream-d/messages", params={"latest": True, "limit": 1})
    assert latest.status_code == 200
    assert latest.headers.get("X-Runtime-Boot-Id") == service.runtime_boot_id
    gap = client.get("/api/communication/streams/stream-d/messages", params={"after_seq": 1, "limit": 10})
    assert gap.status_code == 409
    detail = gap.json()["detail"]
    assert detail["error_code"] == "communication_gap_unrecoverable"


def test_sse_ping_includes_runtime_boot_id() -> None:
    frame = _format_sse_ping(
        stream_id="stream-e",
        sent_count=1,
        keepalive_count=1,
        runtime_boot_id="boot-xyz",
    )
    assert "runtime_boot_id" in frame
    assert "boot-xyz" in frame
    assert "\nid: " not in frame


@pytest.mark.parametrize(
    "event_type",
    (
        "user.message.accepted",
        "agent.task.completed",
        "agent.subagent.released",
        "llm.response.completed",
        "tool.call.completed",
        "context_compression.completed",
        "terminal.output.delta",
        "frontend.debug.element.selected",
    ),
)
def test_agent_event_families_require_explicit_metadata_owner(event_type: str) -> None:
    assert classify_message_owner_policy(event_type) is CommunicationMessageOwnerPolicy.AGENT_INSTANCE_REQUIRED
    with pytest.raises(ValueError, match="metadata.agent_instance_id"):
        require_message_agent_instance_id(event_type, None)
    with pytest.raises(ValueError, match="metadata.agent_instance_id"):
        require_message_agent_instance_id(event_type, {"agent_instance_id": "   "})
    require_message_agent_instance_id(event_type, OWNER_METADATA)


@pytest.mark.parametrize(
    "event_type",
    (
        "communication.ping",
        "system.network.status",
        "network.snapshot",
        "file_resource.upload.progress",
        "custom.audit.event",
        "vendor.extension.event",
    ),
)
def test_global_transport_resource_and_custom_events_may_omit_agent_owner(event_type: str) -> None:
    assert classify_message_owner_policy(event_type) is CommunicationMessageOwnerPolicy.UNOWNED_ALLOWED
    require_message_agent_instance_id(event_type, None)


@pytest.mark.asyncio
async def test_memory_owner_gate_rejects_before_ring_append() -> None:
    service = _service(memory_ring_max_events=100)
    service.create_stream(topic="agent.scene", stream_id="stream-owner-memory")
    with pytest.raises(ValueError, match="metadata.agent_instance_id"):
        await service.publish_message(
            stream_id="stream-owner-memory",
            message_type="agent.lifecycle.message",
            payload={"content": "ownerless"},
        )
    assert service.list_messages("stream-owner-memory", after_seq=0, limit=10) == []
    accepted = await service.publish_message(
        stream_id="stream-owner-memory",
        message_type="agent.lifecycle.message",
        payload={"content": "owned"},
        metadata=OWNER_METADATA,
    )
    assert accepted.seq == 1


@pytest.mark.asyncio
async def test_runtime_owner_violation_is_logged_and_not_written(caplog) -> None:
    service = _service(memory_ring_max_events=100)
    service.create_stream(topic="agent.scene", stream_id="stream-owner-runtime")
    publisher = CommunicationRuntimeEventPublisher(CommunicationGatewayImpl(service))
    context = RuntimeCommunicationContext(
        stream_id="stream-owner-runtime",
        actor_type="runtime",
        actor_id=None,
        metadata={},
    )
    with caplog.at_level(logging.ERROR):
        await publisher.publish(context, "llm.request.started", {"request_id": "request-ownerless"})
    assert service.list_messages("stream-owner-runtime", after_seq=0, limit=10) == []
    assert any(
        "Runtime communication event publish failed" in record.getMessage()
        and "metadata.agent_instance_id" in record.getMessage()
        for record in caplog.records
    )


def test_rest_agent_event_without_owner_returns_diagnostic_409_but_custom_event_is_allowed() -> None:
    service = _service(memory_ring_max_events=100)
    service.create_stream(topic="agent.scene", stream_id="stream-owner-rest")
    app = FastAPI()
    app.include_router(create_communication_router(CommunicationGatewayImpl(service)), prefix="/api/communication")
    client = TestClient(app)
    rejected = client.post(
        "/api/communication/streams/stream-owner-rest/messages",
        json={"message_type": "frontend.debug.element.selected", "payload": {}},
    )
    assert rejected.status_code == 409
    assert "metadata.agent_instance_id" in rejected.json()["detail"]
    assert service.list_messages("stream-owner-rest", after_seq=0, limit=10) == []
    allowed = client.post(
        "/api/communication/streams/stream-owner-rest/messages",
        json={"message_type": "custom.audit.event", "payload": {"ok": True}},
    )
    assert allowed.status_code == 201
    assert allowed.json()["seq"] == 1


class _FrontendDebugRepository:
    def __init__(self) -> None:
        self.saved = []

    def save(self, session):  # noqa: ANN001
        self.saved.append(session)
        return session

    def get(self, session_id: str):
        return next((item for item in self.saved if item.session_id == session_id), None)

    def list_active(self):
        return list(self.saved)


class _FrontendDebugProxy:
    def __init__(self) -> None:
        self.start_calls = 0

    def start_session_proxy(self, **kwargs) -> str:  # noqa: ANN003
        self.start_calls += 1
        return "http://127.0.0.1:12345"


class _FrontendDebugPublisher:
    def publish_element_selected(self, **kwargs) -> None:  # noqa: ANN003
        return None


def test_frontend_debug_rejects_ownerless_session_before_starting_proxy() -> None:
    repository = _FrontendDebugRepository()
    proxy = _FrontendDebugProxy()
    service = FrontendDebugService(
        repository,  # type: ignore[arg-type]
        proxy,  # type: ignore[arg-type]
        _FrontendDebugPublisher(),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="agent_instance_id"):
        service.create_session(
            CreateFrontendDebugSessionRequest(
                target_url="http://localhost:5173/chat",
                agent_instance_id="   ",
                communication_stream_id="stream-scene-1",
            )
        )
    assert proxy.start_calls == 0
    assert repository.saved == []


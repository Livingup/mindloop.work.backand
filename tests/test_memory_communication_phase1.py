# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.communication.communication_errors import CommunicationGapUnrecoverableError
from agent_runtime.communication.communication_service import CommunicationService
from agent_runtime.communication.event_hub import InMemoryCommunicationEventHub
from agent_runtime.communication.north_gateway.communication_gateway_impl import CommunicationGatewayImpl
from agent_runtime.communication.north_gateway.communication_rest_adapter import (
    _format_sse_ping,
    create_communication_router,
)


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
    first = await service.publish_message(stream_id="stream-a", message_type="agent.lifecycle.message", payload={"n": 1})
    second = await service.publish_message(stream_id="stream-a", message_type="terminal.output.delta", payload={"chunk": "x"})
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
    await service.publish_message(stream_id="stream-b", message_type="agent.task.started", payload={})
    await service.publish_message(stream_id="stream-b", message_type="llm.request.started", payload={})
    for index in range(5):
        await service.publish_message(
            stream_id="stream-b",
            message_type="terminal.output.delta",
            payload={"chunk": f"line-{index}"},
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


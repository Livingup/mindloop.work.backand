import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from agent_runtime.communication.communication_message import CommunicationMessage
from agent_runtime.communication.communication_service import CommunicationService
from agent_runtime.communication.communication_stream import CommunicationStream
from agent_runtime.communication.event_hub import InMemoryCommunicationEventHub
from agent_runtime.communication.infrastructure.sqlite_communication_repository import SqliteCommunicationRepository
from agent_runtime.communication.north_gateway import communication_rest_adapter as adapter
from agent_runtime.communication.north_gateway.communication_contracts import (
    CreateCommunicationStreamRequest,
    PublishCommunicationMessageRequest,
)
from agent_runtime.communication.north_gateway.communication_rest_adapter import (
    COMMUNICATION_SSE_KEEPALIVE_SECONDS,
    _effective_after_seq,
    _format_sse_message,
    _format_sse_ping,
    _last_event_id_to_seq,
    create_communication_router,
)


def _message(seq: int, *, message_id: str | None = None, stream_id: str = "stream-1") -> CommunicationMessage:
    return CommunicationMessage(
        message_id=message_id or f"message-{seq}",
        stream_id=stream_id,
        seq=seq,
        type="unit.event",
        topic="agent.task",
        source="test",
        actor_id="actor-1",
        correlation_id="corr-1",
        parent_message_id=None,
        payload={"seq": seq},
        metadata={"trace_id": "trace-1"},
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )



class _Subscription:
    def __init__(self) -> None:
        self.closed = False
        self.next_started = asyncio.Event()

    def __aiter__(self) -> "_Subscription":
        return self

    async def __anext__(self) -> CommunicationMessage:
        self.next_started.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True



class _Gateway:
    def __init__(self, subscription: _Subscription) -> None:
        self.subscription = subscription
        self.subscribe_after_seq: int | None = None

    def create_stream(self, request: CreateCommunicationStreamRequest) -> CommunicationStream:
        raise NotImplementedError

    def get_stream(self, stream_id: str) -> CommunicationStream:
        return CommunicationStream(stream_id=stream_id, topic="agent.task")

    def close_stream(self, stream_id: str) -> CommunicationStream:
        raise NotImplementedError

    async def publish_message(self, request: PublishCommunicationMessageRequest) -> CommunicationMessage:
        raise NotImplementedError

    def list_messages(self, stream_id: str, *, after_seq: int = 0, limit: int = 500) -> list[CommunicationMessage]:
        return []

    def list_latest_messages(self, stream_id: str, *, limit: int = 500) -> list[CommunicationMessage]:
        return []

    def get_stream_diagnostics(self, stream_id: str) -> dict[str, Any]:
        return {}

    def subscribe_messages(self, stream_id: str, *, after_seq: int = 0) -> _Subscription:
        self.subscribe_after_seq = after_seq
        return self.subscription



class _Request:
    def __init__(self, *, last_event_id: str | None = None) -> None:
        self.headers = {"last-event-id": last_event_id} if last_event_id is not None else {}
        self.query_params: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return False



class _SequenceRepository:
    def __init__(self, replay_batches: list[list[CommunicationMessage]], gap_batches: list[list[CommunicationMessage]]) -> None:
        self.replay_batches = list(replay_batches)
        self.gap_batches = list(gap_batches)
        self.calls: list[tuple[int, int]] = []

    def create_stream(self, stream: CommunicationStream) -> CommunicationStream:
        raise NotImplementedError

    def save_stream(self, stream: CommunicationStream) -> CommunicationStream:
        raise NotImplementedError

    def get_stream(self, stream_id: str) -> CommunicationStream | None:
        return CommunicationStream(stream_id=stream_id, topic="agent.task")

    def append_message(self, message: CommunicationMessage) -> CommunicationMessage:
        raise NotImplementedError

    def advance_stream_cursor(self, stream_id: str, *, seq: int) -> None:
        return None

    def list_messages(self, stream_id: str, *, after_seq: int = 0, limit: int = 500) -> list[CommunicationMessage]:
        self.calls.append((after_seq, limit))
        if self.replay_batches:
            return self.replay_batches.pop(0)
        if self.gap_batches:
            return self.gap_batches.pop(0)
        return []

    def list_latest_messages(self, stream_id: str, *, limit: int = 500) -> list[CommunicationMessage]:
        return []



class _EventHub:
    def __init__(self, live_messages: list[CommunicationMessage]) -> None:
        self.live_messages = list(live_messages)
        self.closed = False

    async def subscribe(self, stream_id: str, *, after_seq: int = 0):
        try:
            for message in self.live_messages:
                yield message
            await asyncio.Event().wait()
        finally:
            self.closed = True

    async def publish(self, message: CommunicationMessage) -> CommunicationMessage:
        return message

    def get_stream_diagnostics(self, stream_id: str) -> dict[str, int | None]:
        return {}



def test_sse_frame_format_and_resume_cursor_contracts() -> None:
    message = _message(7)

    frame = _format_sse_message(message)
    lines = frame.splitlines()
    assert lines[0] == "id: 7"
    assert lines[1] == "event: communication.message"
    assert frame.endswith("\n\n")
    payload = json.loads(lines[2].removeprefix("data: "))
    assert payload["message_id"] == "message-7"
    assert payload["stream_id"] == "stream-1"
    assert payload["seq"] == 7
    assert payload["type"] == "unit.event"

    ping = _format_sse_ping(stream_id="stream-1", sent_count=3, keepalive_count=2)
    assert ping.startswith("event: communication.ping\n")
    assert "\nid: " not in ping
    assert ping.endswith("\n\n")
    ping_payload = json.loads(ping.splitlines()[1].removeprefix("data: "))
    assert ping_payload["stream_id"] == "stream-1"
    assert ping_payload["sent_count"] == 3
    assert ping_payload["keepalive_count"] == 2
    assert "server_time" in ping_payload

    assert COMMUNICATION_SSE_KEEPALIVE_SECONDS == 15
    assert _last_event_id_to_seq(None) == 0
    assert _last_event_id_to_seq("bad") == 0
    assert _last_event_id_to_seq("-3") == 0
    assert _last_event_id_to_seq("9") == 9
    assert _effective_after_seq(4, "9") == 9
    assert _effective_after_seq(12, "9") == 12
    assert _effective_after_seq(-5, "bad") == 0


def test_adapter_keepalive_ping_and_disconnect_cleanup_do_not_advance_business_cursor(monkeypatch) -> None:
    async def run_case() -> None:
        subscription = _Subscription()
        gateway = _Gateway(subscription)
        router = create_communication_router(gateway)
        route = next(route for route in router.routes if getattr(route, "path", None) == "/streams/{stream_id}/events")

        async def immediate_timeout(tasks: set[asyncio.Task[CommunicationMessage]], *, timeout: float, return_when: Any):
            assert len(tasks) == 1
            assert timeout == COMMUNICATION_SSE_KEEPALIVE_SECONDS
            assert return_when == asyncio.FIRST_COMPLETED
            return set(), set(tasks)

        monkeypatch.setattr(adapter.asyncio, "wait", immediate_timeout)
        before_active_sse = adapter.runtime_network_monitor.snapshot()["active_sse_connections"]

        response = await route.endpoint("stream-1", _Request(last_event_id="8"), after_seq=3)
        body_iterator = response.body_iterator
        try:
            comment_frame = await anext(body_iterator)
            ping_frame = await anext(body_iterator)
        finally:
            await body_iterator.aclose()

        assert gateway.subscribe_after_seq == 8
        assert comment_frame == ": keep-alive\n\n"
        assert ping_frame.startswith("event: communication.ping\n")
        assert "\nid: " not in ping_frame
        ping_payload = json.loads(ping_frame.splitlines()[1].removeprefix("data: "))
        assert ping_payload["stream_id"] == "stream-1"
        assert ping_payload["sent_count"] == 0
        assert ping_payload["keepalive_count"] == 1
        assert subscription.closed is True
        assert adapter.runtime_network_monitor.snapshot()["active_sse_connections"] == before_active_sse

    asyncio.run(run_case())


def test_communication_service_replays_before_live_gap_fills_and_skips_duplicates() -> None:
    async def run_case() -> None:
        repository = _SequenceRepository(
            replay_batches=[[_message(1), _message(2)]],
            gap_batches=[[_message(3), _message(4), _message(5)]],
        )
        event_hub = _EventHub([_message(2, message_id="duplicate-live"), _message(5, message_id="live-gap-duplicate"), _message(6)])
        service = CommunicationService(repository, event_hub, storage_mode="sqlite")
        subscription = service.subscribe_messages("stream-1", after_seq=-10)
        yielded: list[CommunicationMessage] = []
        try:
            for _ in range(6):
                yielded.append(await anext(subscription))
        finally:
            await subscription.aclose()

        assert [message.seq for message in yielded] == [1, 2, 3, 4, 5, 6]
        assert yielded[4].message_id == "message-5"
        assert yielded[5].message_id == "message-6"
        assert repository.calls == [(0, 5000), (2, 5000)]
        assert event_hub.closed is True

    asyncio.run(run_case())


def test_communication_service_closes_live_subscription_when_consumer_disconnects() -> None:
    async def run_case() -> None:
        repository = _SequenceRepository(replay_batches=[[]], gap_batches=[])
        event_hub = _EventHub([_message(1)])
        service = CommunicationService(repository, event_hub, storage_mode="sqlite")
        subscription = service.subscribe_messages("stream-1", after_seq=0)

        first = await anext(subscription)
        assert first.seq == 1
        await subscription.aclose()
        assert event_hub.closed is True

    asyncio.run(run_case())


def test_sqlite_history_read_does_not_advance_producer_sequence(tmp_path) -> None:
    repository = SqliteCommunicationRepository(lambda: str(tmp_path))
    repository.create_stream(CommunicationStream(stream_id="stream-1", topic="agent.task"))
    first = repository.append_message(_message(0, message_id="message-first"))
    repository.list_messages("stream-1", after_seq=1000, limit=1)
    second = repository.append_message(_message(0, message_id="message-second"))
    try:
        assert first.seq == 1
        assert second.seq == 2
    finally:
        repository.close()


def test_communication_service_pages_live_gap_without_skipping_messages() -> None:
    async def run_case() -> None:
        repository = _SequenceRepository(
            replay_batches=[[]],
            gap_batches=[
                [_message(seq) for seq in range(1, 5001)],
                [_message(5001), _message(5002)],
            ],
        )
        event_hub = _EventHub([_message(5002, message_id="live-5002")])
        service = CommunicationService(repository, event_hub, storage_mode="sqlite")
        subscription = service.subscribe_messages("stream-1", after_seq=0)
        yielded: list[CommunicationMessage] = []
        try:
            for _ in range(5002):
                yielded.append(await anext(subscription))
        finally:
            await subscription.aclose()

        assert [message.seq for message in yielded] == list(range(1, 5003))
        assert repository.calls == [(0, 5000), (0, 5000), (5000, 5000)]
        assert event_hub.closed is True

    asyncio.run(run_case())


def test_communication_service_fails_when_live_gap_is_not_durable() -> None:
    async def run_case() -> None:
        repository = _SequenceRepository(
            replay_batches=[[]],
            gap_batches=[[_message(1)], []],
        )
        event_hub = _EventHub([_message(3, message_id="live-3")])
        service = CommunicationService(repository, event_hub, storage_mode="sqlite")
        subscription = service.subscribe_messages("stream-1", after_seq=0)
        try:
            first = await anext(subscription)
            assert first.seq == 1
            try:
                await anext(subscription)
            except RuntimeError as exc:
                assert "无法补齐实时事件缺口" in str(exc)
            else:
                raise AssertionError("missing durable gap must terminate the subscription")
        finally:
            await subscription.aclose()
        assert event_hub.closed is True

    asyncio.run(run_case())


def test_sqlite_owner_gate_rejects_before_durable_append(tmp_path) -> None:
    async def run_case() -> None:
        repository = SqliteCommunicationRepository(lambda: str(tmp_path))
        service = CommunicationService(
            repository,
            InMemoryCommunicationEventHub(enable_idle_reclaimer=False),
            storage_mode="sqlite",
        )
        service.create_stream(topic="agent.scene", stream_id="stream-owner-sqlite")
        try:
            with pytest.raises(ValueError, match="metadata.agent_instance_id"):
                await service.publish_message(
                    stream_id="stream-owner-sqlite",
                    message_type="terminal.output.delta",
                    payload={"chunk": "ownerless"},
                )
            assert service.list_messages("stream-owner-sqlite", after_seq=0, limit=10) == []
            accepted = await service.publish_message(
                stream_id="stream-owner-sqlite",
                message_type="terminal.output.delta",
                payload={"chunk": "owned"},
                metadata={"agent_instance_id": "agent-instance-sqlite"},
            )
            assert accepted.seq == 1
        finally:
            repository.close()

    asyncio.run(run_case())

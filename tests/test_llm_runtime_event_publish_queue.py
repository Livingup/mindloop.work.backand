import asyncio
import copy
import time
from typing import Any, Callable

from agent_runtime.infrastructure.runtime_task_registry import RuntimeTaskRegistry
from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_CHAT_COMPLETIONS
from agent_runtime.llm_client.llm_protocol_clients import _emit_stream_event, _read_sse_response_text
from agent_runtime.llm_client.llm_request_service import _build_stream_event_handler
from agent_runtime.llm_client.llm_runtime_event_publish_queue import (
    LlmRuntimeEventPublishAdmissionLimits,
    LlmRuntimeEventPublishQueue,
    _payload_size_bytes,
    llm_runtime_event_publish_admission_snapshot,
)


class _GatePublisher:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started: list[tuple[str, dict[str, Any]]] = []
        self.completed: list[tuple[str, dict[str, Any]]] = []
        self._started_condition = asyncio.Condition()

    async def __call__(self, event_type: str, payload: dict[str, Any]) -> None:
        async with self._started_condition:
            self.started.append((event_type, copy.deepcopy(payload)))
            self._started_condition.notify_all()
        await self.release.wait()
        self.completed.append((event_type, copy.deepcopy(payload)))

    async def wait_started(self, count: int) -> None:
        async with self._started_condition:
            await asyncio.wait_for(
                self._started_condition.wait_for(lambda: len(self.started) >= count),
                timeout=1,
            )


class _BlockingRuntimeEventPublisher:
    def __init__(self) -> None:
        self.gate = _GatePublisher()

    async def publish(
        self,
        context: Any,
        event_type: str,
        payload: dict[str, Any],
        *,
        source: str,
        metadata: dict[str, Any],
    ) -> None:
        assert source == "llm_client"
        assert metadata["conversation_id"] == "conversation-1"
        await self.gate(event_type, payload)


class _NeverEndingSseResponse:
    def __init__(self) -> None:
        self.read_started = asyncio.Event()

    async def aiter_lines(self):
        self.read_started.set()
        await asyncio.Event().wait()
        yield ""


class _FailingSseResponse:
    def __init__(self) -> None:
        self.read_started = asyncio.Event()

    async def aiter_lines(self):
        self.read_started.set()
        raise ValueError("stream read failed")
        yield ""


def _content_payload(
    request_id: str,
    delta: str,
    *,
    content_index: int = 0,
    provider_event_type: str = "chunk",
) -> dict[str, Any]:
    return {
        "llm_call_id": "call-1",
        "llm_request_id": request_id,
        "candidate_index": 0,
        "content_index": content_index,
        "provider_event_type": provider_event_type,
        "delta": delta,
    }


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("condition was not met")
        await asyncio.sleep(0)


def test_content_delta_coalescing_uses_one_drain_and_preserves_text() -> None:
    async def run_case() -> None:
        gate = _GatePublisher()
        registry = RuntimeTaskRegistry()
        queue = LlmRuntimeEventPublishQueue(
            publish=gate,
            conversation_id="conversation-1",
            llm_request_id="request-1",
            task_registry=registry,
        )

        await queue.submit("llm.content.delta", _content_payload("request-1", "A"))
        await gate.wait_started(1)
        for delta in ("B", "C", "D"):
            await queue.submit("llm.content.delta", _content_payload("request-1", delta))

        snapshot = queue.snapshot()
        assert snapshot["pending_count"] == 2
        assert snapshot["queued_message_count"] == 1
        assert snapshot["coalesced_content_event_count"] == 2
        registry_snapshot = registry.snapshot(include_completed=False)
        assert registry_snapshot["active_by_class"] == {"runtime_event_publish_drain": 1}

        gate.release.set()
        await asyncio.wait_for(queue.close(), timeout=1)
        await asyncio.sleep(0)

        assert len(gate.completed) == 2
        assert "".join(payload["delta"] for event_type, payload in gate.completed if event_type == "llm.content.delta") == "ABCD"
        final_snapshot = queue.snapshot()
        assert final_snapshot["closed"] is True
        assert final_snapshot["pending_count"] == 0
        assert final_snapshot["published_message_count"] == 2
        assert registry.snapshot()["total_count"] == 1

    asyncio.run(run_case())


def test_request_count_limit_backpressures_content_but_reserved_control_is_admitted() -> None:
    async def run_case() -> None:
        limits = LlmRuntimeEventPublishAdmissionLimits(
            request_max_pending_count=3,
            request_reserved_control_count=1,
            request_max_pending_bytes=64 * 1024,
            request_reserved_control_bytes=8 * 1024,
            global_max_pending_count=20,
            global_reserved_control_count=4,
            global_max_pending_bytes=512 * 1024,
            global_reserved_control_bytes=64 * 1024,
        )
        gate = _GatePublisher()
        queue = LlmRuntimeEventPublishQueue(
            publish=gate,
            conversation_id="conversation-1",
            llm_request_id="request-count",
            limits=limits,
            task_registry=RuntimeTaskRegistry(),
        )

        await queue.submit("llm.content.delta", _content_payload("request-count", "A", content_index=0))
        await gate.wait_started(1)
        await queue.submit("llm.content.delta", _content_payload("request-count", "B", content_index=1))
        blocked_content = asyncio.create_task(
            queue.submit("llm.content.delta", _content_payload("request-count", "C", content_index=2))
        )
        await _wait_until(lambda: queue.snapshot()["local_wait_count"] >= 1)
        assert blocked_content.done() is False

        await queue.submit("llm.content.completed", {"llm_request_id": "request-count"})
        snapshot = queue.snapshot()
        assert snapshot["pending_count"] == limits.request_max_pending_count
        assert snapshot["peak_pending_count"] <= limits.request_max_pending_count
        assert blocked_content.done() is False

        gate.release.set()
        await asyncio.wait_for(blocked_content, timeout=1)
        await asyncio.wait_for(queue.close(), timeout=1)

        event_types = [event_type for event_type, _payload in gate.completed]
        assert event_types.count("llm.content.delta") == 3
        assert "llm.content.completed" in event_types

    asyncio.run(run_case())


def test_request_byte_limit_backpressures_content_but_preserves_control_reserve() -> None:
    async def run_case() -> None:
        first = _content_payload("request-bytes", "A" * 24, content_index=0)
        second = _content_payload("request-bytes", "B" * 24, content_index=1)
        third = _content_payload("request-bytes", "C" * 24, content_index=2)
        control = {"llm_request_id": "request-bytes", "provider_event_type": "done"}
        content_capacity = _payload_size_bytes(first) + _payload_size_bytes(second)
        control_reserve = _payload_size_bytes(control)
        request_max_bytes = content_capacity + control_reserve
        limits = LlmRuntimeEventPublishAdmissionLimits(
            request_max_pending_count=10,
            request_reserved_control_count=2,
            request_max_pending_bytes=request_max_bytes,
            request_reserved_control_bytes=control_reserve,
            global_max_pending_count=40,
            global_reserved_control_count=8,
            global_max_pending_bytes=request_max_bytes * 8,
            global_reserved_control_bytes=request_max_bytes,
        )
        gate = _GatePublisher()
        queue = LlmRuntimeEventPublishQueue(
            publish=gate,
            conversation_id="conversation-1",
            llm_request_id="request-bytes",
            limits=limits,
            task_registry=RuntimeTaskRegistry(),
        )

        await queue.submit("llm.content.delta", first)
        await gate.wait_started(1)
        await queue.submit("llm.content.delta", second)
        blocked_content = asyncio.create_task(queue.submit("llm.content.delta", third))
        await _wait_until(lambda: queue.snapshot()["local_wait_count"] >= 1)
        assert blocked_content.done() is False

        await queue.submit("llm.content.completed", control)
        snapshot = queue.snapshot()
        assert snapshot["pending_bytes"] == request_max_bytes
        assert snapshot["peak_pending_bytes"] <= request_max_bytes

        gate.release.set()
        await asyncio.wait_for(blocked_content, timeout=1)
        await asyncio.wait_for(queue.close(), timeout=1)
        assert "llm.content.completed" in [event_type for event_type, _payload in gate.completed]

    asyncio.run(run_case())


def test_global_count_limit_is_shared_across_requests_and_reserves_control_capacity() -> None:
    async def run_case() -> None:
        limits = LlmRuntimeEventPublishAdmissionLimits(
            request_max_pending_count=8,
            request_reserved_control_count=2,
            request_max_pending_bytes=128 * 1024,
            request_reserved_control_bytes=16 * 1024,
            global_max_pending_count=3,
            global_reserved_control_count=1,
            global_max_pending_bytes=512 * 1024,
            global_reserved_control_bytes=64 * 1024,
        )
        gate = _GatePublisher()
        registry = RuntimeTaskRegistry()
        queues = [
            LlmRuntimeEventPublishQueue(
                publish=gate,
                conversation_id="conversation-1",
                llm_request_id=f"global-request-{index}",
                limits=limits,
                task_registry=registry,
            )
            for index in range(3)
        ]

        await queues[0].submit("llm.content.delta", _content_payload("global-request-0", "A"))
        await gate.wait_started(1)
        await queues[1].submit("llm.content.delta", _content_payload("global-request-1", "B"))
        await gate.wait_started(2)

        blocked_content = asyncio.create_task(
            queues[2].submit("llm.content.delta", _content_payload("global-request-2", "C"))
        )
        await _wait_until(lambda: queues[2].snapshot()["global"]["wait_count"] >= 1)
        assert blocked_content.done() is False

        await queues[2].submit("llm.content.completed", {"llm_request_id": "global-request-2"})
        await gate.wait_started(3)
        global_snapshot = queues[2].snapshot()["global"]
        assert global_snapshot["pending_count"] == limits.global_max_pending_count
        assert global_snapshot["peak_pending_count"] <= limits.global_max_pending_count
        process_snapshot = llm_runtime_event_publish_admission_snapshot()
        assert any(
            budget["max_pending_count"] == limits.global_max_pending_count
            and budget["pending_count"] == limits.global_max_pending_count
            for budget in process_snapshot["budgets"]
        )

        gate.release.set()
        await asyncio.wait_for(blocked_content, timeout=1)
        await asyncio.wait_for(asyncio.gather(*(queue.close() for queue in queues)), timeout=1)
        assert queues[2].snapshot()["global_wait_count"] >= 1
        assert "llm.content.completed" in [event_type for event_type, _payload in gate.completed]

    asyncio.run(run_case())


def test_global_byte_limit_reserves_space_for_control_across_requests() -> None:
    async def run_case() -> None:
        first = _content_payload("global-bytes-0", "A" * 32)
        second = _content_payload("global-bytes-1", "B" * 32)
        third = _content_payload("global-bytes-2", "C" * 32)
        control = {"llm_request_id": "global-bytes-2", "provider_event_type": "done"}
        content_capacity = _payload_size_bytes(first) + _payload_size_bytes(second)
        control_reserve = _payload_size_bytes(control)
        global_max_bytes = content_capacity + control_reserve
        limits = LlmRuntimeEventPublishAdmissionLimits(
            request_max_pending_count=10,
            request_reserved_control_count=2,
            request_max_pending_bytes=global_max_bytes * 2,
            request_reserved_control_bytes=control_reserve,
            global_max_pending_count=10,
            global_reserved_control_count=2,
            global_max_pending_bytes=global_max_bytes,
            global_reserved_control_bytes=control_reserve,
        )
        gate = _GatePublisher()
        queues = [
            LlmRuntimeEventPublishQueue(
                publish=gate,
                conversation_id="conversation-1",
                llm_request_id=f"global-bytes-{index}",
                limits=limits,
                task_registry=RuntimeTaskRegistry(),
            )
            for index in range(3)
        ]

        await queues[0].submit("llm.content.delta", first)
        await gate.wait_started(1)
        await queues[1].submit("llm.content.delta", second)
        await gate.wait_started(2)
        blocked_content = asyncio.create_task(queues[2].submit("llm.content.delta", third))
        await _wait_until(lambda: queues[2].snapshot()["global"]["wait_count"] >= 1)
        assert blocked_content.done() is False

        await queues[2].submit("llm.content.completed", control)
        await gate.wait_started(3)
        snapshot = queues[2].snapshot()["global"]
        assert snapshot["pending_bytes"] == global_max_bytes
        assert snapshot["peak_pending_bytes"] <= global_max_bytes

        gate.release.set()
        await asyncio.wait_for(blocked_content, timeout=1)
        await asyncio.wait_for(asyncio.gather(*(queue.close() for queue in queues)), timeout=1)

    asyncio.run(run_case())


def test_cancelled_drain_releases_active_and_queued_global_budget() -> None:
    async def run_case() -> None:
        limits = LlmRuntimeEventPublishAdmissionLimits(
            request_max_pending_count=4,
            request_reserved_control_count=1,
            request_max_pending_bytes=64 * 1024,
            request_reserved_control_bytes=8 * 1024,
            global_max_pending_count=8,
            global_reserved_control_count=2,
            global_max_pending_bytes=256 * 1024,
            global_reserved_control_bytes=32 * 1024,
        )
        gate = _GatePublisher()
        registry = RuntimeTaskRegistry()
        queue = LlmRuntimeEventPublishQueue(
            publish=gate,
            conversation_id="conversation-1",
            llm_request_id="cancelled-drain",
            limits=limits,
            task_registry=registry,
        )

        await queue.submit("llm.content.delta", _content_payload("cancelled-drain", "A", content_index=0))
        await gate.wait_started(1)
        await queue.submit("llm.content.delta", _content_payload("cancelled-drain", "B", content_index=1))
        assert queue.snapshot()["pending_count"] == 2
        assert queue.snapshot()["global"]["pending_count"] == 2

        drain_task = queue._drain_task
        assert drain_task is not None
        assert registry.cancel(drain_task, message="test cancellation") is True
        await asyncio.wait_for(queue.wait_closed(), timeout=1)
        await queue.close()

        snapshot = queue.snapshot()
        assert snapshot["pending_count"] == 0
        assert snapshot["pending_bytes"] == 0
        assert snapshot["global"]["pending_count"] == 0
        assert snapshot["global"]["pending_bytes"] == 0

    asyncio.run(run_case())


def test_oversized_event_is_published_inline_and_observed_without_pending_growth() -> None:
    async def run_case() -> None:
        limits = LlmRuntimeEventPublishAdmissionLimits(
            request_max_pending_count=4,
            request_reserved_control_count=1,
            request_max_pending_bytes=128,
            request_reserved_control_bytes=32,
            global_max_pending_count=8,
            global_reserved_control_count=2,
            global_max_pending_bytes=256,
            global_reserved_control_bytes=64,
        )
        gate = _GatePublisher()
        gate.release.set()
        registry = RuntimeTaskRegistry()
        queue = LlmRuntimeEventPublishQueue(
            publish=gate,
            conversation_id="conversation-1",
            llm_request_id="oversized",
            limits=limits,
            task_registry=registry,
        )

        await queue.submit("llm.content.delta", _content_payload("oversized", "x" * 512))
        await queue.close()

        snapshot = queue.snapshot()
        assert snapshot["oversized_inline_count"] == 1
        assert snapshot["pending_count"] == 0
        assert snapshot["published_message_count"] == 1
        assert registry.snapshot()["total_count"] == 0
        assert gate.completed[0][1]["delta"] == "x" * 512

    asyncio.run(run_case())


def test_stream_event_emitter_awaits_async_handler_and_keeps_sync_handler_compatible() -> None:
    async def run_case() -> None:
        calls: list[tuple[str, str]] = []

        def sync_handler(event: dict[str, Any]) -> int:
            calls.append(("sync", event["event_type"]))
            return 7

        async def async_handler(event: dict[str, Any]) -> None:
            await asyncio.sleep(0)
            calls.append(("async", event["event_type"]))

        await _emit_stream_event(sync_handler, "sync.event", {})
        await _emit_stream_event(async_handler, "async.event", {})
        assert calls == [("sync", "sync.event"), ("async", "async.event")]

    asyncio.run(run_case())


def test_sse_reader_notifies_async_handler_on_task_cancellation() -> None:
    async def run_case() -> None:
        response = _NeverEndingSseResponse()
        event_types: list[str] = []

        async def handler(event: dict[str, Any]) -> None:
            event_types.append(event["event_type"])

        task = asyncio.create_task(_read_sse_response_text(response, "text/event-stream", handler, None))
        await asyncio.wait_for(response.read_started.wait(), timeout=1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("SSE read task should be cancelled")

        assert event_types == ["llm.response.stream.started", "llm.response.stream.failed"]

    asyncio.run(run_case())


def test_cancelled_sse_request_waits_for_admitted_request_handler_drain() -> None:
    async def run_case() -> None:
        publisher = _BlockingRuntimeEventPublisher()
        stream_state = {"response_started": False, "response_failed": False}
        handler = _build_stream_event_handler(
            publisher=publisher,
            context=object(),
            conversation_id="conversation-1",
            request_event_observer=None,
            llm_call_id="call-1",
            llm_request_id="request-cancel",
            provider_id="provider-1",
            binding_id="binding-1",
            model_id="model-1",
            model_alias="model-alias",
            api_format=API_FORMAT_OPENAI_CHAT_COMPLETIONS,
            candidate_index=0,
            api_key_index=0,
            api_key_label=None,
            retry_index=0,
            attempt_count=1,
            stream_state=stream_state,
        )
        response = _NeverEndingSseResponse()
        task = asyncio.create_task(_read_sse_response_text(response, "text/event-stream", handler, None))
        await asyncio.wait_for(response.read_started.wait(), timeout=1)
        await publisher.gate.wait_started(1)

        task.cancel()
        await _wait_until(lambda: stream_state["response_failed"] is True)
        assert task.done() is False

        publisher.gate.release.set()
        try:
            await asyncio.wait_for(task, timeout=1)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("SSE read task should remain cancelled")
        assert [event_type for event_type, _payload in publisher.gate.completed] == ["llm.response.started"]

    asyncio.run(run_case())


def test_failed_sse_request_waits_for_admitted_request_handler_drain() -> None:
    async def run_case() -> None:
        publisher = _BlockingRuntimeEventPublisher()
        stream_state = {"response_started": False, "response_failed": False}
        handler = _build_stream_event_handler(
            publisher=publisher,
            context=object(),
            conversation_id="conversation-1",
            request_event_observer=None,
            llm_call_id="call-1",
            llm_request_id="request-failed",
            provider_id="provider-1",
            binding_id="binding-1",
            model_id="model-1",
            model_alias="model-alias",
            api_format=API_FORMAT_OPENAI_CHAT_COMPLETIONS,
            candidate_index=0,
            api_key_index=0,
            api_key_label=None,
            retry_index=0,
            attempt_count=1,
            stream_state=stream_state,
        )
        response = _FailingSseResponse()
        task = asyncio.create_task(_read_sse_response_text(response, "text/event-stream", handler, None))
        await asyncio.wait_for(response.read_started.wait(), timeout=1)
        await publisher.gate.wait_started(1)
        await _wait_until(lambda: stream_state["response_failed"] is True)
        assert task.done() is False

        publisher.gate.release.set()
        try:
            await asyncio.wait_for(task, timeout=1)
        except ValueError as exc:
            assert str(exc) == "stream read failed"
        else:
            raise AssertionError("SSE read task should preserve the stream failure")
        assert [event_type for event_type, _payload in publisher.gate.completed] == ["llm.response.started"]

    asyncio.run(run_case())


def test_request_stream_handler_coalesces_published_delta_but_preserves_observer_events() -> None:
    async def run_case() -> None:
        publisher = _BlockingRuntimeEventPublisher()
        observed: list[tuple[str, dict[str, Any]]] = []
        stream_state = {"response_started": False, "response_failed": False}
        handler = _build_stream_event_handler(
            publisher=publisher,
            context=object(),
            conversation_id="conversation-1",
            request_event_observer=lambda event_type, payload: observed.append((event_type, payload)),
            llm_call_id="call-1",
            llm_request_id="request-integration",
            provider_id="provider-1",
            binding_id="binding-1",
            model_id="model-1",
            model_alias="model-alias",
            api_format=API_FORMAT_OPENAI_CHAT_COMPLETIONS,
            candidate_index=0,
            api_key_index=0,
            api_key_label=None,
            retry_index=0,
            attempt_count=1,
            stream_state=stream_state,
        )

        await handler({"event_type": "llm.response.stream.started", "payload": {"content_type": "text/event-stream"}})
        await publisher.gate.wait_started(1)
        for delta in ("A", "B", "C"):
            await handler(
                {
                    "event_type": "llm.response.stream.event",
                    "payload": {
                        "event": "message",
                        "data": {"choices": [{"index": 0, "delta": {"content": delta}}]},
                    },
                }
            )

        completion = asyncio.create_task(
            handler({"event_type": "llm.response.stream.completed", "payload": {"event_count": 3}})
        )
        await asyncio.sleep(0)
        assert completion.done() is False

        publisher.gate.release.set()
        await asyncio.wait_for(completion, timeout=1)

        published_deltas = [
            payload["delta"]
            for event_type, payload in publisher.gate.completed
            if event_type == "llm.content.delta"
        ]
        observed_deltas = [
            payload["delta"]
            for event_type, payload in observed
            if event_type == "llm.content.delta"
        ]
        assert published_deltas == ["ABC"]
        assert observed_deltas == ["A", "B", "C"]
        assert stream_state["response_started"] is True

    asyncio.run(run_case())

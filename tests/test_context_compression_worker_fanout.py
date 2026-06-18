import asyncio
import unittest
from typing import Any

from agent_runtime.context_compression import compression_service as compression_service_module
from agent_runtime.context_compression.compression_service import ContextCompressionService
from agent_runtime.context_compression.config import ContextCompressionConfig
from agent_runtime.context_compression.event_publisher import (
    CONTEXT_COMPRESSION_CANCELLED,
    CONTEXT_COMPRESSION_RANGE_PLANNED,
    CONTEXT_COMPRESSION_SECTION_COMPLETED,
    CONTEXT_COMPRESSION_SECTION_STARTED,
    CONTEXT_COMPRESSION_STARTED,
    CONTEXT_COMPRESSION_WORKER_CANCELLED,
    CONTEXT_COMPRESSION_WORKER_QUEUED,
    ContextCompressionEventPublisher,
)
from agent_runtime.context_compression.job_store import InMemoryCompressionJobStore
from agent_runtime.context_compression.models import (
    CompressionDecision,
    CompressionJob,
    SECTION_WORKERS,
    SectionItem,
)
from agent_runtime.context_compression.worker_input import ContextCompressionWorkerInput
from agent_runtime.conversation_message.message.models import PROVIDER_OPENAI, ROLE_ASSISTANT, TextPart, TextPartMeta, UniMessage
from agent_runtime.conversation_message.message.operations import create_conversation
from agent_runtime.infrastructure.runtime_task_registry import RuntimeTaskRegistry
from agent_runtime.llm_client.llm_protocol_clients import LlmRequestCancelled


def _job() -> CompressionJob:
    return CompressionJob(
        job_id="job-stage8-p12",
        task_id="task-stage8-p12",
        thread_id="thread-stage8-p12",
        agent_id="agent-stage8-p12",
        agent_instance_id="instance-stage8-p12",
        root_conversation_id="root-stage8-p12",
        source_conversation_id="conversation-stage8-p12",
        compression_index=8,
        status="running",
        input_mode="transcript_snapshot",
    )


class _PromptLoader:
    def load_prompt(self, *, input_mode: str, worker_name: str) -> str:
        return f"prompt for {worker_name} in {input_mode}"


class _RuntimeEventSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any], str]] = []

    async def publish(
        self,
        _context: Any,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        source: str = "context_compression_service",
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        del correlation_id, metadata
        self.events.append((event_type, dict(payload or {}), source))


class _FakeWorkerRunner:
    def __init__(self, behaviors: dict[str, str] | None = None) -> None:
        self.behaviors = dict(behaviors or {})
        self.started: dict[str, asyncio.Event] = {worker_name: asyncio.Event() for worker_name, _section in SECTION_WORKERS}
        self.block_release: dict[str, asyncio.Event] = {worker_name: asyncio.Event() for worker_name, _section in SECTION_WORKERS}
        self.cancelled_workers: list[str] = []
        self.calls: list[str] = []

    async def run_worker(
        self,
        *,
        job: CompressionJob,
        worker_name: str,
        worker_role: str,
        output_section: str | None,
        input_mode: str,
        system_prompt: str,
        worker_input: ContextCompressionWorkerInput,
        resolved_candidates: tuple[Any, ...],
        max_rounds: int,
        communication_context: Any,
        allowed_tool_names: set[str] | None = None,
        interrupt_checker: Any = None,
        request_canceller_observer: Any = None,
    ) -> None:
        del worker_role, input_mode, system_prompt, worker_input, resolved_candidates, max_rounds
        del communication_context, allowed_tool_names, interrupt_checker, request_canceller_observer
        self.calls.append(worker_name)
        self.started[worker_name].set()
        behavior = self.behaviors.get(worker_name, "success")
        try:
            if behavior == "failure":
                raise RuntimeError("secret raw worker payload should not leak through registry diagnostics")
            if behavior == "llm_cancel":
                raise LlmRequestCancelled("secret raw cancel text should not become a failure")
            if behavior == "blocked":
                await self.block_release[worker_name].wait()
            if output_section is not None:
                job.section_items.append(
                    SectionItem(
                        section=output_section,
                        content=f"{worker_name} extracted facts",
                        priority="P1",
                        status="active",
                        worker_name=worker_name,
                    )
                )
                job.completed_sections.add(output_section)
        except asyncio.CancelledError:
            self.cancelled_workers.append(worker_name)
            raise


def _service(worker_runner: _FakeWorkerRunner, sink: _RuntimeEventSink) -> ContextCompressionService:
    service = ContextCompressionService.__new__(ContextCompressionService)
    service._prompt_loader = _PromptLoader()
    service._worker_runner = worker_runner
    service._event_publisher = ContextCompressionEventPublisher(sink)
    service._job_store = InMemoryCompressionJobStore()
    return service


def _event_types(sink: _RuntimeEventSink) -> list[str]:
    return [event_type for event_type, _payload, _source in sink.events]


def _worker_names_for_event(sink: _RuntimeEventSink, event_type: str) -> list[str]:
    names: list[str] = []
    for current_event_type, payload, _source in sink.events:
        if current_event_type == event_type:
            names.append(str(payload.get("worker", {}).get("name")))
    return names


def _cancel_reasons(sink: _RuntimeEventSink) -> dict[str, str]:
    result: dict[str, str] = {}
    for event_type, payload, _source in sink.events:
        if event_type == CONTEXT_COMPRESSION_WORKER_CANCELLED:
            worker = payload.get("worker", {})
            result[str(worker.get("name"))] = str(worker.get("reason"))
    return result


def _registry_record(registry: RuntimeTaskRegistry) -> dict[str, Any]:
    snapshot = registry.snapshot()
    assert snapshot["tasks"], snapshot
    return snapshot["tasks"][0]


class ContextCompressionWorkerFanoutContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._original_registry_accessor = compression_service_module.get_runtime_task_registry
        self._original_decide_context_compression = compression_service_module.decide_context_compression
        self.registry = RuntimeTaskRegistry(completed_task_retention=16)
        compression_service_module.get_runtime_task_registry = lambda: self.registry

    async def asyncTearDown(self) -> None:
        compression_service_module.get_runtime_task_registry = self._original_registry_accessor
        compression_service_module.decide_context_compression = self._original_decide_context_compression

    async def _run_under_registry(self, service: ContextCompressionService, job: CompressionJob) -> None:
        service._job_store.create_job(job)
        await service._run_section_workers(
            job=job,
            worker_input=ContextCompressionWorkerInput(input_mode="transcript_snapshot", user_text="worker input must not leak"),
            input_mode="transcript_snapshot",
            resolved_candidates=(),
            max_rounds=1,
            communication_context=None,
            interrupt_checker=None,
            request_canceller_observer=None,
        )

    async def test_section_worker_fanout_success_orders_queued_before_started_and_completes(self) -> None:
        sink = _RuntimeEventSink()
        worker_runner = _FakeWorkerRunner()
        service = _service(worker_runner, sink)
        job = _job()

        owner = self.registry.create_task(
            self._run_under_registry(service, job),
            task_name="context-compression-owner-success",
            owner_type="agent_execution_engine",
            owner_id=job.agent_instance_id,
            task_class="agent_instance_execution",
            diagnostic_context={"phase": "owner.started"},
        )
        await asyncio.wait_for(owner, timeout=2)

        event_types = _event_types(sink)
        first_started_index = min(
            event_types.index(CONTEXT_COMPRESSION_SECTION_STARTED),
            event_types.index(CONTEXT_COMPRESSION_SECTION_COMPLETED),
        )
        queued_indexes = [index for index, event_type in enumerate(event_types) if event_type == CONTEXT_COMPRESSION_WORKER_QUEUED]
        self.assertEqual(len(queued_indexes), len(SECTION_WORKERS))
        self.assertTrue(all(index < first_started_index for index in queued_indexes))
        self.assertEqual(set(_worker_names_for_event(sink, CONTEXT_COMPRESSION_SECTION_STARTED)), {name for name, _section in SECTION_WORKERS})
        self.assertEqual(set(_worker_names_for_event(sink, CONTEXT_COMPRESSION_SECTION_COMPLETED)), {name for name, _section in SECTION_WORKERS})
        self.assertEqual(job.status, "running")
        self.assertEqual(job.completed_sections, {section for _name, section in SECTION_WORKERS})
        self.assertFalse(
            [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_name().startswith("context-compression-section-worker-")
                and not task.done()
            ]
        )

        record = _registry_record(self.registry)
        self.assertEqual(record["status"], "completed")
        context = record["diagnostic_context"]
        self.assertEqual(context["phase"], "section_workers.cleanup_completed")
        self.assertEqual(context["worker_count"], len(SECTION_WORKERS))
        self.assertEqual(context["completed_worker_count"], len(SECTION_WORKERS))
        self.assertEqual(context["cancelled_worker_count"], 0)
        diagnostic_text = str(context).lower()
        self.assertNotIn("worker input must not leak", diagnostic_text)
        self.assertNotIn("prompt", diagnostic_text)
        self.assertNotIn("payload", diagnostic_text)

    async def test_first_exception_cancels_pending_siblings_and_avoids_completion_events(self) -> None:
        failing_worker = SECTION_WORKERS[0][0]
        blocked_workers = [worker_name for worker_name, _section in SECTION_WORKERS[1:]]
        behaviors = {failing_worker: "failure", **{worker_name: "blocked" for worker_name in blocked_workers}}
        sink = _RuntimeEventSink()
        worker_runner = _FakeWorkerRunner(behaviors)
        service = _service(worker_runner, sink)
        job = _job()

        with self.assertRaisesRegex(RuntimeError, "secret raw worker payload"):
            await self._run_under_registry(service, job)

        self.assertEqual(job.status, "failed")
        self.assertEqual(set(worker_runner.cancelled_workers), set(blocked_workers))
        cancel_reasons = _cancel_reasons(sink)
        self.assertEqual(
            {worker: cancel_reasons[worker] for worker in blocked_workers},
            {worker: "sibling_worker_failed" for worker in blocked_workers},
        )
        completed_workers = set(_worker_names_for_event(sink, CONTEXT_COMPRESSION_SECTION_COMPLETED))
        self.assertNotIn(failing_worker, completed_workers)
        self.assertTrue(completed_workers.isdisjoint(blocked_workers))
        self.assertNotIn("secret raw worker payload", str(self.registry.snapshot()).lower())

    async def test_llm_request_cancelled_is_cancelled_not_failed(self) -> None:
        cancelling_worker = SECTION_WORKERS[0][0]
        blocked_workers = [worker_name for worker_name, _section in SECTION_WORKERS[1:]]
        behaviors = {cancelling_worker: "llm_cancel", **{worker_name: "blocked" for worker_name in blocked_workers}}
        sink = _RuntimeEventSink()
        worker_runner = _FakeWorkerRunner(behaviors)
        service = _service(worker_runner, sink)
        job = _job()

        with self.assertRaises(LlmRequestCancelled):
            await self._run_under_registry(service, job)

        self.assertEqual(job.status, "cancelled")
        self.assertEqual(set(worker_runner.cancelled_workers), set(blocked_workers))
        cancel_reasons = _cancel_reasons(sink)
        self.assertEqual(cancel_reasons[cancelling_worker], "interrupted")
        self.assertEqual(
            {worker: cancel_reasons[worker] for worker in blocked_workers},
            {worker: "context_compression_cancelled" for worker in blocked_workers},
        )
        self.assertNotIn("secret raw cancel text", str(self.registry.snapshot()).lower())

    async def test_outer_cancellation_cancels_and_gathers_pending_workers(self) -> None:
        behaviors = {worker_name: "blocked" for worker_name, _section in SECTION_WORKERS}
        sink = _RuntimeEventSink()
        worker_runner = _FakeWorkerRunner(behaviors)
        service = _service(worker_runner, sink)
        job = _job()

        owner = asyncio.create_task(self._run_under_registry(service, job), name="context-compression-owner-cancel")
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in worker_runner.started.values())), timeout=2)
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        await asyncio.sleep(0)

        self.assertEqual(job.status, "cancelled")
        self.assertEqual(set(worker_runner.cancelled_workers), {worker_name for worker_name, _section in SECTION_WORKERS})
        self.assertEqual(set(_cancel_reasons(sink).values()), {"context_compression_cancelled"})
        self.assertFalse(
            [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_name().startswith("context-compression-section-worker-")
                and not task.done()
            ]
        )

    async def test_run_if_needed_llm_cancel_returns_interrupted_without_conversation_rebind(self) -> None:
        sink = _RuntimeEventSink()
        service = ContextCompressionService.__new__(ContextCompressionService)
        service._config_accessor = lambda: ContextCompressionConfig(
            input_mode="transcript_snapshot",
            max_worker_rounds=1,
            verifier_enabled=False,
        )
        service._settings_repository = type(
            "SettingsRepo",
            (),
            {
                "get_settings": lambda _self: type("Settings", (), {"input_mode": None})(),
                "get_llm_policy_group_id": lambda _self: "policy-stage8-p12",
            },
        )()
        service._resolve_worker_candidates = lambda _config: (
            type(
                "Candidate",
                (),
                {
                    "supports_tool_calling": True,
                    "supported_input_modalities": (),
                },
            )(),
        )
        service._archive_repository = _ArchiveRepository()
        service._job_store = InMemoryCompressionJobStore()
        service._event_publisher = ContextCompressionEventPublisher(sink)
        service._build_worker_input = _async_return(
            ContextCompressionWorkerInput(input_mode="transcript_snapshot", user_text="secret conversation text")
        )
        service._run_section_workers = _async_raise(LlmRequestCancelled("secret raw cancel text"))
        service._conversation_repository = _ConversationRepository()
        service._runtime_gateway = _RuntimeGateway()
        compression_service_module.decide_context_compression = lambda *_args, **_kwargs: CompressionDecision(
            should_compress=True,
            reason="stage8_p12_contract",
        )

        result = await service.compress_if_needed(
            task=type("Task", (), {"task_id": "task-stage8-p12"})(),
            instance=type(
                "Instance",
                (),
                {
                    "instance_id": "instance-stage8-p12",
                    "thread_id": "thread-stage8-p12",
                    "agent_definition_id": "agent-stage8-p12",
                },
            )(),
            source_conversation=_conversation(),
            trigger_model=type("Model", (), {})(),
            communication_context=None,
        )

        self.assertFalse(result.created)
        self.assertEqual(result.reason, "interrupted")
        self.assertEqual(service._runtime_gateway.switched_conversations, [])
        self.assertEqual([event_type for event_type, _payload, _source in sink.events], [
            CONTEXT_COMPRESSION_STARTED,
            CONTEXT_COMPRESSION_RANGE_PLANNED,
            CONTEXT_COMPRESSION_CANCELLED,
        ])
        cancelled_payload = sink.events[-1][1]
        self.assertEqual(cancelled_payload["status"], "cancelled")
        self.assertEqual(cancelled_payload["reason"], "interrupted")


class _ArchiveRepository:
    def root_conversation_id_for(self, *, agent_instance_id: str, source_conversation_id: str) -> str:
        del agent_instance_id, source_conversation_id
        return "root-stage8-p12"

    def next_compression_index(self, *, task_id: str, agent_instance_id: str, root_conversation_id: str) -> int:
        del task_id, agent_instance_id, root_conversation_id
        return 8

    def upsert_job(self, job: CompressionJob, *, source_conversation: Any, target_conversation: Any | None = None) -> None:
        del job, source_conversation, target_conversation


def _conversation():
    conversation = create_conversation()
    conversation.id = "conversation-stage8-p12"
    conversation.add_user_message([TextPart("old user message")], [TextPartMeta()])
    conversation.append_message(
        UniMessage(
            role=ROLE_ASSISTANT,
            content=[TextPart("latest assistant anchor")],
            source_provider=PROVIDER_OPENAI,
        )
    )
    conversation.add_user_message([TextPart("retained tail")], [TextPartMeta()])
    return conversation


class _ConversationRepository:
    async def create(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("conversation rebind must not create a target conversation after LLM cancellation")


class _RuntimeGateway:
    def __init__(self) -> None:
        self.switched_conversations: list[tuple[str, str]] = []

    def switch_agent_instance_conversation(self, instance_id: str, conversation_id: str) -> None:
        self.switched_conversations.append((instance_id, conversation_id))


def _async_return(value: Any):
    async def inner(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return inner


def _async_raise(exc: BaseException):
    async def inner(*_args: Any, **_kwargs: Any) -> None:
        raise exc

    return inner


if __name__ == "__main__":
    unittest.main()

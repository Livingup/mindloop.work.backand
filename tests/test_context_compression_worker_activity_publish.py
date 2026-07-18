import asyncio
import unittest
from typing import Any

from agent_runtime.context_compression import worker_runner as worker_runner_module
from agent_runtime.context_compression.event_publisher import (
    CONTEXT_COMPRESSION_WORKER_ACTIVITY,
    CONTEXT_COMPRESSION_WORKER_COMPLETED,
    CONTEXT_COMPRESSION_WORKER_PROGRESS,
    CONTEXT_COMPRESSION_WORKER_STARTED,
    ContextCompressionEventPublisher,
)
from agent_runtime.context_compression.job_store import InMemoryCompressionJobStore
from agent_runtime.context_compression.models import CompressionJob, SectionItem
from agent_runtime.context_compression.summary_tools import MARK_SECTION_COMPLETE
from agent_runtime.context_compression.worker_input import ContextCompressionWorkerInput
from agent_runtime.context_compression.worker_runner import ContextCompressionWorkerRunner
from agent_runtime.conversation_message.message.models import ROLE_ASSISTANT, TextPart, ToolUsePart, UniMessage
from agent_runtime.infrastructure.runtime_task_registry import RuntimeTaskRegistry
from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_RESPONSES


RAW_ACTIVITY_TEXT = "SECRET_ACTIVITY_TEXT_SHOULD_NOT_LEAK"
RAW_ACTIVITY_ERROR = "secret raw activity text token should not leak"
WORKER_INPUT_SECRET = "SECRET_WORKER_INPUT_SHOULD_NOT_LEAK"


def _job() -> CompressionJob:
    return CompressionJob(
        job_id="job-stage8-p13",
        task_id="task-stage8-p13",
        thread_id="thread-stage8-p13",
        agent_id="agent-stage8-p13",
        agent_instance_id="instance-stage8-p13",
        root_conversation_id="root-stage8-p13",
        source_conversation_id="conversation-stage8-p13",
        compression_index=13,
        status="running",
        input_mode="native_multimodal_history",
    )


def _resolved_model() -> Any:
    return type(
        "ResolvedModel",
        (),
        {
            "supports_tool_calling": True,
            "api_format": API_FORMAT_OPENAI_RESPONSES,
            "supported_input_modalities": (),
            "provider_id": "provider-stage8-p13",
            "binding_id": "binding-stage8-p13",
            "model_id": "model-stage8-p13",
        },
    )()


class _RuntimeEventSink:
    def __init__(self, *, fail_activity: bool = False) -> None:
        self.fail_activity = fail_activity
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
        if self.fail_activity and event_type == CONTEXT_COMPRESSION_WORKER_ACTIVITY:
            raise RuntimeError(RAW_ACTIVITY_ERROR)
        self.events.append((event_type, dict(payload or {}), source))


class _FakeLlmRequestService:
    def __init__(self, events: list[tuple[str, dict[str, Any]]]) -> None:
        self.events = events

    async def send_prompt(self, request: Any) -> Any:
        for event_type, payload in self.events:
            request.request_event_observer(event_type, dict(payload))
        return type(
            "Response",
            (),
            {
                "assistant_message": UniMessage(
                    role=ROLE_ASSISTANT,
                    content=[
                        ToolUsePart(
                            name=MARK_SECTION_COMPLETE,
                            input={"section": "requirements_constraints"},
                            tool_use_id="tool-stage8-p13",
                        )
                    ],
                )
            },
        )()


def _runner(sink: _RuntimeEventSink, events: list[tuple[str, dict[str, Any]]]) -> ContextCompressionWorkerRunner:
    job_store = InMemoryCompressionJobStore()
    return ContextCompressionWorkerRunner(
        llm_request_service=_FakeLlmRequestService(events),
        job_store=job_store,
        event_publisher=ContextCompressionEventPublisher(sink),
    )


def _activity_publish_records(registry: RuntimeTaskRegistry) -> list[dict[str, Any]]:
    return [
        task
        for task in registry.snapshot()["tasks"]
        if task["task_class"] == "context_compression_worker_activity_publish"
    ]


async def _wait_for_activity_tasks(registry: RuntimeTaskRegistry, expected_count: int) -> list[dict[str, Any]]:
    for _ in range(100):
        records = _activity_publish_records(registry)
        if len(records) >= expected_count and all(record["status"] != "running" for record in records):
            return records
        await asyncio.sleep(0)
    records = _activity_publish_records(registry)
    raise AssertionError(f"activity publish tasks did not finish: expected={expected_count}, records={records}")


def _event_types(sink: _RuntimeEventSink) -> list[str]:
    return [event_type for event_type, _payload, _source in sink.events]


def _activity_events(sink: _RuntimeEventSink) -> list[dict[str, Any]]:
    return [payload for event_type, payload, _source in sink.events if event_type == CONTEXT_COMPRESSION_WORKER_ACTIVITY]


def _assert_snapshot_has_no_sensitive_text(test_case: unittest.TestCase, registry: RuntimeTaskRegistry) -> None:
    snapshot_text = str(registry.snapshot()).lower()
    for forbidden in (
        RAW_ACTIVITY_TEXT.lower(),
        RAW_ACTIVITY_ERROR.lower(),
        WORKER_INPUT_SECRET.lower(),
        "prompt",
        "payload",
        "details",
        "error_message",
    ):
        test_case.assertNotIn(forbidden, snapshot_text)


class ContextCompressionWorkerActivityPublishTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._original_registry_accessor = worker_runner_module.get_runtime_task_registry
        self.registry = RuntimeTaskRegistry(completed_task_retention=32)
        worker_runner_module.get_runtime_task_registry = lambda: self.registry

    async def asyncTearDown(self) -> None:
        worker_runner_module.get_runtime_task_registry = self._original_registry_accessor

    async def _run_worker(
        self,
        *,
        sink: _RuntimeEventSink,
        events: list[tuple[str, dict[str, Any]]],
    ) -> CompressionJob:
        runner = _runner(sink, events)
        job = _job()
        runner._job_store.create_job(job)
        await runner.run_worker(
            job=job,
            worker_name="RequirementConstraintWorker",
            worker_role="section_worker",
            output_section="requirements_constraints",
            input_mode="native_multimodal_history",
            system_prompt="system prompt must not leak",
            worker_input=ContextCompressionWorkerInput(
                input_mode="native_multimodal_history",
                user_text=WORKER_INPUT_SECRET,
            ),
            resolved_candidates=(_resolved_model(),),
            max_rounds=1,
            communication_context=None,
            allowed_tool_names={MARK_SECTION_COMPLETE},
        )
        return job

    async def test_activity_publish_uses_registry_task_and_preserves_worker_completion(self) -> None:
        sink = _RuntimeEventSink()
        job = await self._run_worker(
            sink=sink,
            events=[
                (
                    "llm.response.started",
                    {
                        "model_alias": RAW_ACTIVITY_TEXT,
                        "api_key_label": "key-label-must-stay-out-of-registry-text",
                    },
                )
            ],
        )
        records = await _wait_for_activity_tasks(self.registry, expected_count=3)

        self.assertIn(CONTEXT_COMPRESSION_WORKER_STARTED, _event_types(sink))
        self.assertIn(CONTEXT_COMPRESSION_WORKER_PROGRESS, _event_types(sink))
        self.assertIn(CONTEXT_COMPRESSION_WORKER_COMPLETED, _event_types(sink))
        self.assertEqual(job.completed_sections, {"requirements_constraints"})
        self.assertEqual(len(_activity_events(sink)), 3)
        self.assertTrue(all(record["status"] == "completed" for record in records))
        for record in records:
            self.assertEqual(record["owner_type"], "context_compression_worker_runner")
            self.assertEqual(record["owner_id"], "RequirementConstraintWorker")
            self.assertEqual(record["task_class"], "context_compression_worker_activity_publish")
            self.assertTrue(record["task_name"].startswith("context-compression-worker-activity-publish-RequirementConstraintWorker-"))
            context = record["diagnostic_context"]
            self.assertEqual(context["event_type"], CONTEXT_COMPRESSION_WORKER_ACTIVITY)
            self.assertEqual(context["job_id"], job.job_id)
            self.assertEqual(context["agent_task_id"], job.task_id)
            self.assertEqual(context["worker_id"], "RequirementConstraintWorker")
            self.assertEqual(context["worker_name"], "RequirementConstraintWorker")
            self.assertEqual(context["worker_role"], "section_worker")
            self.assertEqual(context["output_section"], "requirements_constraints")
            self.assertEqual(context["input_mode"], "native_multimodal_history")
            self.assertIn(context["phase"], {
                "context_compression.worker_activity_publish.completed",
                "context_compression.worker_activity_publish.scheduled",
            })
            self.assertIn(context["activity_kind"], {"preparing", "llm_responding", "writing_result"})
        _assert_snapshot_has_no_sensitive_text(self, self.registry)

    async def test_activity_publish_failure_is_bounded_and_does_not_fail_worker(self) -> None:
        sink = _RuntimeEventSink(fail_activity=True)
        job = await self._run_worker(
            sink=sink,
            events=[("llm.response.started", {"error_message": RAW_ACTIVITY_TEXT})],
        )
        records = await _wait_for_activity_tasks(self.registry, expected_count=3)

        self.assertIn(CONTEXT_COMPRESSION_WORKER_COMPLETED, _event_types(sink))
        self.assertEqual(job.completed_sections, {"requirements_constraints"})
        self.assertEqual(_activity_events(sink), [])
        self.assertTrue(all(record["status"] == "completed" for record in records))
        failed_records = [
            record
            for record in records
            if record["diagnostic_context"].get("phase") == "context_compression.worker_activity_publish.failed"
        ]
        self.assertEqual(len(failed_records), 3)
        for record in failed_records:
            context = record["diagnostic_context"]
            self.assertEqual(context["reason"], "publish_failed")
            self.assertEqual(context["error_type"], "RuntimeError")
            self.assertEqual(context["event_type"], CONTEXT_COMPRESSION_WORKER_ACTIVITY)
        _assert_snapshot_has_no_sensitive_text(self, self.registry)

    async def test_activity_dedupe_remains_kind_and_text_based(self) -> None:
        sink = _RuntimeEventSink()
        await self._run_worker(
            sink=sink,
            events=[
                ("llm.response.started", {"attempt_count": 1}),
                ("llm.response.started", {"attempt_count": 2}),
                ("llm.tool_call.started", {"tool_name": RAW_ACTIVITY_TEXT}),
            ],
        )
        records = await _wait_for_activity_tasks(self.registry, expected_count=3)

        activities = _activity_events(sink)
        self.assertEqual([payload["worker"]["activity"]["kind"] for payload in activities], [
            "preparing",
            "llm_responding",
            "writing_result",
        ])
        self.assertEqual(len(records), 3)
        self.assertEqual({record["diagnostic_context"]["activity_kind"] for record in records}, {
            "preparing",
            "llm_responding",
            "writing_result",
        })
        _assert_snapshot_has_no_sensitive_text(self, self.registry)

    async def test_activity_publish_is_non_blocking_for_main_worker_events(self) -> None:
        sink = _RuntimeEventSink(fail_activity=True)
        await self._run_worker(
            sink=sink,
            events=[("llm.response.started", {"error_message": RAW_ACTIVITY_TEXT})],
        )
        await _wait_for_activity_tasks(self.registry, expected_count=3)

        event_types = _event_types(sink)
        self.assertLess(event_types.index(CONTEXT_COMPRESSION_WORKER_STARTED), event_types.index(CONTEXT_COMPRESSION_WORKER_PROGRESS))
        self.assertLess(event_types.index(CONTEXT_COMPRESSION_WORKER_PROGRESS), event_types.index(CONTEXT_COMPRESSION_WORKER_COMPLETED))
        self.assertNotIn(CONTEXT_COMPRESSION_WORKER_ACTIVITY, event_types)
        _assert_snapshot_has_no_sensitive_text(self, self.registry)


if __name__ == "__main__":
    unittest.main()

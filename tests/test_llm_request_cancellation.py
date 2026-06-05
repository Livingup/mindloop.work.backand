import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine, _AgentExecutionInterruptState
from agent_runtime.agent.agent_instance import AGENT_INSTANCE_STATUS_READY, AGENT_INSTANCE_STATUS_RUNNING, AgentInstance
from agent_runtime.agent.agent_task import AGENT_TASK_STATUS_UNFINISHED, AgentTask
from agent_runtime.agent.infrastructure.sqlite_agent_runtime_repository import SqliteAgentRuntimeRepository
from agent_runtime.agent.tool_dispatch.tool_dispatcher import ToolDispatchResult
from agent_runtime.communication.runtime_event_publisher import RuntimeCommunicationContext
from agent_runtime.conversation_message.message.models import Conversation, ToolResultPart, ToolUsePart, UniMessage
from agent_runtime.conversation_message.infrastructure.sqlite_conversation_repository import SqliteConversationRepository
from agent_runtime.llm_client.llm_protocol_clients import LlmRequestCancelled, _post_json
from agent_runtime.llm_client.llm_request_models import LlmPromptRequest
from agent_runtime.llm_client.llm_request_service import (
    LlmRequestTimeoutError,
    _await_client_request,
    _is_llm_service_call_failure,
)
from agent_runtime.tool.local_tool_definition import LocalToolDefinition
from agent_runtime.tool.local_tool_service import LocalToolService
from agent_runtime.tool.tool_execution_models import BaseToolResult


class PanicLike(BaseException):
    pass


class _ToolRepository:
    def __init__(self, fn):
        self._definition = LocalToolDefinition(
            tool_name="panic_tool",
            description="panic tool",
            fn=fn,
        )

    def get_by_name(self, tool_name: str) -> LocalToolDefinition:
        return self._definition


class _DispatchRaisesPanic:
    async def dispatch_tool_call(self, **_: Any):
        raise PanicLike("panic from dispatcher")


class _DelayedTwoToolDispatcher:
    def __init__(self) -> None:
        self.slow_started = asyncio.Event()
        self.release_slow = asyncio.Event()

    async def dispatch_tool_call(self, **kwargs: Any) -> ToolDispatchResult:
        tool_use = kwargs["tool_use"]
        if tool_use.tool_use_id == "call-slow":
            self.slow_started.set()
            await self.release_slow.wait()
            output = "slow"
        else:
            output = "fast"
        return ToolDispatchResult(
            tool_name=tool_use.name,
            source="test",
            result_part=ToolResultPart(
                tool_use_ref=tool_use.tool_use_id,
                is_error=False,
                output=output,
            ),
            tool_result=BaseToolResult(output=output),
        )


class _ConversationRepository:
    def __init__(self) -> None:
        self.saved = 0
        self.pending_tool_results: dict[tuple[str, int], list[ToolResultPart]] = {}

    async def save(self, conversation: Conversation) -> None:
        self.saved += 1

    async def save_pending_tool_result(
        self,
        conversation_id: str,
        assistant_message_index: int,
        result_part: ToolResultPart,
    ) -> None:
        key = (conversation_id, assistant_message_index)
        self.pending_tool_results.setdefault(key, []).append(result_part)

    async def list_pending_tool_results(
        self,
        conversation_id: str,
        assistant_message_index: int,
    ) -> list[ToolResultPart]:
        return list(self.pending_tool_results.get((conversation_id, assistant_message_index), []))

    async def clear_pending_tool_results(
        self,
        conversation_id: str,
        assistant_message_index: int,
    ) -> None:
        self.pending_tool_results.pop((conversation_id, assistant_message_index), None)


class _RuntimeGateway:
    def __init__(self, instance: AgentInstance) -> None:
        self.instance = instance
        self.interrupt_message: str | None = None

    def get_agent_instance(self, instance_id: str) -> AgentInstance:
        return self.instance

    def interrupt_agent_instance(self, instance_id: str, reason: str) -> AgentInstance:
        self.interrupt_message = reason
        self.instance = replace(
            self.instance,
            status=AGENT_INSTANCE_STATUS_READY,
            failure_message=reason,
        )
        return self.instance


async def _never_finishes() -> None:
    await asyncio.Event().wait()


class LlmRequestCancellationTests(unittest.TestCase):
    def test_registered_canceller_cancels_inner_llm_task(self) -> None:
        async def run_case() -> None:
            cancelled = False
            unregister_called = False

            def is_cancelled() -> bool:
                return cancelled

            def observe(canceller):
                nonlocal unregister_called
                canceller()

                def unregister() -> None:
                    nonlocal unregister_called
                    unregister_called = True

                return unregister

            request = LlmPromptRequest(
                conversation=object(),
                resolved_model=object(),
                cancellation_checker=is_cancelled,
                request_canceller_observer=observe,
            )
            cancelled = True
            with self.assertRaises(LlmRequestCancelled):
                await _await_client_request(_never_finishes(), request)
            self.assertTrue(unregister_called)

        asyncio.run(run_case())

    def test_llm_request_attempt_timeout_cancels_inner_task_and_is_retryable(self) -> None:
        async def run_case() -> None:
            inner_cancelled = asyncio.Event()

            async def never_completes() -> None:
                try:
                    await asyncio.Event().wait()
                finally:
                    inner_cancelled.set()

            request = LlmPromptRequest(
                conversation=object(),
                resolved_model=object(),
            )
            with self.assertRaises(LlmRequestTimeoutError) as ctx:
                await _await_client_request(
                    never_completes(),
                    request,
                    attempt_timeout_seconds=0.01,
                )

            self.assertEqual(ctx.exception.timeout_seconds, 0.01)
            self.assertTrue(_is_llm_service_call_failure(ctx.exception))
            await asyncio.wait_for(inner_cancelled.wait(), timeout=1)

        asyncio.run(run_case())

    def test_http_request_cancelled_before_response_headers(self) -> None:
        async def run_case() -> None:
            request_received = asyncio.Event()
            release_server = asyncio.Event()
            cancelled = False

            async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                try:
                    await reader.readuntil(b"\r\n\r\n")
                    request_received.set()
                    await release_server.wait()
                finally:
                    writer.close()
                    await writer.wait_closed()

            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            try:
                port = server.sockets[0].getsockname()[1]
                task = asyncio.create_task(
                    _post_json(
                        f"http://127.0.0.1:{port}/v1/chat/completions",
                        {"Content-Type": "application/json"},
                        {"model": "test", "messages": []},
                        cancellation_checker=lambda: cancelled,
                    )
                )
                await asyncio.wait_for(request_received.wait(), timeout=2)
                cancelled = True
                task.cancel()
                with self.assertRaises(LlmRequestCancelled):
                    await task
            finally:
                release_server.set()
                server.close()
                await server.wait_closed()

        asyncio.run(run_case())


class AgentExecutionFailureBoundaryTests(unittest.TestCase):
    def test_local_tool_service_converts_base_exception_to_error_result(self) -> None:
        def panic_tool():
            raise PanicLike("pywinpty panic")

        service = LocalToolService(_ToolRepository(panic_tool), schema_cache=object())
        result = asyncio.run(service.execute_tool("panic_tool", {}))

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "PanicLike")
        self.assertEqual(result.error_message, "pywinpty panic")

    def test_tool_batch_preserves_tool_result_for_base_exception(self) -> None:
        async def run_case() -> None:
            engine = object.__new__(AgentExecutionEngine)
            engine._tool_dispatcher = _DispatchRaisesPanic()
            engine._execution_progress = {}
            engine._event_publisher = None
            context = RuntimeCommunicationContext(
                stream_id="stream-1",
                metadata={"agent_instance_id": "instance-1"},
            )

            message, results = await engine._execute_tool_uses(
                object(),
                [ToolUsePart(name="panic_tool", input={}, tool_use_id="call-1")],
                context,
                interrupt_state=_AgentExecutionInterruptState(),
            )

            self.assertEqual(message.role, "tool")
            self.assertEqual(len(message.content), 1)
            part = message.content[0]
            self.assertIsInstance(part, ToolResultPart)
            self.assertEqual(part.tool_use_ref, "call-1")
            self.assertTrue(part.is_error)
            self.assertEqual(results[0].error_code, "PanicLike")

        asyncio.run(run_case())

    def test_tool_batch_persists_each_result_before_whole_batch_finishes(self) -> None:
        async def run_case() -> None:
            engine = object.__new__(AgentExecutionEngine)
            dispatcher = _DelayedTwoToolDispatcher()
            repository = _ConversationRepository()
            engine._tool_dispatcher = dispatcher
            engine._conversation_repository = repository
            engine._execution_progress = {}
            engine._event_publisher = None
            context = RuntimeCommunicationContext(
                stream_id="stream-1",
                metadata={"agent_instance_id": "instance-1"},
            )

            batch_task = asyncio.create_task(
                engine._execute_tool_uses(
                    object(),
                    [
                        ToolUsePart(name="fast_tool", input={}, tool_use_id="call-fast"),
                        ToolUsePart(name="slow_tool", input={}, tool_use_id="call-slow"),
                    ],
                    context,
                    conversation_id="conversation-1",
                    assistant_message_index=7,
                    interrupt_state=_AgentExecutionInterruptState(),
                )
            )
            await asyncio.wait_for(dispatcher.slow_started.wait(), timeout=2)
            for _ in range(20):
                pending = await repository.list_pending_tool_results("conversation-1", 7)
                if pending:
                    break
                await asyncio.sleep(0.01)

            self.assertFalse(batch_task.done())
            self.assertEqual([part.tool_use_ref for part in pending], ["call-fast"])
            dispatcher.release_slow.set()
            message, _results = await asyncio.wait_for(batch_task, timeout=2)
            self.assertEqual(message.role, "tool")
            self.assertEqual(
                [part.tool_use_ref for part in message.content],
                ["call-fast", "call-slow"],
            )

        asyncio.run(run_case())

    def test_repair_unmatched_tool_results_appends_error_tool_message(self) -> None:
        async def run_case() -> None:
            engine = object.__new__(AgentExecutionEngine)
            repository = _ConversationRepository()
            engine._conversation_repository = repository
            conversation = Conversation(id="conversation-1")
            conversation.append_message(
                UniMessage(
                    role="assistant",
                    content=[ToolUsePart(name="terminal_create", input={}, tool_use_id="call-1")],
                )
            )

            repaired = await engine._repair_unmatched_tool_results(
                conversation,
                reason="previous_execution_interrupted",
            )

            self.assertTrue(repaired)
            self.assertEqual(repository.saved, 1)
            self.assertEqual(len(conversation.messages), 2)
            tool_message = conversation.messages[-1]
            self.assertEqual(tool_message.role, "tool")
            part = tool_message.content[0]
            self.assertIsInstance(part, ToolResultPart)
            self.assertEqual(part.tool_use_ref, "call-1")
            self.assertTrue(part.is_error)

        asyncio.run(run_case())

    def test_repair_unmatched_tool_results_recovers_persisted_results(self) -> None:
        async def run_case() -> None:
            engine = object.__new__(AgentExecutionEngine)
            repository = _ConversationRepository()
            engine._conversation_repository = repository
            conversation = Conversation(id="conversation-1")
            conversation.append_message(
                UniMessage(
                    role="assistant",
                    content=[
                        ToolUsePart(name="terminal_create", input={}, tool_use_id="call-1"),
                        ToolUsePart(name="terminal_write", input={}, tool_use_id="call-2"),
                        ToolUsePart(name="terminal_read", input={}, tool_use_id="call-3"),
                    ],
                )
            )
            await repository.save_pending_tool_result(
                conversation.id,
                0,
                ToolResultPart(tool_use_ref="call-1", is_error=False, output="created"),
            )
            await repository.save_pending_tool_result(
                conversation.id,
                0,
                ToolResultPart(tool_use_ref="call-2", is_error=False, output="written"),
            )

            repaired = await engine._repair_unmatched_tool_results(
                conversation,
                reason="previous_execution_interrupted",
            )

            self.assertTrue(repaired)
            tool_message = conversation.messages[-1]
            parts = tool_message.content
            self.assertEqual([part.tool_use_ref for part in parts], ["call-1", "call-2", "call-3"])
            self.assertEqual(parts[0].output, "created")
            self.assertEqual(parts[1].output, "written")
            self.assertFalse(parts[0].is_error)
            self.assertFalse(parts[1].is_error)
            self.assertTrue(parts[2].is_error)
            self.assertIn("interrupted", parts[2].output[0]["text"])
            self.assertEqual(repository.pending_tool_results, {})

        asyncio.run(run_case())

    def test_background_task_base_exception_recovers_running_instance(self) -> None:
        async def run_case() -> None:
            engine = object.__new__(AgentExecutionEngine)
            instance = AgentInstance(
                instance_id="instance-1",
                task_id="task-1",
                thread_id="thread-1",
                current_working_directory="D:/project/agent",
                agent_definition_id="coding",
                llm_provider_id="provider-1",
                llm_model_binding_id="binding-1",
                conversation_id="conversation-1",
                trace_id="trace-1",
                status=AGENT_INSTANCE_STATUS_RUNNING,
            )
            runtime_gateway = _RuntimeGateway(instance)
            engine._runtime_gateway = runtime_gateway
            engine._background_tasks = set()
            engine._active_instance_ids = {instance.instance_id}
            engine._active_execution_handles = {}
            engine._execution_progress = {}

            async def boom():
                raise PanicLike("background panic")

            task = asyncio.create_task(boom())
            try:
                await task
            except PanicLike:
                pass

            engine._on_background_task_done(task, instance.instance_id)

            self.assertEqual(runtime_gateway.instance.status, AGENT_INSTANCE_STATUS_READY)
            self.assertEqual(runtime_gateway.interrupt_message, "background panic")

        asyncio.run(run_case())

    def test_reset_runtime_state_preserves_unfinished_task_and_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SqliteAgentRuntimeRepository(Path(tmp))
            try:
                task = AgentTask(
                    task_id="task-1",
                    root_agent_instance_id="instance-1",
                    agent_instance_ids=("instance-1",),
                    scene_id="scene-1",
                )
                instance = AgentInstance(
                    instance_id="instance-1",
                    task_id="task-1",
                    thread_id="thread-1",
                    current_working_directory="D:/project/agent",
                    agent_definition_id="coding",
                    llm_provider_id="provider-1",
                    llm_model_binding_id="binding-1",
                    conversation_id="conversation-1",
                    trace_id="trace-1",
                    status=AGENT_INSTANCE_STATUS_RUNNING,
                )
                repository.save_task(task)
                repository.save_agent_instance(instance)

                reset_count = repository.reset_runtime_state()

                recovered_task = repository.get_task("task-1")
                recovered_instance = repository.get_agent_instance("instance-1")
                self.assertGreater(reset_count, 0)
                self.assertIsNotNone(recovered_task)
                self.assertIsNotNone(recovered_instance)
                self.assertEqual(recovered_task.status, AGENT_TASK_STATUS_UNFINISHED)
                self.assertIsNone(recovered_task.failed_at)
                self.assertIsNone(recovered_task.failure_message)
                self.assertEqual(recovered_instance.status, AGENT_INSTANCE_STATUS_READY)
                self.assertEqual(recovered_instance.conversation_id, "conversation-1")
            finally:
                repository.close()

    def test_sqlite_conversation_pending_tool_results_and_atomic_save_round_trip(self) -> None:
        async def run_case() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                repository = SqliteConversationRepository(Path(tmp))
                try:
                    conversation = Conversation(id="conversation-1")
                    conversation.append_message(UniMessage(role="user", content=[]))
                    await repository.create(
                        conversation,
                        task_id="task-1",
                        thread_id="thread-1",
                        agent_id="agent-1",
                        agent_instance_id="instance-1",
                    )
                    conversation.append_message(UniMessage(role="assistant", content=[]))
                    await repository.save(conversation)
                    loaded = await repository.get("conversation-1")
                    self.assertEqual(len(loaded.messages), 2)

                    await repository.save_pending_tool_result(
                        "conversation-1",
                        1,
                        ToolResultPart(tool_use_ref="call-1", is_error=False, output={"ok": True}),
                    )
                    pending = await repository.list_pending_tool_results("conversation-1", 1)
                    self.assertEqual(len(pending), 1)
                    self.assertEqual(pending[0].tool_use_ref, "call-1")
                    self.assertEqual(pending[0].output, [{"kind": "text", "text": '{\n  "ok": true\n}'}])
                    await repository.clear_pending_tool_results("conversation-1", 1)
                    self.assertEqual(await repository.list_pending_tool_results("conversation-1", 1), [])
                finally:
                    repository.close()

        asyncio.run(run_case())


if __name__ == "__main__":
    unittest.main()

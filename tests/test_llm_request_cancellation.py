import asyncio
import unittest
from dataclasses import replace
from typing import Any

from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine, _AgentExecutionInterruptState
from agent_runtime.agent.agent_instance import AGENT_INSTANCE_STATUS_READY, AGENT_INSTANCE_STATUS_RUNNING, AgentInstance
from agent_runtime.communication.runtime_event_publisher import RuntimeCommunicationContext
from agent_runtime.conversation_message.message.models import Conversation, ToolResultPart, ToolUsePart, UniMessage
from agent_runtime.llm_client.llm_protocol_clients import LlmRequestCancelled, _post_json
from agent_runtime.llm_client.llm_request_models import LlmPromptRequest
from agent_runtime.llm_client.llm_request_service import _await_client_request
from agent_runtime.tool.local_tool_definition import LocalToolDefinition
from agent_runtime.tool.local_tool_service import LocalToolService


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


class _ConversationRepository:
    def __init__(self) -> None:
        self.saved = 0

    async def save(self, conversation: Conversation) -> None:
        self.saved += 1


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


if __name__ == "__main__":
    unittest.main()

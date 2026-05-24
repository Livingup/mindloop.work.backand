import asyncio
import unittest

from agent_runtime.llm_client.llm_protocol_clients import LlmRequestCancelled, _post_json
from agent_runtime.llm_client.llm_request_models import LlmPromptRequest
from agent_runtime.llm_client.llm_request_service import _await_client_request


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


if __name__ == "__main__":
    unittest.main()

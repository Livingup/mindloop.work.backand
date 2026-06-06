import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_runtime.context_compression.archive_repository import ContextCompressionArchiveRepository
from agent_runtime.context_compression.models import CompressionJob
from agent_runtime.conversation_message.infrastructure.sqlite_conversation_repository import SqliteConversationRepository
from agent_runtime.conversation_message.message.models import Conversation, ToolResultPart, UniMessage


class SqliteThreadedRepositoryTests(unittest.TestCase):
    def test_conversation_repository_can_be_used_from_execution_thread(self) -> None:
        async def exercise_repository(repository: SqliteConversationRepository) -> None:
            conversation = Conversation(id="conversation-threaded")
            conversation.append_message(UniMessage(role="user", content=[]))
            await repository.create(
                conversation,
                task_id="task-threaded",
                thread_id="thread-1",
                agent_id="agent-1",
                agent_instance_id="instance-1",
            )
            conversation.append_message(UniMessage(role="assistant", content=[]))
            await repository.save(conversation)
            await repository.append_message(
                "conversation-threaded",
                UniMessage(role="tool", content=[]),
            )
            await repository.save_pending_tool_result(
                "conversation-threaded",
                1,
                ToolResultPart(
                    tool_use_ref="call-threaded",
                    is_error=False,
                    output={"ok": True},
                ),
            )
            pending = await repository.list_pending_tool_results("conversation-threaded", 1)
            loaded = await repository.get("conversation-threaded")
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].tool_use_ref, "call-threaded")
            self.assertEqual(len(loaded.messages), 3)

        with tempfile.TemporaryDirectory() as tmp:
            repository = SqliteConversationRepository(Path(tmp))
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(lambda: asyncio.run(exercise_repository(repository)))
                    future.result(timeout=5)
            finally:
                repository.close()

    def test_context_compression_archive_repository_can_be_used_from_execution_thread(self) -> None:
        def exercise_repository(repository: ContextCompressionArchiveRepository) -> None:
            compression_index = repository.next_compression_index(
                task_id="task-threaded",
                agent_instance_id="instance-1",
                root_conversation_id="conversation-root",
            )
            self.assertEqual(compression_index, 1)
            repository.upsert_job(
                CompressionJob(
                    job_id="job-threaded",
                    task_id="task-threaded",
                    thread_id="thread-1",
                    agent_id="agent-1",
                    agent_instance_id="instance-1",
                    root_conversation_id="conversation-root",
                    source_conversation_id="conversation-source",
                    target_conversation_id="conversation-target",
                    compression_index=compression_index,
                    status="completed",
                    final_markdown_chunks=["# Summary"],
                )
            )
            self.assertEqual(
                repository.root_conversation_id_for(
                    agent_instance_id="instance-1",
                    source_conversation_id="conversation-target",
                ),
                "conversation-root",
            )
            self.assertEqual(
                repository.next_compression_index(
                    task_id="task-threaded",
                    agent_instance_id="instance-1",
                    root_conversation_id="conversation-root",
                ),
                2,
            )

        with tempfile.TemporaryDirectory() as tmp:
            repository = ContextCompressionArchiveRepository(Path(tmp))
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(lambda: exercise_repository(repository))
                    future.result(timeout=5)
            finally:
                repository.close()


if __name__ == "__main__":
    unittest.main()

import unittest

from agent_runtime.context_compression.event_publisher import (
    CONTEXT_COMPRESSION_EVENT_TYPES,
    CONTEXT_COMPRESSION_WORKER_CANCELLED,
)
from agent_runtime.context_compression.job_store import CompressionJobClosed, InMemoryCompressionJobStore
from agent_runtime.context_compression.models import CompressionJob


def _job() -> CompressionJob:
    return CompressionJob(
        job_id="job-1",
        task_id="task-1",
        thread_id="thread-1",
        agent_id="agent-1",
        agent_instance_id="instance-1",
        root_conversation_id="root-1",
        source_conversation_id="conversation-1",
        compression_index=1,
    )


class ContextCompressionClosedLoopTests(unittest.TestCase):
    def test_terminal_job_rejects_late_section_writes(self) -> None:
        store = InMemoryCompressionJobStore()
        job = store.create_job(_job())

        store.update_status(job.job_id, "failed")

        with self.assertRaises(CompressionJobClosed):
            store.append_section_items(
                job.job_id,
                worker_name="CodeArchitectureWorker",
                section="code_architecture",
                expected_section="code_architecture",
                items=[{"content": "late item"}],
            )
        with self.assertRaises(CompressionJobClosed):
            store.mark_section_complete(job.job_id, "code_architecture", expected_section="code_architecture")

    def test_terminal_job_rejects_state_regression(self) -> None:
        for status in ["failed", "cancelled", "completed", "skipped"]:
            with self.subTest(status=status):
                store = InMemoryCompressionJobStore()
                job = store.create_job(_job())

                store.update_status(job.job_id, status)

                with self.assertRaises(CompressionJobClosed):
                    store.update_status(job.job_id, "assembled")

    def test_section_worker_can_only_write_expected_section(self) -> None:
        store = InMemoryCompressionJobStore()
        job = store.create_job(_job())
        store.update_status(job.job_id, "running")

        with self.assertRaises(ValueError):
            store.append_section_items(
                job.job_id,
                worker_name="CodeArchitectureWorker",
                section="progress_plan",
                expected_section="code_architecture",
                items=[{"content": "wrong section"}],
            )
        with self.assertRaises(ValueError):
            store.mark_section_complete(job.job_id, "progress_plan", expected_section="code_architecture")

    def test_worker_cancelled_event_is_registered(self) -> None:
        self.assertIn(CONTEXT_COMPRESSION_WORKER_CANCELLED, CONTEXT_COMPRESSION_EVENT_TYPES)

if __name__ == "__main__":
    unittest.main()


import unittest

from agent_runtime.context_compression.north_gateway.context_compression_rest_adapter import (
    _worker_content_responses,
)


class ContextCompressionWorkerContentsTests(unittest.TestCase):
    def test_worker_content_responses_expose_only_readable_content_for_section_workers(self) -> None:
        responses = _worker_content_responses(
            [
                {
                    "section": "requirements_constraints",
                    "content": "需求 A",
                    "priority": "P0",
                    "status": "active",
                    "resource_references": [{"uri": "file:///secret"}],
                },
                {
                    "section": "requirements_constraints",
                    "content": "需求 A",
                },
                {
                    "section": "progress_plan",
                    "worker_name": "ProgressPlanWorker",
                    "content": "计划 B",
                },
                {
                    "section": "final_markdown",
                    "worker_name": "FinalAssemblerWorker",
                    "content": "不应暴露的最终汇编内容",
                },
            ],
        )

        by_worker = {response.worker_name: response for response in responses}
        self.assertEqual(len(responses), 6)
        self.assertEqual(by_worker["RequirementConstraintWorker"].content_markdown, "需求 A")
        self.assertEqual(by_worker["ProgressPlanWorker"].content_markdown, "计划 B")
        self.assertNotIn("FinalAssemblerWorker", by_worker)
        self.assertNotIn("priority", by_worker["RequirementConstraintWorker"].content_markdown)
        self.assertNotIn("resource_references", by_worker["RequirementConstraintWorker"].content_markdown)


if __name__ == "__main__":
    unittest.main()

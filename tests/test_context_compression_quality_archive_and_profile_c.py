from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_runtime.context_compression.archive_repository import (
    ContextCompressionArchiveRepository,
    build_quality_analysis_text,
)
from agent_runtime.context_compression.default_prompts import get_default_worker_prompt
from agent_runtime.context_compression.history_markdown import native_history_to_markdown
from agent_runtime.context_compression.models import CompressionJob, CompressionRange
from agent_runtime.context_compression.profiles import (
    PROFILE_SINGLE_AGENT_GOAL_V1,
    get_profile,
    list_profiles,
)
from agent_runtime.context_compression.compression_service import _native_history_instruction
from agent_runtime.conversation_message.message.models import TextPart, UniMessage


class ContextCompressionQualityArchiveAndProfileCTests(unittest.TestCase):
    def test_profile_c_registered_as_single_serial_worker(self) -> None:
        profile_ids = [profile.profile_id for profile in list_profiles()]
        self.assertIn(PROFILE_SINGLE_AGENT_GOAL_V1, profile_ids)
        profile = get_profile(PROFILE_SINGLE_AGENT_GOAL_V1)
        self.assertEqual(len(profile.stages), 1)
        self.assertEqual(profile.stages[0].mode, "serial")
        self.assertEqual(profile.stages[0].worker_names, ("SingleAgentCompressorWorker",))
        self.assertEqual(profile.assembly.strategy, "ordered_section_concat")
        self.assertEqual(profile.assembly.section_order, ("goal_compressed_context",))
        self.assertIsNone(profile.verifier.worker_name)

    def test_profile_c_prompt_and_shared_instruction_require_multi_append(self) -> None:
        prompt = get_default_worker_prompt(
            profile_id=PROFILE_SINGLE_AGENT_GOAL_V1,
            worker_name="SingleAgentCompressorWorker",
        )
        self.assertIn("多次调用以增量追加", prompt)
        self.assertIn("不要强行一次就返回完整的压缩结果", prompt)
        instruction = _native_history_instruction(worker_title="目标导向单 Agent 压缩")
        self.assertIn("多次调用以增量追加", instruction)
        self.assertIn("不要强行一次就返回完整的压缩结果", instruction)

    def test_archive_only_persists_native_history_and_final_markdown(self) -> None:
        messages = [
            UniMessage(role="user", content=[TextPart(text="请继续完成任务")]),
            UniMessage(role="assistant", content=[TextPart(text="收到，继续执行")]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            repo = ContextCompressionArchiveRepository(Path(tmp))
            try:
                job = CompressionJob(
                    job_id="ccjob-quality-1",
                    task_id="task-1",
                    thread_id="thread-1",
                    agent_id="agent-1",
                    agent_instance_id="instance-1",
                    root_conversation_id="root-1",
                    source_conversation_id="source-1",
                    compression_index=1,
                    status="running",
                    range=CompressionRange(
                        compressed_start_index=0,
                        compressed_end_index=1,
                        retained_tail_start_index=2,
                        retained_tail_end_index=2,
                        retained_tail_anchor="tail",
                        reason="test",
                        compression_index=1,
                    ),
                )
                repo.upsert_job(job, native_history_messages=messages)
                job.final_markdown_chunks = ["## 压缩后的任务上下文\n\n保留有效信息"]
                job.status = "completed"
                repo.upsert_job(job)

                archive = repo.get_quality_archive(job.job_id)
                self.assertEqual(archive.job_id, job.job_id)
                self.assertEqual(len(archive.native_history), 2)
                self.assertIn("请继续完成任务", archive.native_history_markdown)
                self.assertIn("保留有效信息", archive.final_markdown)
                self.assertIn("## 下面是目标业务agent的历史对话：", archive.quality_analysis_text)
                self.assertIn("## 下面是我的上下文压缩系统整理压缩后的完整上下文内容：", archive.quality_analysis_text)
                self.assertIn("不要矫枉过正", archive.quality_analysis_text)

                # 不写过程型 before/after 快照目录内容
                snapshots_dir = Path(tmp) / "snapshots"
                if snapshots_dir.exists():
                    self.assertEqual(list(snapshots_dir.iterdir()), [])
                histories = list((Path(tmp) / "histories").glob("*.jsonl"))
                summaries = list((Path(tmp) / "summaries").glob("*.md"))
                self.assertEqual(len(histories), 1)
                self.assertEqual(len(summaries), 1)
            finally:
                repo.close()

    def test_native_history_markdown_is_deterministic(self) -> None:
        messages = [
            UniMessage(role="user", content=[TextPart(text="A")]),
            UniMessage(role="assistant", content=[TextPart(text="B")]),
        ]
        first = native_history_to_markdown(messages)
        second = native_history_to_markdown(messages)
        self.assertEqual(first, second)
        self.assertIn("### [0] user", first)
        self.assertIn("A", first)
        assembled = build_quality_analysis_text(
            native_history_markdown=first,
            final_markdown="## 结果\n\nok",
        )
        self.assertTrue(assembled.startswith("## 下面是目标业务agent的历史对话："))



if __name__ == "__main__":
    unittest.main()

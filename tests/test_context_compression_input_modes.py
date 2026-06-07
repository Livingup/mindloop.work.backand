import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_runtime.context_compression.compression_service import (
    ContextCompressionService,
    _assembler_input,
    _required_native_input_modalities,
    _select_worker_input_mode_and_candidates,
)
from agent_runtime.context_compression.conversation_rewriter import rewrite_conversation_with_summary
from agent_runtime.context_compression.default_prompts import get_default_worker_prompt
from agent_runtime.context_compression.models import CompressionJob, CompressionRange, SectionItem
from agent_runtime.context_compression.settings_repository import ContextCompressionSettingsRepository
from agent_runtime.context_compression.worker_input import ContextCompressionWorkerInput
from agent_runtime.context_compression.worker_runner import _worker_render_options
from agent_runtime.conversation_message.message.models import (
    PROVIDER_OPENAI,
    ImagePart,
    PdfPart,
    PdfPartMeta,
    ROLE_ASSISTANT,
    ROLE_TOOL,
    TextPart,
    TextPartMeta,
    ToolResultPart,
    UniMessage,
)
from agent_runtime.conversation_message.message.operations import create_conversation
from agent_runtime.conversation_message.message.tool_result_output import tool_result_output_to_text
from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import ResolvedResource
from agent_runtime.input_modality import INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF, INPUT_MODALITY_TEXT
from agent_runtime.llm_model_registry.llm_model_registry_service import ResolvedLlmProviderModel
from agent_runtime.llm_model_registry.llm_provider_definition import LlmApiKey, LlmRetryPolicy


def _job(input_mode: str = "transcript_snapshot") -> CompressionJob:
    return CompressionJob(
        job_id="job-1",
        task_id="task-1",
        thread_id="thread-1",
        agent_id="agent-1",
        agent_instance_id="instance-1",
        root_conversation_id="root-1",
        source_conversation_id="conversation-1",
        compression_index=3,
        input_mode=input_mode,
    )


def _range_plan(start: int = 0, end: int = 1) -> CompressionRange:
    return CompressionRange(
        compressed_start_index=start,
        compressed_end_index=end,
        retained_tail_start_index=end + 1,
        retained_tail_end_index=end + 1,
        retained_tail_anchor="latest_assistant_message",
        reason="unit_test",
        compression_index=3,
    )


def _candidate(
    candidate_id: str,
    *,
    modalities: tuple[str, ...] = (INPUT_MODALITY_TEXT,),
    supports_tool_calling: bool = True,
) -> ResolvedLlmProviderModel:
    return ResolvedLlmProviderModel(
        provider_id=f"provider-{candidate_id}",
        binding_id=f"binding-{candidate_id}",
        model_id=f"model-{candidate_id}",
        model_alias=f"alias-{candidate_id}",
        api_format="openai_responses",
        sdk_platform="openai",
        api_base_url="https://example.invalid",
        api_keys=(LlmApiKey(key="test-key", name="test"),),
        custom_headers={},
        retry=LlmRetryPolicy(),
        tool_choice=None,
        thinking=None,
        custom_request_params=None,
        context_window=100000,
        max_output_tokens=4096,
        compression_threshold=None,
        supports_tool_calling=supports_tool_calling,
        supports_structured_output=True,
        supported_input_modalities=modalities,
    )


class _ResourceResolver:
    def __init__(self, resources: dict[str, ResolvedResource] | None = None) -> None:
        self._resources = resources or {}

    async def get_base64(self, resource_id: str) -> str:
        return self._resources[resource_id].base64_content or ""

    async def get_public_url(self, resource_id: str) -> str | None:
        return self._resources[resource_id].object_storage_url

    async def get_resource_meta(self, resource_id: str):
        resource = self._resources[resource_id]
        from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import ResourceMeta

        return ResourceMeta(
            mime_type=resource.mime_type,
            resource_category=resource.resource_category,
            size_in_bytes=resource.size_in_bytes,
            image_width=resource.image_width,
            image_height=resource.image_height,
            pdf_page_count=resource.pdf_page_count,
        )

    async def resolve_resource(self, resource_id: str) -> ResolvedResource:
        if resource_id not in self._resources:
            raise KeyError(resource_id)
        return self._resources[resource_id]


class ContextCompressionRuntimeSettingsTests(unittest.TestCase):
    def test_settings_repository_persists_and_clears_runtime_input_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = ContextCompressionSettingsRepository(lambda: temp_dir)

            self.assertIsNone(repo.get_settings().input_mode)

            saved = repo.set_settings(
                llm_policy_group_id=" group-a ",
                input_mode="native_multimodal_history",
            )
            self.assertEqual(saved.llm_policy_group_id, "group-a")
            self.assertEqual(saved.input_mode, "native_multimodal_history")

            reloaded = ContextCompressionSettingsRepository(lambda: temp_dir).get_settings()
            self.assertEqual(reloaded.llm_policy_group_id, "group-a")
            self.assertEqual(reloaded.input_mode, "native_multimodal_history")

            cleared = repo.set_settings(llm_policy_group_id="group-a", input_mode="  ")
            self.assertEqual(cleared.llm_policy_group_id, "group-a")
            self.assertIsNone(cleared.input_mode)

    def test_settings_repository_migrates_legacy_schema_without_input_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "context_compression_worker_prompts.db"
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    "CREATE TABLE context_compression_settings ("
                    " id INTEGER PRIMARY KEY CHECK (id = 1),"
                    " llm_policy_group_id TEXT,"
                    " updated_at TEXT NOT NULL"
                    ")"
                )
                conn.execute(
                    "INSERT INTO context_compression_settings (id, llm_policy_group_id, updated_at) "
                    "VALUES (1, 'legacy-policy', '2026-01-01T00:00:00+00:00')"
                )
                conn.commit()
            finally:
                conn.close()

            repo = ContextCompressionSettingsRepository(lambda: temp_dir)
            settings = repo.get_settings()

            self.assertEqual(settings.llm_policy_group_id, "legacy-policy")
            self.assertIsNone(settings.input_mode)

            conn = sqlite3.connect(str(db_path))
            try:
                migrated_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(context_compression_settings)"
                    ).fetchall()
                }
            finally:
                conn.close()
            self.assertIn("input_mode", migrated_columns)

    def test_settings_repository_rejects_unsupported_input_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = ContextCompressionSettingsRepository(lambda: temp_dir)

            with self.assertRaises(ValueError):
                repo.set_settings(llm_policy_group_id=None, input_mode="transcript_only")


class ContextCompressionWorkerInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_history_worker_input_replays_compressed_range_and_adds_instruction(self) -> None:
        conversation = create_conversation()
        conversation.add_user_message([TextPart("old question")], [TextPartMeta()])
        conversation.append_message(UniMessage(role=ROLE_ASSISTANT, content=[TextPart("old answer")], source_provider=PROVIDER_OPENAI))
        conversation.add_user_message([TextPart("retained tail")], [TextPartMeta()])
        conversation.provider_state[PROVIDER_OPENAI] = {"prompt_cache_key": "source-cache-key"}
        job = _job(input_mode="native_multimodal_history")
        range_plan = _range_plan(start=0, end=1)
        service = ContextCompressionService.__new__(ContextCompressionService)
        service._resource_resolver = _ResourceResolver()

        worker_input = await service._build_worker_input(
            conversation,
            job,
            range_plan,
            "native_multimodal_history",
        )

        self.assertEqual(worker_input.input_mode, "native_multimodal_history")
        self.assertTrue(worker_input.uses_history_messages)
        self.assertEqual(worker_input.history_messages, tuple(conversation.messages[:2]))
        self.assertFalse(hasattr(worker_input, "openai_responses_prompt_cache_key"))
        self.assertNotIn("source-cache-key", worker_input.user_text)
        self.assertIn("只用于上下文压缩分析", worker_input.user_text)
        self.assertIn("compressed_message_range: 0..1", worker_input.user_text)
        self.assertNotIn("retained tail", worker_input.user_text)

    def test_worker_render_options_does_not_override_prompt_cache_key(self) -> None:
        worker_input = ContextCompressionWorkerInput(
            input_mode="native_multimodal_history",
            user_text="compress",
        )

        self.assertEqual(_worker_render_options(_candidate("openai"), worker_input), {})

    async def test_transcript_worker_input_preserves_multimodal_resource_locator_text(self) -> None:
        conversation = create_conversation()
        conversation.add_user_message([TextPart("before")], [TextPartMeta()])
        conversation.add_user_message([ImagePart("image-1")], [TextPartMeta()])
        job = _job(input_mode="transcript_snapshot")
        range_plan = _range_plan(start=0, end=1)
        service = ContextCompressionService.__new__(ContextCompressionService)
        service._resource_resolver = _ResourceResolver(
            {
                "image-1": ResolvedResource(
                    resource_id="image-1",
                    mime_type="image/png",
                    resource_category="image",
                    object_storage_url="https://cdn.example.invalid/image-1.png",
                    base64_content=None,
                    local_storage_path="C:/local/image-1.png",
                ),
            }
        )

        worker_input = await service._build_worker_input(
            conversation,
            job,
            range_plan,
            "transcript_snapshot",
        )

        self.assertEqual(worker_input.input_mode, "transcript_snapshot")
        self.assertFalse(worker_input.uses_history_messages)
        self.assertIn("[image file] resource_id=image-1 locator=https://cdn.example.invalid/image-1.png", worker_input.user_text)


class ContextCompressionInputModeSelectionTests(unittest.TestCase):
    def test_native_history_without_multimodal_requirements_keeps_all_candidates(self) -> None:
        conversation = create_conversation()
        conversation.add_user_message([TextPart("text only")], [TextPartMeta()])
        candidates = (_candidate("text"), _candidate("image", modalities=(INPUT_MODALITY_TEXT, INPUT_MODALITY_IMAGE)))

        input_mode, selected = _select_worker_input_mode_and_candidates(
            requested_input_mode="native_multimodal_history",
            source_conversation=conversation,
            range_plan=_range_plan(start=0, end=0),
            worker_candidates=candidates,
        )

        self.assertEqual(input_mode, "native_multimodal_history")
        self.assertEqual(selected, candidates)

    def test_native_history_filters_to_candidates_supporting_image_requirements(self) -> None:
        conversation = create_conversation()
        conversation.add_user_message([ImagePart("image-1")], [TextPartMeta()])
        text_candidate = _candidate("text", modalities=(INPUT_MODALITY_TEXT,))
        image_candidate = _candidate("image", modalities=(INPUT_MODALITY_TEXT, INPUT_MODALITY_IMAGE))

        input_mode, selected = _select_worker_input_mode_and_candidates(
            requested_input_mode="native_multimodal_history",
            source_conversation=conversation,
            range_plan=_range_plan(start=0, end=0),
            worker_candidates=(text_candidate, image_candidate),
        )

        self.assertEqual(input_mode, "native_multimodal_history")
        self.assertEqual(selected, (image_candidate,))

    def test_native_history_downgrades_when_pdf_requirement_has_no_compatible_candidate(self) -> None:
        conversation = create_conversation()
        conversation.add_user_message([PdfPart("pdf-1")], [PdfPartMeta(page_count=2)])
        candidates = (_candidate("text", modalities=(INPUT_MODALITY_TEXT,)),)

        input_mode, selected = _select_worker_input_mode_and_candidates(
            requested_input_mode="native_multimodal_history",
            source_conversation=conversation,
            range_plan=_range_plan(start=0, end=0),
            worker_candidates=candidates,
        )

        self.assertEqual(input_mode, "transcript_snapshot")
        self.assertEqual(selected, candidates)

    def test_tool_result_media_contributes_to_native_required_modalities(self) -> None:
        message = UniMessage(
            role=ROLE_TOOL,
            content=[
                ToolResultPart(
                    tool_use_ref="tool-1",
                    is_error=False,
                    output=[
                        {"kind": "image", "resource_id": "image-1"},
                        {"kind": "pdf", "resource_id": "pdf-1"},
                    ],
                )
            ],
        )

        self.assertEqual(
            _required_native_input_modalities((message,)),
            {INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF},
        )


class ToolResultTranscriptFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_result_output_to_text_preserves_resource_id_and_locator_states(self) -> None:
        resolver = _ResourceResolver(
            {
                "image-1": ResolvedResource(
                    resource_id="image-1",
                    mime_type="image/png",
                    resource_category="image",
                    object_storage_url="https://cdn.example.invalid/image-1.png",
                    base64_content=None,
                    local_storage_path="C:/local/image-1.png",
                ),
                "pdf-1": ResolvedResource(
                    resource_id="pdf-1",
                    mime_type="application/pdf",
                    resource_category="pdf",
                    object_storage_url=None,
                    base64_content=None,
                    local_storage_path="C:/local/doc.pdf",
                ),
                "image-2": ResolvedResource(
                    resource_id="image-2",
                    mime_type="image/png",
                    resource_category="image",
                    object_storage_url=None,
                    base64_content=None,
                    local_storage_path=None,
                ),
            }
        )

        text = await tool_result_output_to_text(
            [
                {"kind": "text", "text": "plain evidence"},
                {"kind": "image", "resource_id": "image-1"},
                {"kind": "pdf", "resource_id": "pdf-1"},
                {"kind": "image", "resource_id": "missing-image"},
                {"kind": "pdf"},
                {"kind": "image", "resource_id": "image-2"},
            ],
            resolver,
        )

        self.assertIn("plain evidence", text)
        self.assertIn("[image file] resource_id=image-1 locator=https://cdn.example.invalid/image-1.png", text)
        self.assertIn("[pdf file] resource_id=pdf-1 locator=C:/local/doc.pdf", text)
        self.assertIn("[image file] resource_id=missing-image unresolved", text)
        self.assertIn("[pdf file] resource_id=<missing>", text)
        self.assertIn("[image file] resource_id=image-2 resolved_without_url_or_path", text)


class ContextCompressionVisibilityContractTests(unittest.TestCase):
    def test_assembler_input_uses_business_materials_without_worker_or_job_metadata(self) -> None:
        job = _job()
        job.section_items.append(
            SectionItem(
                section="requirements_constraints",
                content="用户要求压缩摘要不要暴露内部 worker 信息。",
                priority="P0",
                status="active",
                source_message_ranges=[[0, 3]],
                resource_references=[{"absolute_path": "D:/repo/src/app.py", "line_ranges": [[10, 20]]}],
                worker_name="RequirementConstraintWorker",
            )
        )
        job.section_items.append(
            SectionItem(
                section="progress_plan",
                content="当前任务已定位到汇编输入契约问题。",
                priority="P1",
                status="active",
                source_message_ranges=[[4, 6]],
                worker_name="ProgressPlanWorker",
            )
        )

        assembler_input = _assembler_input(job)

        self.assertIn("用户需求与硬约束", assembler_input)
        self.assertIn("任务计划与执行进度", assembler_input)
        self.assertIn("用户要求压缩摘要不要暴露内部 worker 信息。", assembler_input)
        self.assertIn("可复查资源定位", assembler_input)
        self.assertNotIn("job-1", assembler_input)
        self.assertNotIn("root-1", assembler_input)
        self.assertNotIn("conversation-1", assembler_input)
        self.assertNotIn("compression_index", assembler_input)
        self.assertNotIn("worker:", assembler_input)
        self.assertNotIn("RequirementConstraintWorker", assembler_input)
        self.assertNotIn("ProgressPlanWorker", assembler_input)
        self.assertNotIn("source_message_ranges", assembler_input)

    def test_rewritten_summary_visible_markdown_hides_runtime_metadata_but_keeps_provider_extras(self) -> None:
        source_conversation = create_conversation()
        source_conversation.add_user_message([TextPart("compressed history")], [TextPartMeta()])
        source_conversation.add_user_message([TextPart("retained tail")], [TextPartMeta()])
        job = _job()
        range_plan = _range_plan()

        target_conversation, summary_message_id = rewrite_conversation_with_summary(
            source_conversation=source_conversation,
            job=job,
            range_plan=range_plan,
            final_markdown="## Current Objective\n继续定位上下文压缩摘要可见性问题。",
        )

        summary_message = target_conversation.messages[0]
        summary_text = summary_message.content[0].text
        self.assertEqual(summary_message_id, "summary-job-1")
        self.assertIn("# Runtime Compressed Task Context", summary_text)
        self.assertIn("## Usage Contract", summary_text)
        self.assertIn("## Current Objective", summary_text)
        self.assertNotIn("Runtime Compression Metadata", summary_text)
        self.assertNotIn("job-1", summary_text)
        self.assertNotIn("root-1", summary_text)
        self.assertNotIn("conversation-1", summary_text)
        self.assertNotIn("compressed_message_range", summary_text)
        self.assertNotIn("retained_tail_range", summary_text)

        context_compression_extras = summary_message.provider_extras["context_compression"]
        self.assertEqual(context_compression_extras["job_id"], "job-1")
        self.assertEqual(context_compression_extras["root_conversation_id"], "root-1")
        self.assertEqual(context_compression_extras["source_conversation_id"], "conversation-1")
        self.assertEqual(context_compression_extras["compressed_range"], {"start_index": 0, "end_index": 1})
        self.assertEqual(context_compression_extras["retained_tail_range"]["start_index"], 2)

    def test_final_assembler_default_prompts_use_business_visible_contract(self) -> None:
        for input_mode in ["transcript_snapshot", "native_multimodal_history"]:
            with self.subTest(input_mode=input_mode):
                prompt = get_default_worker_prompt(input_mode=input_mode, worker_name="FinalAssemblerWorker")

                self.assertIn("业务维度", prompt)
                self.assertIn("最终 Markdown 的读者是继续执行任务的业务 Agent", prompt)
                self.assertIn("禁止在最终 Markdown 中输出压缩执行角色名称", prompt)
                self.assertNotIn("Section Worker", prompt)
                self.assertNotIn("FinalAssemblerWorker", prompt)
                self.assertNotIn("Provenance", prompt)
                self.assertNotIn("Usage Contract", prompt)


if __name__ == "__main__":
    unittest.main()

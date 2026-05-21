from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from agent_runtime.communication.runtime_event_publisher import RuntimeCommunicationContext, RuntimeEventPublisher
from agent_runtime.context_compression.archive_repository import ContextCompressionArchiveRepository
from agent_runtime.context_compression.config import ContextCompressionConfig
from agent_runtime.context_compression.settings_repository import ContextCompressionSettingsRepository
from agent_runtime.context_compression.conversation_rewriter import rewrite_conversation_with_summary
from agent_runtime.context_compression.event_publisher import (
    CONTEXT_COMPRESSION_ASSEMBLY_COMPLETED,
    CONTEXT_COMPRESSION_ASSEMBLY_STARTED,
    CONTEXT_COMPRESSION_CANCELLED,
    CONTEXT_COMPRESSION_COMPLETED,
    CONTEXT_COMPRESSION_CONVERSATION_REBIND_COMPLETED,
    CONTEXT_COMPRESSION_FAILED,
    CONTEXT_COMPRESSION_RANGE_PLANNED,
    CONTEXT_COMPRESSION_SKIPPED,
    CONTEXT_COMPRESSION_STARTED,
    CONTEXT_COMPRESSION_VERIFICATION_COMPLETED,
    CONTEXT_COMPRESSION_VERIFICATION_STARTED,
    ContextCompressionEventPublisher,
)
from agent_runtime.context_compression.gate import decide_context_compression
from agent_runtime.context_compression.job_store import InMemoryCompressionJobStore
from agent_runtime.context_compression.models import CompressionJob, CompressionResult, SECTION_WORKERS
from agent_runtime.context_compression.prompt_loader import ContextCompressionPromptLoader
from agent_runtime.context_compression.prompt_repository import ContextCompressionWorkerPromptRepository
from agent_runtime.context_compression.range_planner import plan_compression_range
from agent_runtime.context_compression.summary_tools import (
    APPEND_FINAL_MARKDOWN_SECTION,
    APPEND_SECTION_ITEMS,
    MARK_FINAL_COMPLETE,
    MARK_SECTION_COMPLETE,
    REPORT_SECTION_ISSUE,
)
from agent_runtime.context_compression.transcript_snapshot import build_transcript_snapshot
from agent_runtime.context_compression.worker_runner import ContextCompressionWorkerRunner
from agent_runtime.conversation_message.conversation_repository import ConversationRepository
from agent_runtime.conversation_message.message.models import Conversation
from agent_runtime.llm_client.llm_protocol_clients import LlmRequestCancelled
from agent_runtime.llm_client.llm_request_service import LlmRequestService
from agent_runtime.llm_model_registry.llm_model_registry_service import ResolvedLlmProviderModel
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import LlmModelRegistryGateway

if TYPE_CHECKING:
    from agent_runtime.agent.agent_instance import AgentInstance
    from agent_runtime.agent.agent_task import AgentTask
    from agent_runtime.agent.north_gateway.agent_runtime_contracts import AgentRuntimeGateway


class ContextCompressionService:
    def __init__(
        self,
        *,
        llm_request_service: LlmRequestService,
        conversation_repository: ConversationRepository,
        runtime_gateway: AgentRuntimeGateway,
        llm_registry_gateway: LlmModelRegistryGateway,
        archive_repository: ContextCompressionArchiveRepository,
        event_publisher: RuntimeEventPublisher,
        config_accessor: Callable[[], ContextCompressionConfig],
        settings_repository: ContextCompressionSettingsRepository,
        prompt_repository: ContextCompressionWorkerPromptRepository | None = None,
    ) -> None:
        self._conversation_repository = conversation_repository
        self._runtime_gateway = runtime_gateway
        self._llm_registry_gateway = llm_registry_gateway
        self._archive_repository = archive_repository
        self._config_accessor = config_accessor
        self._settings_repository = settings_repository
        self._event_publisher = ContextCompressionEventPublisher(event_publisher)
        self._job_store = InMemoryCompressionJobStore()
        self._prompt_loader = ContextCompressionPromptLoader(
            prompt_repository=prompt_repository,
        )
        self._worker_runner = ContextCompressionWorkerRunner(
            llm_request_service=llm_request_service,
            job_store=self._job_store,
            event_publisher=self._event_publisher,
        )

    async def compress_if_needed(
        self,
        *,
        task: AgentTask,
        instance: AgentInstance,
        source_conversation: Conversation,
        trigger_model: ResolvedLlmProviderModel,
        communication_context: RuntimeCommunicationContext | None,
        interrupt_checker: Callable[[], bool] | None = None,
    ) -> CompressionResult:
        config = self._config_accessor()
        decision = decide_context_compression(source_conversation, trigger_model, config)
        if not decision.should_compress:
            return CompressionResult(created=False, reason=decision.reason)
        try:
            worker_candidates = self._resolve_worker_candidates(config)
        except (KeyError, ValueError) as exc:
            await self._publish_skipped(
                task=task,
                instance=instance,
                source_conversation=source_conversation,
                communication_context=communication_context,
                reason="context_compression_worker_model_unavailable",
                error_type=exc.__class__.__name__,
                error_message=str(exc),
            )
            return CompressionResult(created=False, reason="context_compression_worker_model_unavailable")
        if not any(candidate.supports_tool_calling for candidate in worker_candidates):
            await self._publish_skipped(
                task=task,
                instance=instance,
                source_conversation=source_conversation,
                communication_context=communication_context,
                reason="context_compression_worker_model_unavailable",
            )
            return CompressionResult(created=False, reason="context_compression_worker_model_unavailable")
        if interrupt_checker is not None and interrupt_checker():
            return CompressionResult(created=False, reason="interrupted")
        root_conversation_id = self._archive_repository.root_conversation_id_for(
            agent_instance_id=instance.instance_id,
            source_conversation_id=source_conversation.id,
        )
        compression_index = self._archive_repository.next_compression_index(
            task_id=task.task_id,
            agent_instance_id=instance.instance_id,
            root_conversation_id=root_conversation_id,
        )
        job = CompressionJob(
            job_id=f"ccjob-{uuid4().hex}",
            task_id=task.task_id,
            thread_id=instance.thread_id,
            agent_id=instance.agent_definition_id,
            agent_instance_id=instance.instance_id,
            root_conversation_id=root_conversation_id,
            source_conversation_id=source_conversation.id,
            compression_index=compression_index,
            input_mode=config.input_mode,
        )
        self._job_store.create_job(job)
        await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_STARTED, _base_payload(job, status="running", phase="started"))
        try:
            range_plan = plan_compression_range(
                source_conversation,
                compression_index=compression_index,
                reason=decision.reason,
            )
            if range_plan is None:
                job.status = "skipped"
                await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_SKIPPED, _base_payload(job, status="skipped", phase="range_planning", reason="insufficient_compressible_range"))
                return CompressionResult(created=False, job=job, reason="insufficient_compressible_range")
            job.range = range_plan
            self._archive_repository.upsert_job(job, source_conversation=source_conversation)
            await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_RANGE_PLANNED, _base_payload(job, status="running", phase="range_planned"))
            input_text = build_transcript_snapshot(source_conversation, job, range_plan)
            await self._run_section_workers(
                job=job,
                input_text=input_text,
                input_mode=config.input_mode,
                resolved_candidates=worker_candidates,
                max_rounds=config.max_worker_rounds,
                communication_context=communication_context,
                interrupt_checker=interrupt_checker,
            )
            if not job.section_items:
                raise RuntimeError("上下文压缩 Section Worker 未写入任何结构化条目。")
            await self._assemble_final_markdown(
                job=job,
                input_mode=config.input_mode,
                resolved_candidates=worker_candidates,
                max_rounds=config.max_worker_rounds,
                communication_context=communication_context,
                interrupt_checker=interrupt_checker,
            )
            final_markdown = job.final_markdown
            if not final_markdown:
                raise RuntimeError("上下文压缩最终 Markdown 为空。")
            if config.verifier_enabled:
                await self._verify_final_markdown(
                    job=job,
                    input_mode=config.input_mode,
                    resolved_candidates=worker_candidates,
                    max_rounds=config.max_worker_rounds,
                    communication_context=communication_context,
                    interrupt_checker=interrupt_checker,
                )
            target_conversation, summary_message_id = rewrite_conversation_with_summary(
                source_conversation=source_conversation,
                job=job,
                range_plan=range_plan,
                final_markdown=final_markdown,
            )
            await self._conversation_repository.create(
                target_conversation,
                task_id=task.task_id,
                thread_id=instance.thread_id,
                agent_id=instance.agent_definition_id,
                agent_instance_id=instance.instance_id,
            )
            self._runtime_gateway.switch_agent_instance_conversation(instance.instance_id, target_conversation.id)
            self._job_store.set_target_conversation(job.job_id, target_conversation_id=target_conversation.id, summary_message_id=summary_message_id)
            self._job_store.update_status(job.job_id, "completed")
            self._archive_repository.upsert_job(job, source_conversation=source_conversation, target_conversation=target_conversation)
            await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_CONVERSATION_REBIND_COMPLETED, _completion_payload(job, source_conversation, target_conversation, summary_message_id))
            await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_COMPLETED, _completion_payload(job, source_conversation, target_conversation, summary_message_id))
            return CompressionResult(created=True, target_conversation=target_conversation, job=job)
        except LlmRequestCancelled:
            job.status = "cancelled"
            self._archive_repository.upsert_job(job, source_conversation=source_conversation)
            await self._event_publisher.publish(
                communication_context,
                CONTEXT_COMPRESSION_CANCELLED,
                _base_payload(job, status="cancelled", phase="cancelled", reason="interrupted"),
            )
            return CompressionResult(created=False, reason="interrupted")
        except asyncio.CancelledError:
            job.status = "cancelled"
            self._archive_repository.upsert_job(job, source_conversation=source_conversation)
            await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_CANCELLED, _base_payload(job, status="cancelled", phase="cancelled"))
            raise
        except Exception as exc:
            job.status = "failed"
            self._archive_repository.upsert_job(job, source_conversation=source_conversation)
            payload = _base_payload(job, status="failed", phase="failed")
            payload["error_type"] = exc.__class__.__name__
            payload["error_message"] = str(exc)
            await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_FAILED, payload)
            raise

    async def _run_section_workers(
        self,
        *,
        job: CompressionJob,
        input_text: str,
        input_mode: str,
        resolved_candidates: tuple[ResolvedLlmProviderModel, ...],
        max_rounds: int,
        communication_context: RuntimeCommunicationContext | None,
        interrupt_checker: Callable[[], bool] | None,
    ) -> None:
        for worker_name, section in SECTION_WORKERS:
            prompt = self._prompt_loader.load_prompt(input_mode=input_mode, worker_name=worker_name)
            await self._worker_runner.run_worker(
                job=job,
                worker_name=worker_name,
                worker_role="section_worker",
                output_section=section,
                input_mode=input_mode,
                system_prompt=prompt,
                input_text=input_text,
                resolved_candidates=resolved_candidates,
                max_rounds=max_rounds,
                communication_context=communication_context,
                allowed_tool_names={
                    APPEND_SECTION_ITEMS,
                    MARK_SECTION_COMPLETE,
                    REPORT_SECTION_ISSUE,
                },
                interrupt_checker=interrupt_checker,
            )

    async def _assemble_final_markdown(
        self,
        *,
        job: CompressionJob,
        input_mode: str,
        resolved_candidates: tuple[ResolvedLlmProviderModel, ...],
        max_rounds: int,
        communication_context: RuntimeCommunicationContext | None,
        interrupt_checker: Callable[[], bool] | None,
    ) -> None:
        await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_ASSEMBLY_STARTED, _base_payload(job, status="running", phase="assembly"))
        prompt = self._prompt_loader.load_prompt(input_mode=input_mode, worker_name="FinalAssemblerWorker")
        input_text = _assembler_input(job)
        await self._worker_runner.run_worker(
            job=job,
            worker_name="FinalAssemblerWorker",
            worker_role="final_assembler",
            output_section=None,
            input_mode=input_mode,
            system_prompt=prompt,
            input_text=input_text,
            resolved_candidates=resolved_candidates,
            max_rounds=max_rounds,
            communication_context=communication_context,
            allowed_tool_names={
                APPEND_FINAL_MARKDOWN_SECTION,
                MARK_FINAL_COMPLETE,
                REPORT_SECTION_ISSUE,
            },
            interrupt_checker=interrupt_checker,
        )
        await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_ASSEMBLY_COMPLETED, _base_payload(job, status="running", phase="assembly_completed"))

    async def _verify_final_markdown(
        self,
        *,
        job: CompressionJob,
        input_mode: str,
        resolved_candidates: tuple[ResolvedLlmProviderModel, ...],
        max_rounds: int,
        communication_context: RuntimeCommunicationContext | None,
        interrupt_checker: Callable[[], bool] | None,
    ) -> None:
        await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_VERIFICATION_STARTED, _base_payload(job, status="running", phase="verification"))
        prompt = self._prompt_loader.load_prompt(input_mode=input_mode, worker_name="FinalVerifierWorker")
        await self._worker_runner.run_worker(
            job=job,
            worker_name="FinalVerifierWorker",
            worker_role="final_verifier",
            output_section=None,
            input_mode=input_mode,
            system_prompt=prompt,
            input_text=_verifier_input(job),
            resolved_candidates=resolved_candidates,
            max_rounds=max_rounds,
            communication_context=communication_context,
            allowed_tool_names={REPORT_SECTION_ISSUE},
            interrupt_checker=interrupt_checker,
        )
        await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_VERIFICATION_COMPLETED, _base_payload(job, status="running", phase="verification_completed"))

    def _resolve_worker_candidates(self, config: ContextCompressionConfig) -> tuple[ResolvedLlmProviderModel, ...]:
        del config  # 预留参数，未来可能从 config 读取退让策略
        policy_group_id = self._settings_repository.get_llm_policy_group_id()
        if not policy_group_id:
            raise ValueError(
                "上下文压缩未配置 LLM 执行策略组，请到「上下文压缩」设置页选择策略组。"
            )
        policy = self._llm_registry_gateway.resolve_execution_policy(policy_group_id)
        candidates = tuple(candidate.resolved_model for candidate in policy.candidates)
        if not candidates:
            raise ValueError(
                f"上下文压缩绑定的 LLM 执行策略组没有可用候选项: {policy_group_id}"
            )
        return candidates

    async def _publish_skipped(
        self,
        *,
        task: AgentTask,
        instance: AgentInstance,
        source_conversation: Conversation,
        communication_context: RuntimeCommunicationContext | None,
        reason: str,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        payload = {
            "task_id": task.task_id,
            "agent_instance_id": instance.instance_id,
            "source_conversation_id": source_conversation.id,
            "reason": reason,
            "status": "skipped",
        }
        if error_type is not None:
            payload["error_type"] = error_type
        if error_message is not None:
            payload["error_message"] = error_message
        await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_SKIPPED, payload)


def _assembler_input(job: CompressionJob) -> str:
    lines = [
        "# Context Compression Section Items",
        "",
        f"job_id: {job.job_id}",
        f"root_conversation_id: {job.root_conversation_id}",
        f"source_conversation_id: {job.source_conversation_id}",
        f"compression_index: {job.compression_index}",
        "",
    ]
    for item in job.section_items:
        lines.extend([
            f"## {item.section}",
            f"- priority: {item.priority}",
            f"- status: {item.status}",
            f"- worker: {item.worker_name or ''}",
            f"- source_message_ranges: {item.source_message_ranges}",
            f"- resource_references: {item.resource_references}",
            item.content,
            "",
        ])
    return "\n".join(lines)


def _verifier_input(job: CompressionJob) -> str:
    return "\n".join([
        "# Context Compression Final Verification Input",
        "",
        f"job_id: {job.job_id}",
        f"root_conversation_id: {job.root_conversation_id}",
        f"source_conversation_id: {job.source_conversation_id}",
        f"compression_index: {job.compression_index}",
        "",
        "## Final Markdown",
        job.final_markdown,
        "",
        "## Existing Issues",
        str(job.issues),
    ])


def _base_payload(job: CompressionJob, *, status: str, phase: str, reason: str | None = None) -> dict[str, Any]:
    range_plan = job.range
    return {
        "job_id": job.job_id,
        "task_id": job.task_id,
        "thread_id": job.thread_id,
        "agent_instance_id": job.agent_instance_id,
        "root_conversation_id": job.root_conversation_id,
        "source_conversation_id": job.source_conversation_id,
        "target_conversation_id": job.target_conversation_id,
        "compression_index": job.compression_index,
        "phase": phase,
        "status": status,
        "reason": reason or (range_plan.reason if range_plan else None),
        "compressed_range": range_plan.compressed_range_payload() if range_plan else None,
        "retained_tail_range": range_plan.retained_tail_range_payload() if range_plan else None,
        "metrics": {
            "section_item_count": len(job.section_items),
            "completed_section_count": len(job.completed_sections),
            "final_markdown_chunks": len(job.final_markdown_chunks),
        },
    }


def _completion_payload(
    job: CompressionJob,
    source_conversation: Conversation,
    target_conversation: Conversation,
    summary_message_id: str,
) -> dict[str, Any]:
    payload = _base_payload(job, status="completed", phase="completed")
    range_plan = job.range
    payload.update({
        "target_conversation_id": target_conversation.id,
        "source_message_count": len(source_conversation.messages),
        "target_message_count": len(target_conversation.messages),
        "compressed_message_count": range_plan.compressed_message_count if range_plan else None,
        "retained_tail_message_count": range_plan.retained_tail_message_count if range_plan else None,
        "summary_message_id": summary_message_id,
        "summary_message_index": 0,
        "archive_job_id": job.job_id,
        "final_markdown_available": True,
    })
    return payload

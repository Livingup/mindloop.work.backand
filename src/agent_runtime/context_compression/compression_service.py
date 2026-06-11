from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
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
    CONTEXT_COMPRESSION_SECTION_COMPLETED,
    CONTEXT_COMPRESSION_SECTION_STARTED,
    CONTEXT_COMPRESSION_SKIPPED,
    CONTEXT_COMPRESSION_STARTED,
    CONTEXT_COMPRESSION_VERIFICATION_COMPLETED,
    CONTEXT_COMPRESSION_VERIFICATION_STARTED,
    CONTEXT_COMPRESSION_WORKER_CANCELLED,
    CONTEXT_COMPRESSION_WORKER_QUEUED,
    ContextCompressionEventPublisher,
)
from agent_runtime.context_compression.gate import decide_context_compression
from agent_runtime.context_compression.job_store import InMemoryCompressionJobStore
from agent_runtime.context_compression.models import (
    CompressionJob,
    CompressionRange,
    CompressionResult,
    SECTION_WORKERS,
    SECTION_WORKER_TITLES,
    context_compression_worker_id,
    context_compression_worker_title,
)
from agent_runtime.context_compression.prompt_loader import ContextCompressionPromptLoader
from agent_runtime.context_compression.prompt_repository import ContextCompressionWorkerPromptRepository
from agent_runtime.context_compression.range_planner import plan_compression_range
from agent_runtime.context_compression.summary_tools import (
    APPEND_FINAL_MARKDOWN_SECTION,
    APPEND_SECTION_ITEMS,
    MARK_FINAL_COMPLETE,
    MARK_SECTION_COMPLETE,
    READ_DRAFT,
    REPLACE_DRAFT_LINES,
    REPORT_SECTION_ISSUE,
)
from agent_runtime.context_compression.transcript_snapshot import build_transcript_snapshot
from agent_runtime.context_compression.worker_input import ContextCompressionWorkerInput
from agent_runtime.context_compression.worker_runner import ContextCompressionWorkerRunner
from agent_runtime.conversation_message.conversation_repository import ConversationRepository
from agent_runtime.conversation_message.message.models import Conversation, ImagePart, PdfPart, ToolResultPart, UniMessage
from agent_runtime.conversation_message.message.tool_result_output import normalize_tool_result_output
from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import ResourceResolver
from agent_runtime.input_modality import INPUT_MODALITY_IMAGE, INPUT_MODALITY_PDF
from agent_runtime.llm_client.llm_protocol_clients import LlmRequestCancelled
from agent_runtime.llm_client.llm_request_service import LlmRequestService
from agent_runtime.llm_model_registry.llm_model_registry_service import ResolvedLlmProviderModel
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import LlmModelRegistryGateway

if TYPE_CHECKING:
    from agent_runtime.agent.agent_instance import AgentInstance
    from agent_runtime.agent.agent_task import AgentTask
    from agent_runtime.agent.north_gateway.agent_runtime_contracts import AgentRuntimeGateway


@dataclass(frozen=True, slots=True)
class _SectionWorkerRun:
    worker_name: str
    section: str
    system_prompt: str


class ContextCompressionService:
    def __init__(
        self,
        *,
        llm_request_service: LlmRequestService,
        conversation_repository: ConversationRepository,
        runtime_gateway: AgentRuntimeGateway,
        llm_registry_gateway: LlmModelRegistryGateway,
        resource_resolver: ResourceResolver,
        archive_repository: ContextCompressionArchiveRepository,
        event_publisher: RuntimeEventPublisher,
        config_accessor: Callable[[], ContextCompressionConfig],
        settings_repository: ContextCompressionSettingsRepository,
        prompt_repository: ContextCompressionWorkerPromptRepository | None = None,
    ) -> None:
        self._conversation_repository = conversation_repository
        self._runtime_gateway = runtime_gateway
        self._llm_registry_gateway = llm_registry_gateway
        self._resource_resolver = resource_resolver
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
        request_canceller_observer: Callable[[Callable[[], None]], Callable[[], None] | None] | None = None,
    ) -> CompressionResult:
        static_config = self._config_accessor()
        runtime_settings = self._settings_repository.get_settings()
        config = replace(static_config, input_mode=runtime_settings.input_mode or static_config.input_mode)
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
        self._job_store.update_status(job.job_id, "running")
        await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_STARTED, _base_payload(job, status="running", phase="started"))
        try:
            range_plan = plan_compression_range(
                source_conversation,
                compression_index=compression_index,
                reason=decision.reason,
            )
            if range_plan is None:
                self._job_store.update_status(job.job_id, "skipped")
                await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_SKIPPED, _base_payload(job, status="skipped", phase="range_planning", reason="insufficient_compressible_range"))
                return CompressionResult(created=False, job=job, reason="insufficient_compressible_range")
            job.range = range_plan
            effective_input_mode, worker_candidates = _select_worker_input_mode_and_candidates(
                requested_input_mode=config.input_mode,
                source_conversation=source_conversation,
                range_plan=range_plan,
                worker_candidates=worker_candidates,
            )
            job.input_mode = effective_input_mode
            self._archive_repository.upsert_job(job, source_conversation=source_conversation)
            await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_RANGE_PLANNED, _base_payload(job, status="running", phase="range_planned"))
            worker_input = await self._build_worker_input(source_conversation, job, range_plan, effective_input_mode)
            await self._run_section_workers(
                job=job,
                worker_input=worker_input,
                input_mode=effective_input_mode,
                resolved_candidates=worker_candidates,
                max_rounds=config.max_worker_rounds,
                communication_context=communication_context,
                interrupt_checker=interrupt_checker,
                request_canceller_observer=request_canceller_observer,
            )
            if not job.section_items:
                raise RuntimeError("上下文压缩 Section Worker 未写入任何结构化条目。")
            await self._assemble_final_markdown(
                job=job,
                input_mode=effective_input_mode,
                resolved_candidates=worker_candidates,
                max_rounds=config.max_worker_rounds,
                communication_context=communication_context,
                interrupt_checker=interrupt_checker,
                request_canceller_observer=request_canceller_observer,
            )
            if not job.final_markdown:
                raise RuntimeError("上下文压缩最终 Markdown 为空。")
            if config.verifier_enabled:
                await self._verify_final_markdown(
                    source_conversation=source_conversation,
                    range_plan=range_plan,
                    job=job,
                    input_mode=effective_input_mode,
                    resolved_candidates=worker_candidates,
                    max_rounds=config.max_worker_rounds,
                    communication_context=communication_context,
                    interrupt_checker=interrupt_checker,
                    request_canceller_observer=request_canceller_observer,
                )
            final_markdown = job.final_markdown
            if not final_markdown:
                raise RuntimeError("上下文压缩最终 Markdown 为空。")
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
            self._job_store.update_status(job.job_id, "cancelled")
            self._archive_repository.upsert_job(job, source_conversation=source_conversation)
            await self._event_publisher.publish(
                communication_context,
                CONTEXT_COMPRESSION_CANCELLED,
                _base_payload(job, status="cancelled", phase="cancelled", reason="interrupted"),
            )
            return CompressionResult(created=False, reason="interrupted")
        except asyncio.CancelledError:
            self._job_store.update_status(job.job_id, "cancelled")
            self._archive_repository.upsert_job(job, source_conversation=source_conversation)
            await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_CANCELLED, _base_payload(job, status="cancelled", phase="cancelled"))
            raise
        except Exception as exc:
            self._job_store.update_status(job.job_id, "failed")
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
        worker_input: ContextCompressionWorkerInput,
        input_mode: str,
        resolved_candidates: tuple[ResolvedLlmProviderModel, ...],
        max_rounds: int,
        communication_context: RuntimeCommunicationContext | None,
        interrupt_checker: Callable[[], bool] | None,
        request_canceller_observer: Callable[[Callable[[], None]], Callable[[], None] | None] | None,
    ) -> None:
        workers = tuple(
            _SectionWorkerRun(
                worker_name=worker_name,
                section=section,
                system_prompt=self._prompt_loader.load_prompt(input_mode=input_mode, worker_name=worker_name),
            )
            for worker_name, section in SECTION_WORKERS
        )
        for worker in workers:
            await self._publish_section_worker_lifecycle_event(
                communication_context,
                CONTEXT_COMPRESSION_WORKER_QUEUED,
                job,
                worker=worker,
                input_mode=input_mode,
                reason="waiting_for_concurrent_start",
            )
        tasks = {
            asyncio.create_task(
                self._run_one_section_worker(
                    job=job,
                    worker=worker,
                    worker_input=worker_input,
                    input_mode=input_mode,
                    resolved_candidates=resolved_candidates,
                    max_rounds=max_rounds,
                    communication_context=communication_context,
                    interrupt_checker=interrupt_checker,
                    request_canceller_observer=request_canceller_observer,
                )
            ): worker
            for worker in workers
        }
        pending = set(tasks)
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_EXCEPTION)
                failure_error: BaseException | None = None
                cancellation_error: BaseException | None = None
                for task in done:
                    try:
                        await task
                    except LlmRequestCancelled as exc:
                        cancellation_error = cancellation_error or exc
                    except asyncio.CancelledError as exc:
                        cancellation_error = cancellation_error or exc
                    except Exception as exc:
                        failure_error = failure_error or exc
                first_error = failure_error or cancellation_error
                if first_error is not None:
                    self._job_store.update_status(job.job_id, "failed" if failure_error is not None else "cancelled")
                    await self._cancel_pending_section_workers(
                        job=job,
                        pending_tasks=pending,
                        task_workers=tasks,
                        input_mode=input_mode,
                        communication_context=communication_context,
                        reason="sibling_worker_failed" if job.status == "failed" else "context_compression_cancelled",
                    )
                    pending = set()
                    if failure_error is None and isinstance(first_error, asyncio.CancelledError):
                        raise first_error
                    raise first_error
        except asyncio.CancelledError:
            self._job_store.update_status(job.job_id, "cancelled")
            await self._cancel_pending_section_workers(
                job=job,
                pending_tasks=pending,
                task_workers=tasks,
                input_mode=input_mode,
                communication_context=communication_context,
                reason="context_compression_cancelled",
            )
            raise
        finally:
            remaining = [task for task in tasks if not task.done()]
            if remaining:
                for task in remaining:
                    task.cancel()
                await asyncio.gather(*remaining, return_exceptions=True)

    async def _run_one_section_worker(
        self,
        *,
        job: CompressionJob,
        worker: _SectionWorkerRun,
        worker_input: ContextCompressionWorkerInput,
        input_mode: str,
        resolved_candidates: tuple[ResolvedLlmProviderModel, ...],
        max_rounds: int,
        communication_context: RuntimeCommunicationContext | None,
        interrupt_checker: Callable[[], bool] | None,
        request_canceller_observer: Callable[[Callable[[], None]], Callable[[], None] | None] | None,
    ) -> None:
        try:
            await self._publish_section_worker_lifecycle_event(
                communication_context,
                CONTEXT_COMPRESSION_SECTION_STARTED,
                job,
                worker=worker,
                input_mode=input_mode,
            )
            await self._worker_runner.run_worker(
                job=job,
                worker_name=worker.worker_name,
                worker_role="section_worker",
                output_section=worker.section,
                input_mode=input_mode,
                system_prompt=worker.system_prompt,
                worker_input=worker_input,
                resolved_candidates=resolved_candidates,
                max_rounds=max_rounds,
                communication_context=communication_context,
                allowed_tool_names={
                    APPEND_SECTION_ITEMS,
                    MARK_SECTION_COMPLETE,
                    REPORT_SECTION_ISSUE,
                },
                interrupt_checker=interrupt_checker,
                request_canceller_observer=request_canceller_observer,
            )
            await self._publish_section_worker_lifecycle_event(
                communication_context,
                CONTEXT_COMPRESSION_SECTION_COMPLETED,
                job,
                worker=worker,
                input_mode=input_mode,
            )
        except LlmRequestCancelled:
            await self._publish_section_worker_lifecycle_event(
                communication_context,
                CONTEXT_COMPRESSION_WORKER_CANCELLED,
                job,
                worker=worker,
                input_mode=input_mode,
                reason="interrupted",
            )
            raise

    async def _cancel_pending_section_workers(
        self,
        *,
        job: CompressionJob,
        pending_tasks: set[asyncio.Task[None]],
        task_workers: dict[asyncio.Task[None], _SectionWorkerRun],
        input_mode: str,
        communication_context: RuntimeCommunicationContext | None,
        reason: str,
    ) -> None:
        for task in pending_tasks:
            worker = task_workers[task]
            await self._publish_section_worker_lifecycle_event(
                communication_context,
                CONTEXT_COMPRESSION_WORKER_CANCELLED,
                job,
                worker=worker,
                input_mode=input_mode,
                reason=reason,
            )
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

    async def _publish_section_worker_lifecycle_event(
        self,
        communication_context: RuntimeCommunicationContext | None,
        event_type: str,
        job: CompressionJob,
        *,
        worker: _SectionWorkerRun,
        input_mode: str,
        reason: str | None = None,
    ) -> None:
        payload = _base_payload(job, status=job.status, phase="workers")
        worker_id = context_compression_worker_id(worker.worker_name, "section_worker", worker.section)
        payload["worker"] = {
            "worker_id": worker_id,
            "id": worker_id,
            "name": worker.worker_name,
            "title": context_compression_worker_title(worker.worker_name, "section_worker", worker.section),
            "role": "section_worker",
            "output_section": worker.section,
            "input_mode": input_mode,
        }
        if reason is not None:
            payload["worker"]["reason"] = reason
        await self._event_publisher.publish(
            communication_context,
            event_type,
            payload,
            source="context_compression_service",
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
        request_canceller_observer: Callable[[Callable[[], None]], Callable[[], None] | None] | None,
    ) -> None:
        del input_mode, resolved_candidates, max_rounds, interrupt_checker, request_canceller_observer
        await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_ASSEMBLY_STARTED, _base_payload(job, status="running", phase="assembly"))
        self._job_store.set_draft_markdown(job.job_id, _render_draft_markdown(job))
        await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_ASSEMBLY_COMPLETED, _base_payload(job, status="running", phase="assembly_completed"))

    async def _verify_final_markdown(
        self,
        *,
        source_conversation: Conversation,
        range_plan: CompressionRange,
        job: CompressionJob,
        input_mode: str,
        resolved_candidates: tuple[ResolvedLlmProviderModel, ...],
        max_rounds: int,
        communication_context: RuntimeCommunicationContext | None,
        interrupt_checker: Callable[[], bool] | None,
        request_canceller_observer: Callable[[Callable[[], None]], Callable[[], None] | None] | None,
    ) -> None:
        await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_VERIFICATION_STARTED, _base_payload(job, status="running", phase="verification"))
        prompt = self._prompt_loader.load_prompt(input_mode=input_mode, worker_name="FinalVerifierWorker")
        worker_input = await self._build_final_review_worker_input(source_conversation, job, range_plan, input_mode)
        await self._worker_runner.run_worker(
            job=job,
            worker_name="FinalVerifierWorker",
            worker_role="final_verifier",
            output_section=None,
            input_mode=input_mode,
            system_prompt=prompt,
            worker_input=worker_input,
            resolved_candidates=resolved_candidates,
            max_rounds=max_rounds,
            communication_context=communication_context,
            allowed_tool_names={READ_DRAFT, REPLACE_DRAFT_LINES, MARK_FINAL_COMPLETE, REPORT_SECTION_ISSUE},
            interrupt_checker=interrupt_checker,
            request_canceller_observer=request_canceller_observer,
        )
        await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_VERIFICATION_COMPLETED, _base_payload(job, status="running", phase="verification_completed"))

    async def _build_worker_input(
        self,
        source_conversation: Conversation,
        job: CompressionJob,
        range_plan: CompressionRange,
        input_mode: str,
    ) -> ContextCompressionWorkerInput:
        if input_mode == "native_multimodal_history":
            return ContextCompressionWorkerInput(
                input_mode=input_mode,
                user_text=_native_history_instruction(job, range_plan),
                history_messages=tuple(
                    source_conversation.messages[
                        range_plan.compressed_start_index:range_plan.compressed_end_index + 1
                    ]
                ),
            )
        transcript = await build_transcript_snapshot(source_conversation, job, range_plan, self._resource_resolver)
        return ContextCompressionWorkerInput(input_mode="transcript_snapshot", user_text=transcript)

    async def _build_final_review_worker_input(
        self,
        source_conversation: Conversation,
        job: CompressionJob,
        range_plan: CompressionRange,
        input_mode: str,
    ) -> ContextCompressionWorkerInput:
        instruction = _final_review_instruction(job, range_plan)
        if input_mode == "native_multimodal_history":
            return ContextCompressionWorkerInput(
                input_mode=input_mode,
                user_text=instruction,
                history_messages=tuple(
                    source_conversation.messages[
                        range_plan.compressed_start_index:range_plan.compressed_end_index + 1
                    ]
                ),
            )
        transcript = await build_transcript_snapshot(source_conversation, job, range_plan, self._resource_resolver)
        return ContextCompressionWorkerInput(
            input_mode="transcript_snapshot",
            user_text="\n\n".join([transcript, instruction]),
        )

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


def _select_worker_input_mode_and_candidates(
    *,
    requested_input_mode: str,
    source_conversation: Conversation,
    range_plan: CompressionRange,
    worker_candidates: tuple[ResolvedLlmProviderModel, ...],
) -> tuple[str, tuple[ResolvedLlmProviderModel, ...]]:
    if requested_input_mode != "native_multimodal_history":
        return "transcript_snapshot", worker_candidates

    required_modalities = _required_native_input_modalities(_compressed_range_messages(source_conversation, range_plan))
    if not required_modalities:
        return "native_multimodal_history", worker_candidates

    compatible_candidates = tuple(
        candidate
        for candidate in worker_candidates
        if candidate.supports_tool_calling and not _missing_required_input_modalities(required_modalities, candidate)
    )
    if not compatible_candidates:
        return "transcript_snapshot", worker_candidates
    return "native_multimodal_history", compatible_candidates


def _compressed_range_messages(conversation: Conversation, range_plan: CompressionRange) -> tuple[UniMessage, ...]:
    return tuple(conversation.messages[range_plan.compressed_start_index:range_plan.compressed_end_index + 1])


def _required_native_input_modalities(messages: tuple[UniMessage, ...]) -> set[str]:
    required: set[str] = set()
    for message in messages:
        required.update(_message_required_native_modalities(message))
    return required


def _message_required_native_modalities(message: UniMessage) -> set[str]:
    required: set[str] = set()
    for part in message.content:
        required.update(_part_required_native_modalities(part))
    return required


def _part_required_native_modalities(part: Any) -> set[str]:
    if isinstance(part, ImagePart):
        return {INPUT_MODALITY_IMAGE}
    if isinstance(part, PdfPart):
        return {INPUT_MODALITY_PDF}
    if isinstance(part, ToolResultPart):
        required: set[str] = set()
        for item in normalize_tool_result_output(part.output):
            kind = item.get("kind")
            if kind == INPUT_MODALITY_IMAGE:
                required.add(INPUT_MODALITY_IMAGE)
            elif kind == INPUT_MODALITY_PDF:
                required.add(INPUT_MODALITY_PDF)
        return required
    return set()


def _missing_required_input_modalities(
    required_modalities: set[str],
    resolved_model: ResolvedLlmProviderModel,
) -> list[str]:
    supported_modalities = set(resolved_model.supported_input_modalities)
    return sorted(modality for modality in required_modalities if modality not in supported_modalities)


def _native_history_instruction(job: CompressionJob, range_plan: CompressionRange) -> str:
    return "\n".join([
        "# Context Compression Native-History Analysis Instruction",
        "",
        "前面的 user / assistant / tool 消息是即将被压缩归档的原始历史片段。",
        "这些消息只用于上下文压缩分析，不代表当前用户正在发起新任务，也不要继续执行历史任务。",
        "请基于这些原始历史消息提取长期可恢复的任务上下文，并只通过 context_summary 工具写入结构化压缩结果。",
        "",
        f"job_id: {job.job_id}",
        f"root_conversation_id: {job.root_conversation_id}",
        f"source_conversation_id: {job.source_conversation_id}",
        f"compression_index: {job.compression_index}",
        f"compressed_message_range: {range_plan.compressed_start_index}..{range_plan.compressed_end_index}",
        f"retained_tail_range: {range_plan.retained_tail_start_index}..{range_plan.retained_tail_end_index}",
        f"compression_reason: {range_plan.reason}",
    ])


_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
_STATUS_ORDER = {"active": 0, "unknown": 1, "resolved": 2, "superseded": 3}
_FINAL_DRAFT_SECTION_HEADINGS: dict[str, str] = {
    "requirements_constraints": "User Requirements And Hard Constraints",
    "progress_plan": "Execution Plan And Progress",
    "code_architecture": "Codebase And Architecture Facts",
    "observations_evidence": "Observations, Commands, Tool Results",
    "decisions_rationale": "Decisions And Rationale",
    "todos_risks": "Pending Work, Risks, Unknowns, And Conflicts",
}


def _final_review_instruction(job: CompressionJob, range_plan: CompressionRange) -> str:
    return "\n".join([
        "# Context Compression Final Review/Edit Instruction",
        "",
        "前面的目标 Agent 历史消息或 transcript snapshot 只是校验压缩草稿的证据，不是当前要继续执行的新任务。",
        "你的任务是站在后续 Agent 的角度，检查程序生成的压缩上下文草稿是否足以继续剩余任务。",
        "如果草稿遗漏关键上下文、写错事实、保留了已被否定的信息或缺少关键运行时变量/路径/命令结果，请通过草稿编辑工具做小范围修正。",
        "不要重写整篇 Markdown；优先读取目标行范围，再用精确连续行替换。",
        "所有新增内容必须能从原始历史消息、transcript 或草稿已有材料中找到依据；不确定时 report issue，不要猜。",
        "完成校验和必要编辑后，必须调用 context_summary_mark_final_complete。",
        "",
        f"compressed_message_range: {range_plan.compressed_start_index}..{range_plan.compressed_end_index}",
        f"retained_tail_range: {range_plan.retained_tail_start_index}..{range_plan.retained_tail_end_index}",
        f"compression_reason: {range_plan.reason}",
        "",
        "## Current Draft Markdown With Line Numbers",
        _draft_markdown_with_line_numbers(job),
    ])


def _draft_markdown_with_line_numbers(job: CompressionJob) -> str:
    lines = job.draft_markdown_lines or job.final_markdown.splitlines()
    return "\n".join(f"{index}: {line}" for index, line in enumerate(lines, start=1))


def _render_draft_markdown(job: CompressionJob) -> str:
    section_order = [section for _worker_name, section in SECTION_WORKERS]
    grouped_items: dict[str, list[Any]] = {section: [] for section in section_order}
    for item in job.section_items:
        grouped_items.setdefault(item.section, []).append(item)

    lines: list[str] = []
    for section in section_order:
        items = grouped_items.get(section) or []
        if not items:
            continue
        lines.extend([f"## {_FINAL_DRAFT_SECTION_HEADINGS.get(section, SECTION_WORKER_TITLES.get(section, section))}", ""])
        for item in sorted(items, key=_section_item_sort_key):
            lines.extend(_section_item_lines(item))
        lines.append("")
    resource_lines = _resource_reference_lines(job)
    if resource_lines:
        lines.extend(["## Persistent Resource References", "", *resource_lines])
    return "\n".join(lines).strip()


def _section_item_sort_key(item: Any) -> tuple[int, int, str]:
    return (
        _PRIORITY_ORDER.get(str(item.priority), 99),
        _STATUS_ORDER.get(str(item.status), 99),
        str(item.content),
    )


def _section_item_lines(item: Any) -> list[str]:
    content_lines = str(item.content).strip().splitlines() or [""]
    lines = [f"- [{item.priority}][{item.status}] {content_lines[0]}"]
    lines.extend(f"  {line}" for line in content_lines[1:])
    if item.resource_references:
        lines.append(f"  - resources: {item.resource_references}")
    if item.entities:
        lines.append(f"  - entities: {', '.join(item.entities)}")
    return lines


def _resource_reference_lines(job: CompressionJob) -> list[str]:
    seen: set[str] = set()
    lines: list[str] = []
    for item in job.section_items:
        for reference in item.resource_references:
            text = str(reference)
            if text in seen:
                continue
            seen.add(text)
            lines.append(f"- {text}")
    return lines


def _assembler_input(job: CompressionJob) -> str:
    section_order = [section for _worker_name, section in SECTION_WORKERS]
    grouped_items: dict[str, list[Any]] = {section: [] for section in section_order}
    for item in job.section_items:
        grouped_items.setdefault(item.section, []).append(item)

    ordered_sections = [section for section in section_order if grouped_items.get(section)]
    ordered_sections.extend(section for section in grouped_items if section not in section_order and grouped_items.get(section))

    lines = [
        "# Context Compression Business Materials",
        "",
        "下面是从目标 Agent 的历史会话中按业务维度提取的压缩材料。",
        "请基于这些业务材料合并、去重、排序并生成下游业务 Agent 可读的历史上下文摘要。",
        "",
    ]
    for section in ordered_sections:
        title = SECTION_WORKER_TITLES.get(section, section)
        lines.extend([
            f"## {title}",
            f"这是从目标 Agent 的会话中提取的{title}信息：",
            "",
        ])
        for index, item in enumerate(grouped_items[section], start=1):
            lines.extend([
                f"### 条目 {index}",
                f"- priority: {item.priority}",
                f"- status: {item.status}",
            ])
            if item.resource_references:
                lines.append(f"- 可复查资源定位: {item.resource_references}")
            lines.extend([
                item.content,
                "",
            ])
    return "\n".join(lines).strip()


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
        "input_mode": job.input_mode,
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

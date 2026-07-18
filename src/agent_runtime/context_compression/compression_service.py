from __future__ import annotations

import asyncio
import time
from contextlib import suppress
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
    SECTION_WORKER_TITLES,
    context_compression_worker_id,
    context_compression_worker_title,
)
from agent_runtime.context_compression.profiles import (
    CompressionPipelineProfile,
    DEFAULT_ACTIVE_PROFILE_ID,
    get_profile,
    normalize_profile_id,
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
from agent_runtime.context_compression.worker_input import ContextCompressionWorkerInput
from agent_runtime.context_compression.worker_runner import ContextCompressionWorkerRunner
from agent_runtime.conversation_message.conversation_repository import ConversationRepository
from agent_runtime.conversation_message.message.models import Conversation
from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import ResourceResolver
from agent_runtime.infrastructure.runtime_task_registry import get_runtime_task_registry
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
    stage_id: str = ""
    title: str = ""
    input_builder: str = "native_history"
    upstream_section_ids: tuple[str, ...] = ()


def _mark_context_compression_task_progress(
    job: CompressionJob,
    *,
    phase: str,
    worker: _SectionWorkerRun | None = None,
    worker_count: int | None = None,
    pending_worker_count: int | None = None,
    completed_worker_count: int | None = None,
    cancelled_worker_count: int | None = None,
    reason: str | None = None,
    error_type: str | None = None,
    duration_ms: int | None = None,
    input_mode: str | None = None,
) -> None:
    """Best-effort Registry progress for the owner task running a compression job.

    The section workers themselves remain job-local structured-concurrency helper
    tasks.  When the caller is already Registry-managed, this records bounded
    job-level phases on that owner task; otherwise mark_progress is a no-op.
    """
    task = asyncio.current_task()
    if task is None:
        return
    worker_id = (
        context_compression_worker_id(worker.worker_name, "section_worker", worker.section)
        if worker is not None
        else None
    )
    diagnostic_context: dict[str, Any] = {
        "phase": phase,
        "job_id": job.job_id,
        "agent_task_id": job.task_id,
        "agent_instance_id": job.agent_instance_id,
        "root_conversation_id": job.root_conversation_id,
        "source_conversation_id": job.source_conversation_id,
        "compression_index": job.compression_index,
        "input_mode": input_mode or job.input_mode,
    }
    if worker_count is not None:
        diagnostic_context["worker_count"] = worker_count
    if pending_worker_count is not None:
        diagnostic_context["pending_worker_count"] = pending_worker_count
    if completed_worker_count is not None:
        diagnostic_context["completed_worker_count"] = completed_worker_count
    if cancelled_worker_count is not None:
        diagnostic_context["cancelled_worker_count"] = cancelled_worker_count
    if worker is not None:
        diagnostic_context.update(
            {
                "worker_id": worker_id,
                "worker_name": worker.worker_name,
                "worker_role": "section_worker",
                "output_section": worker.section,
            }
        )
    if reason is not None:
        diagnostic_context["reason"] = reason
    if error_type is not None:
        diagnostic_context["error_type"] = error_type
    if duration_ms is not None:
        diagnostic_context["duration_ms"] = duration_ms
    with suppress(Exception):
        get_runtime_task_registry().mark_progress(task, diagnostic_context=diagnostic_context)


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
        # 约定大于配置：始终使用 native_multimodal_history，不接受 runtime 模式切换。
        config = replace(self._config_accessor(), input_mode="native_multimodal_history")
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
        active_profile_id = DEFAULT_ACTIVE_PROFILE_ID
        try:
            active_profile_id = normalize_profile_id(self._settings_repository.get_active_profile_id())
        except Exception:
            active_profile_id = DEFAULT_ACTIVE_PROFILE_ID
        profile = get_profile(active_profile_id)
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
            profile_id=profile.profile_id,
            profile_version=profile.version,
            assembly_strategy=profile.assembly.strategy,
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
            effective_input_mode = "native_multimodal_history"
            job.input_mode = effective_input_mode
            self._archive_repository.upsert_job(job, source_conversation=source_conversation)
            await self._event_publisher.publish(communication_context, CONTEXT_COMPRESSION_RANGE_PLANNED, _base_payload(job, status="running", phase="range_planned"))
            await self._run_profile_stages(
                profile=profile,
                job=job,
                source_conversation=source_conversation,
                range_plan=range_plan,
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
                profile=profile,
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
            if config.verifier_enabled and profile.verifier.worker_name:
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

    async def _run_profile_stages(
        self,
        *,
        profile: CompressionPipelineProfile,
        job: CompressionJob,
        source_conversation: Conversation,
        range_plan: CompressionRange,
        input_mode: str,
        resolved_candidates: tuple[ResolvedLlmProviderModel, ...],
        max_rounds: int,
        communication_context: RuntimeCommunicationContext | None,
        interrupt_checker: Callable[[], bool] | None,
        request_canceller_observer: Callable[[Callable[[], None]], Callable[[], None] | None] | None,
    ) -> None:
        for stage in profile.stages:
            workers = tuple(
                self._build_section_worker_run(profile, worker_name, input_mode=input_mode)
                for worker_name in stage.worker_names
            )
            if stage.mode == "serial":
                for worker in workers:
                    worker_input = self._build_worker_input_for_def(
                        worker=worker,
                        source_conversation=source_conversation,
                        job=job,
                        range_plan=range_plan,
                    )
                    await self._run_section_workers(
                        job=job,
                        workers=(worker,),
                        worker_input_by_name={worker.worker_name: worker_input},
                        input_mode=input_mode,
                        resolved_candidates=resolved_candidates,
                        max_rounds=max_rounds,
                        communication_context=communication_context,
                        interrupt_checker=interrupt_checker,
                        request_canceller_observer=request_canceller_observer,
                    )
            else:
                worker_input_by_name = {
                    worker.worker_name: self._build_worker_input_for_def(
                        worker=worker,
                        source_conversation=source_conversation,
                        job=job,
                        range_plan=range_plan,
                    )
                    for worker in workers
                }
                await self._run_section_workers(
                    job=job,
                    workers=workers,
                    worker_input_by_name=worker_input_by_name,
                    input_mode=input_mode,
                    resolved_candidates=resolved_candidates,
                    max_rounds=max_rounds,
                    communication_context=communication_context,
                    interrupt_checker=interrupt_checker,
                    request_canceller_observer=request_canceller_observer,
                )

    def _build_section_worker_run(
        self,
        profile: CompressionPipelineProfile,
        worker_name: str,
        *,
        input_mode: str,
    ) -> _SectionWorkerRun:
        worker_def = profile.worker_by_name(worker_name)
        if worker_def.role != "section_worker" or not worker_def.output_section:
            raise ValueError(f"非 section worker 不能进入阶段执行: {worker_name}")
        return _SectionWorkerRun(
            worker_name=worker_def.worker_name,
            section=worker_def.output_section,
            system_prompt=self._prompt_loader.load_prompt(
                profile_id=profile.profile_id,
                input_mode=input_mode,
                worker_name=worker_def.worker_name,
            ),
            stage_id=worker_def.stage_id,
            title=worker_def.title,
            input_builder=worker_def.input_builder,
            upstream_section_ids=worker_def.upstream_section_ids,
        )

    def _build_worker_input_for_def(
        self,
        *,
        worker: _SectionWorkerRun,
        source_conversation: Conversation,
        job: CompressionJob,
        range_plan: CompressionRange,
    ) -> ContextCompressionWorkerInput:
        history = tuple(
            source_conversation.messages[
                range_plan.compressed_start_index : range_plan.compressed_end_index + 1
            ]
        )
        if worker.input_builder == "native_history_with_upstream_sections":
            upstream_text = _render_upstream_sections_markdown(job, worker.upstream_section_ids)
            user_text = "\n\n".join(
                [
                    _authoritative_upstream_material(upstream_text),
                    _native_history_instruction(worker_title=worker.title),
                ]
            )
        else:
            user_text = _native_history_instruction(worker_title=worker.title)
        return ContextCompressionWorkerInput(
            input_mode="native_multimodal_history",
            user_text=user_text,
            history_messages=history,
        )

    async def _run_section_workers(
        self,
        *,
        job: CompressionJob,
        workers: tuple[_SectionWorkerRun, ...],
        worker_input_by_name: dict[str, ContextCompressionWorkerInput],
        input_mode: str,
        resolved_candidates: tuple[ResolvedLlmProviderModel, ...],
        max_rounds: int,
        communication_context: RuntimeCommunicationContext | None,
        interrupt_checker: Callable[[], bool] | None,
        request_canceller_observer: Callable[[Callable[[], None]], Callable[[], None] | None] | None,
    ) -> None:
        _mark_context_compression_task_progress(
            job,
            phase="section_workers.queued",
            worker_count=len(workers),
            pending_worker_count=len(workers),
            completed_worker_count=0,
            cancelled_worker_count=0,
            input_mode=input_mode,
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
        # Stage 8/P1.2 protected direct-task exception: these section workers are
        # short-lived, job-local structured-concurrency helpers.  This owner
        # coroutine creates every worker, waits with FIRST_EXCEPTION, cancels
        # siblings before cleanup, and gathers remaining tasks before returning.
        # Do not mechanically migrate per-worker tasks into RuntimeTaskRegistry;
        # job-level Registry progress above/below is only best-effort observation.
        started_at = time.perf_counter()
        tasks = {
            asyncio.create_task(
                self._run_one_section_worker(
                    job=job,
                    worker=worker,
                    worker_input=worker_input_by_name[worker.worker_name],
                    input_mode=input_mode,
                    resolved_candidates=resolved_candidates,
                    max_rounds=max_rounds,
                    communication_context=communication_context,
                    interrupt_checker=interrupt_checker,
                    request_canceller_observer=request_canceller_observer,
                ),
                name=f"context-compression-section-worker-{context_compression_worker_id(worker.worker_name, 'section_worker', worker.section)}",
            ): worker
            for worker in workers
        }
        pending = set(tasks)
        completed_worker_count = 0
        cancelled_worker_count = 0
        _mark_context_compression_task_progress(
            job,
            phase="section_workers.started",
            worker_count=len(workers),
            pending_worker_count=len(pending),
            completed_worker_count=completed_worker_count,
            cancelled_worker_count=cancelled_worker_count,
            input_mode=input_mode,
        )
        try:
            while pending:
                _mark_context_compression_task_progress(
                    job,
                    phase="section_workers.waiting",
                    worker_count=len(workers),
                    pending_worker_count=len(pending),
                    completed_worker_count=completed_worker_count,
                    cancelled_worker_count=cancelled_worker_count,
                    input_mode=input_mode,
                )
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_EXCEPTION)
                failure_error: BaseException | None = None
                cancellation_error: BaseException | None = None
                for task in done:
                    worker = tasks[task]
                    try:
                        await task
                        completed_worker_count += 1
                        _mark_context_compression_task_progress(
                            job,
                            phase="section_workers.worker_completed",
                            worker=worker,
                            worker_count=len(workers),
                            pending_worker_count=len(pending),
                            completed_worker_count=completed_worker_count,
                            cancelled_worker_count=cancelled_worker_count,
                            duration_ms=int((time.perf_counter() - started_at) * 1000),
                            input_mode=input_mode,
                        )
                    except LlmRequestCancelled as exc:
                        cancellation_error = cancellation_error or exc
                    except asyncio.CancelledError as exc:
                        cancellation_error = cancellation_error or exc
                    except Exception as exc:
                        failure_error = failure_error or exc
                first_error = failure_error or cancellation_error
                if first_error is not None:
                    self._job_store.update_status(job.job_id, "failed" if failure_error is not None else "cancelled")
                    reason = "sibling_worker_failed" if job.status == "failed" else "context_compression_cancelled"
                    _mark_context_compression_task_progress(
                        job,
                        phase="section_workers.first_exception",
                        worker_count=len(workers),
                        pending_worker_count=len(pending),
                        completed_worker_count=completed_worker_count,
                        cancelled_worker_count=cancelled_worker_count,
                        reason=reason,
                        error_type=first_error.__class__.__name__,
                        duration_ms=int((time.perf_counter() - started_at) * 1000),
                        input_mode=input_mode,
                    )
                    cancelled_worker_count += len(pending)
                    await self._cancel_pending_section_workers(
                        job=job,
                        pending_tasks=pending,
                        task_workers=tasks,
                        input_mode=input_mode,
                        communication_context=communication_context,
                        reason=reason,
                    )
                    _mark_context_compression_task_progress(
                        job,
                        phase="section_workers.sibling_cancelled",
                        worker_count=len(workers),
                        pending_worker_count=0,
                        completed_worker_count=completed_worker_count,
                        cancelled_worker_count=cancelled_worker_count,
                        reason=reason,
                        error_type=first_error.__class__.__name__,
                        duration_ms=int((time.perf_counter() - started_at) * 1000),
                        input_mode=input_mode,
                    )
                    pending = set()
                    if failure_error is None and isinstance(first_error, asyncio.CancelledError):
                        raise first_error
                    raise first_error
        except asyncio.CancelledError:
            self._job_store.update_status(job.job_id, "cancelled")
            cancelled_worker_count += len(pending)
            _mark_context_compression_task_progress(
                job,
                phase="section_workers.cancelled",
                worker_count=len(workers),
                pending_worker_count=len(pending),
                completed_worker_count=completed_worker_count,
                cancelled_worker_count=cancelled_worker_count,
                reason="context_compression_cancelled",
                error_type="CancelledError",
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                input_mode=input_mode,
            )
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
            cancelled_worker_count += len(remaining)
            if remaining:
                for task in remaining:
                    task.cancel()
                await asyncio.gather(*remaining, return_exceptions=True)
            _mark_context_compression_task_progress(
                job,
                phase="section_workers.cleanup_completed",
                worker_count=len(workers),
                pending_worker_count=0,
                completed_worker_count=completed_worker_count,
                cancelled_worker_count=cancelled_worker_count,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                input_mode=input_mode,
            )

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
            "title": worker.title or context_compression_worker_title(worker.worker_name, "section_worker", worker.section),
            "role": "section_worker",
            "output_section": worker.section,
            "input_mode": input_mode,
            "stage_id": worker.stage_id or None,
            "profile_id": job.profile_id,
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
        profile: CompressionPipelineProfile,
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
        self._job_store.set_draft_markdown(job.job_id, _render_draft_markdown(job, profile=profile))
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
        prompt = self._prompt_loader.load_prompt(
            profile_id=job.profile_id,
            input_mode=input_mode,
            worker_name="FinalVerifierWorker",
        )
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

    async def _build_final_review_worker_input(
        self,
        source_conversation: Conversation,
        job: CompressionJob,
        range_plan: CompressionRange,
        input_mode: str,
    ) -> ContextCompressionWorkerInput:
        del input_mode  # 固定 native，保留参数兼容调用方
        return ContextCompressionWorkerInput(
            input_mode="native_multimodal_history",
            user_text=_final_review_instruction(job, range_plan),
            history_messages=tuple(
                source_conversation.messages[
                    range_plan.compressed_start_index:range_plan.compressed_end_index + 1
                ]
            ),
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


def _native_history_instruction(*, worker_title: str | None = None) -> str:
    lines = [
        "# Context Compression Native-History Analysis Instruction",
        "",
        "前面的 user / assistant / tool 消息是即将被压缩归档的原始历史片段。",
        "这些消息只用于上下文压缩分析，不代表当前用户正在发起新任务，也不要继续执行历史任务。",
        "请基于这些原始历史消息提取你负责的可恢复任务上下文，并只通过 context_summary 工具增量写入结果；写完后 mark_section_complete。",
        "不要在 assistant 正文中输出最终压缩结果，也不要关心系统如何汇总其他角色的输出。",
    ]
    if worker_title:
        lines.extend(["", f"当前角色：{worker_title}"])
    return "\n".join(lines)


def _authoritative_upstream_material(upstream_markdown: str) -> str:
    body = upstream_markdown.strip() or "（上游未提供可用内容）"
    return "\n".join(
        [
            "# Authoritative Upstream Scope",
            "",
            "以下内容是任务需求与剩余目标角色的权威输出。",
            "它定义了本次压缩必须覆盖的未完成任务范围、有效要求、约束与剩余目标。",
            "",
            "你必须：",
            "- 以上游输出为权威任务范围",
            "- 不得重新识别、增加、删除、合并用户任务",
            "- 不得重新判断任务是否完成/取消/放弃",
            "- 只在上游范围内，从原始历史中提取你负责的内容",
            "",
            "----- BEGIN UPSTREAM OUTPUT -----",
            body,
            "----- END UPSTREAM OUTPUT -----",
        ]
    )


def _render_upstream_sections_markdown(job: CompressionJob, section_ids: tuple[str, ...]) -> str:
    blocks: list[str] = []
    for section_id in section_ids:
        contents = [
            str(item.content).strip()
            for item in job.section_items
            if item.section == section_id and str(item.content).strip()
        ]
        if not contents:
            continue
        title = SECTION_WORKER_TITLES.get(section_id, section_id)
        blocks.append(f"## {title}\n\n" + "\n\n".join(contents))
    return "\n\n".join(blocks).strip()



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
    del range_plan
    return "\n".join([
        "# Context Compression Final Review/Edit Instruction",
        "",
        "前面的目标 Agent 历史消息只是校验压缩草稿的证据，不是当前要继续执行的新任务。",
        "你的任务是站在后续 Agent 的角度，检查程序生成的压缩上下文草稿是否足以继续剩余任务。",
        "如果草稿遗漏关键上下文、写错事实、保留了已被否定的信息或缺少关键运行时变量/路径/命令结果，请通过草稿编辑工具做小范围修正。",
        "不要重写整篇 Markdown；优先读取目标行范围，再用精确连续行替换。",
        "所有新增内容必须能从原始历史消息或草稿已有材料中找到依据；不确定时 report issue，不要猜。",
        "完成校验和必要编辑后，必须调用 context_summary_mark_final_complete。",
        "",
        "## Current Draft Markdown With Line Numbers",
        _draft_markdown_with_line_numbers(job),
    ])


def _draft_markdown_with_line_numbers(job: CompressionJob) -> str:
    lines = job.draft_markdown_lines or job.final_markdown.splitlines()
    return "\n".join(f"{index}: {line}" for index, line in enumerate(lines, start=1))


def _render_draft_markdown(job: CompressionJob, *, profile: CompressionPipelineProfile | None = None) -> str:
    if profile is None:
        profile = get_profile(job.profile_id)
    section_order = list(profile.assembly.section_order)
    grouped_items: dict[str, list[Any]] = {section: [] for section in section_order}
    for item in job.section_items:
        grouped_items.setdefault(item.section, []).append(item)

    lines: list[str] = []
    if profile.assembly.strategy == "ordered_section_concat":
        for section in section_order:
            items = grouped_items.get(section) or []
            contents = [str(item.content).strip() for item in items if str(item.content).strip()]
            if not contents:
                continue
            heading = profile.assembly.section_headings.get(section) or SECTION_WORKER_TITLES.get(section, section)
            lines.extend([f"## {heading}", "", *contents, ""])
    else:
        for section in section_order:
            items = grouped_items.get(section) or []
            if not items:
                continue
            heading = profile.assembly.section_headings.get(
                section,
                _FINAL_DRAFT_SECTION_HEADINGS.get(section, SECTION_WORKER_TITLES.get(section, section)),
            )
            lines.extend([f"## {heading}", ""])
            for item in sorted(items, key=_section_item_sort_key):
                lines.extend(_section_item_lines(item))
            lines.append("")
        if profile.assembly.include_resource_reference_appendix:
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
    section_order = list(get_profile(job.profile_id).assembly.section_order)
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
        "profile_id": job.profile_id,
        "profile_version": job.profile_version,
        "assembly_strategy": job.assembly_strategy,
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

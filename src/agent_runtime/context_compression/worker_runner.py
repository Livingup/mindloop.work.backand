from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from agent_runtime.context_compression.event_publisher import (
    CONTEXT_COMPRESSION_WORKER_COMPLETED,
    CONTEXT_COMPRESSION_WORKER_ACTIVITY,
    CONTEXT_COMPRESSION_WORKER_FAILED,
    CONTEXT_COMPRESSION_WORKER_PROGRESS,
    CONTEXT_COMPRESSION_WORKER_STARTED,
    ContextCompressionEventPublisher,
)
from agent_runtime.context_compression.job_store import InMemoryCompressionJobStore
from agent_runtime.context_compression.models import (
    CompressionJob,
    context_compression_worker_id,
    context_compression_worker_title,
)
from agent_runtime.context_compression.summary_tools import ContextSummaryToolExecutor
from agent_runtime.conversation_message.message.models import Conversation, ROLE_TOOL, TextPart, TextPartMeta, ToolUsePart, UniMessage
from agent_runtime.conversation_message.message.operations import create_conversation
from agent_runtime.llm_client.llm_protocol_clients import LlmRequestCancelled
from agent_runtime.llm_client.llm_request_models import LlmPromptRequest
from agent_runtime.llm_client.llm_request_service import LlmProviderModelServiceUnavailable, LlmRequestService
from agent_runtime.llm_model_registry.llm_model_registry_service import ResolvedLlmProviderModel
from agent_runtime.communication.runtime_event_publisher import RuntimeCommunicationContext


class ContextCompressionWorkerRunner:
    def __init__(
        self,
        *,
        llm_request_service: LlmRequestService,
        job_store: InMemoryCompressionJobStore,
        event_publisher: ContextCompressionEventPublisher,
    ) -> None:
        self._llm_request_service = llm_request_service
        self._job_store = job_store
        self._event_publisher = event_publisher
        self._summary_tools = ContextSummaryToolExecutor(job_store)

    async def run_worker(
        self,
        *,
        job: CompressionJob,
        worker_name: str,
        worker_role: str,
        output_section: str | None,
        input_mode: str,
        system_prompt: str,
        input_text: str,
        resolved_candidates: tuple[ResolvedLlmProviderModel, ...],
        max_rounds: int,
        communication_context: RuntimeCommunicationContext | None,
        allowed_tool_names: set[str] | None = None,
        interrupt_checker: Callable[[], bool] | None = None,
        request_canceller_observer: Callable[[Callable[[], None]], Callable[[], None] | None] | None = None,
    ) -> None:
        last_error: Exception | None = None
        skipped_tool_calling = False
        for candidate_index, resolved_model in enumerate(resolved_candidates):
            if not resolved_model.supports_tool_calling:
                skipped_tool_calling = True
                continue
            next_resolved_model = _next_tool_calling_candidate(resolved_candidates, candidate_index)
            try:
                await self._run_worker_with_model(
                    job=job,
                    worker_name=worker_name,
                    worker_role=worker_role,
                    output_section=output_section,
                    input_mode=input_mode,
                    system_prompt=system_prompt,
                    input_text=input_text,
                    resolved_model=resolved_model,
                    next_resolved_model=next_resolved_model,
                    max_rounds=max_rounds,
                    communication_context=communication_context,
                    allowed_tool_names=allowed_tool_names,
                    interrupt_checker=interrupt_checker,
                    request_canceller_observer=request_canceller_observer,
                )
                return
            except LlmRequestCancelled:
                raise
            except Exception as exc:
                last_error = exc
                can_failover = (
                    next_resolved_model is not None
                    and isinstance(exc, LlmProviderModelServiceUnavailable)
                )
                if not can_failover:
                    raise
        if last_error is not None:
            raise last_error
        if skipped_tool_calling:
            raise RuntimeError("上下文压缩流水线绑定的 LLM 执行策略组没有支持 tool calling 的可用候选。")
        raise RuntimeError("上下文压缩流水线绑定的 LLM 执行策略组没有可用候选。")

    async def _run_worker_with_model(
        self,
        *,
        job: CompressionJob,
        worker_name: str,
        worker_role: str,
        output_section: str | None,
        input_mode: str,
        system_prompt: str,
        input_text: str,
        resolved_model: ResolvedLlmProviderModel,
        next_resolved_model: ResolvedLlmProviderModel | None,
        max_rounds: int,
        communication_context: RuntimeCommunicationContext | None,
        allowed_tool_names: set[str] | None,
        interrupt_checker: Callable[[], bool] | None,
        request_canceller_observer: Callable[[Callable[[], None]], Callable[[], None] | None] | None,
    ) -> None:
        started_at = time.perf_counter()
        event_loop = asyncio.get_running_loop()
        last_activity_signature: tuple[str, str] | None = None

        def schedule_activity(kind: str, text: str, details: dict[str, Any] | None = None) -> None:
            nonlocal last_activity_signature
            signature = (kind, text)
            if signature == last_activity_signature:
                return
            last_activity_signature = signature

            async def publish() -> None:
                await self._publish_worker_activity(
                    communication_context,
                    job,
                    worker_name=worker_name,
                    worker_role=worker_role,
                    output_section=output_section,
                    input_mode=input_mode,
                    resolved_model=resolved_model,
                    kind=kind,
                    text=text,
                    details=details,
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
                )

            event_loop.call_soon_threadsafe(lambda: asyncio.create_task(publish()))

        def observe_llm_event(event_type: str, payload: dict[str, Any]) -> None:
            activity = _activity_from_llm_event(event_type, payload)
            if activity is None:
                return
            kind, text = activity
            schedule_activity(kind, text, payload)

        await self._publish_worker_event(
            communication_context,
            CONTEXT_COMPRESSION_WORKER_STARTED,
            job,
            worker_name=worker_name,
            worker_role=worker_role,
            output_section=output_section,
            input_mode=input_mode,
            resolved_model=resolved_model,
            round_index=0,
            max_rounds=max_rounds,
        )
        schedule_activity("preparing", "正在准备输入")
        conversation = create_conversation()
        conversation.add_user_message([TextPart(text=input_text)], [TextPartMeta()])
        completed_without_marker = False
        try:
            for round_index in range(1, max_rounds + 1):
                if interrupt_checker is not None and interrupt_checker():
                    raise LlmRequestCancelled("上下文压缩 Worker 被用户中断。")
                response = await self._llm_request_service.send_prompt(
                    LlmPromptRequest(
                        conversation=conversation,
                        resolved_model=resolved_model,
                        system_prompt=system_prompt,
                        tools=self._summary_tools.build_tools(
                            resolved_model.api_format,
                            allowed_tool_names=allowed_tool_names,
                        ),
                        communication_context=None,
                        next_resolved_model=next_resolved_model,
                        cancellation_checker=interrupt_checker,
                        request_canceller_observer=request_canceller_observer,
                        request_event_observer=observe_llm_event,
                    )
                )
                tool_uses = _extract_tool_uses(response.assistant_message)
                if not tool_uses:
                    if worker_role == "final_assembler" and response.assistant_message.content:
                        schedule_activity("writing_result", "正在写入压缩结果")
                        self._job_store.append_final_markdown(job.job_id, _assistant_text(response.assistant_message))
                    if worker_role == "final_verifier":
                        completed_without_marker = True
                    break
                schedule_activity("writing_result", "正在写入压缩结果")
                result_parts = [
                    self._summary_tools.execute(
                        job_id=job.job_id,
                        worker_name=worker_name,
                        expected_section=output_section if worker_role == "section_worker" else None,
                        tool_use=tool_use,
                        allowed_tool_names=allowed_tool_names,
                    )
                    for tool_use in tool_uses
                ]
                conversation.append_message(UniMessage(role=ROLE_TOOL, content=result_parts))
                await self._publish_worker_event(
                    communication_context,
                    CONTEXT_COMPRESSION_WORKER_PROGRESS,
                    job,
                    worker_name=worker_name,
                    worker_role=worker_role,
                    output_section=output_section,
                    input_mode=input_mode,
                    resolved_model=resolved_model,
                    round_index=round_index,
                    max_rounds=max_rounds,
                )
                if _worker_finished(job, worker_role, output_section):
                    break
            if not completed_without_marker and not _worker_finished(job, worker_role, output_section):
                raise RuntimeError(
                    f"上下文压缩 Worker 未在安全轮次内完成: worker={worker_name}, max_rounds={max_rounds}"
                )
            await self._publish_worker_event(
                communication_context,
                CONTEXT_COMPRESSION_WORKER_COMPLETED,
                job,
                worker_name=worker_name,
                worker_role=worker_role,
                output_section=output_section,
                input_mode=input_mode,
                resolved_model=resolved_model,
                round_index=min(max_rounds, round_index if "round_index" in locals() else 0),
                max_rounds=max_rounds,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
        except LlmRequestCancelled:
            raise
        except Exception as exc:
            if not (next_resolved_model is not None and isinstance(exc, LlmProviderModelServiceUnavailable)):
                await self._publish_worker_event(
                    communication_context,
                    CONTEXT_COMPRESSION_WORKER_FAILED,
                    job,
                    worker_name=worker_name,
                    worker_role=worker_role,
                    output_section=output_section,
                    input_mode=input_mode,
                    resolved_model=resolved_model,
                    round_index=0,
                    max_rounds=max_rounds,
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
                    error_type=exc.__class__.__name__,
                    error_message=str(exc),
                )
            raise

    async def _publish_worker_activity(
        self,
        context: RuntimeCommunicationContext | None,
        job: CompressionJob,
        *,
        worker_name: str,
        worker_role: str,
        output_section: str | None,
        input_mode: str,
        resolved_model: ResolvedLlmProviderModel,
        kind: str,
        text: str,
        details: dict[str, Any] | None,
        duration_ms: int,
    ) -> None:
        await self._publish_worker_event(
            context,
            CONTEXT_COMPRESSION_WORKER_ACTIVITY,
            job,
            worker_name=worker_name,
            worker_role=worker_role,
            output_section=output_section,
            input_mode=input_mode,
            resolved_model=resolved_model,
            round_index=int(details.get("retry_index") or 0) if details else 0,
            max_rounds=0,
            duration_ms=duration_ms,
            activity={
                "kind": kind,
                "text": text,
                **_activity_details(details),
            },
        )

    async def _publish_worker_event(
        self,
        context: RuntimeCommunicationContext | None,
        event_type: str,
        job: CompressionJob,
        *,
        worker_name: str,
        worker_role: str,
        output_section: str | None,
        input_mode: str,
        resolved_model: ResolvedLlmProviderModel,
        round_index: int,
        max_rounds: int,
        duration_ms: int | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        activity: dict[str, Any] | None = None,
    ) -> None:
        payload = _base_payload(job)
        payload["phase"] = _phase_for_worker_role(worker_role)
        worker_id = context_compression_worker_id(worker_name, worker_role, output_section)
        payload["worker"] = {
            "worker_id": worker_id,
            "id": worker_id,
            "name": worker_name,
            "title": context_compression_worker_title(worker_name, worker_role, output_section),
            "role": worker_role,
            "output_section": output_section,
            "input_mode": input_mode,
            "round_index": round_index,
            "max_rounds": max_rounds,
            "provider_id": resolved_model.provider_id,
            "model_binding_id": resolved_model.binding_id,
            "model_id": resolved_model.model_id,
            "progress": {
                "section_items_appended": len(job.section_items),
                "total_section_items": len(job.section_items),
                "final_markdown_chunks_appended": len(job.final_markdown_chunks),
            },
        }
        if activity is not None:
            payload["worker"]["activity"] = activity
        if duration_ms is not None:
            payload["worker"]["duration_ms"] = duration_ms
            if event_type not in {CONTEXT_COMPRESSION_WORKER_ACTIVITY, CONTEXT_COMPRESSION_WORKER_COMPLETED, CONTEXT_COMPRESSION_WORKER_FAILED}:
                payload.setdefault("metrics", {})["duration_ms"] = duration_ms
        if error_type is not None:
            payload["worker"]["error_type"] = error_type
            payload["error_type"] = error_type
        if error_message is not None:
            payload["worker"]["error_message"] = error_message
            payload["error_message"] = error_message
        await self._event_publisher.publish(context, event_type, payload, source="context_compression_worker_runner")


def _extract_tool_uses(message: UniMessage) -> list[ToolUsePart]:
    return [part for part in message.content if isinstance(part, ToolUsePart)]


def _assistant_text(message: UniMessage) -> str:
    return "\n".join(part.text for part in message.content if isinstance(part, TextPart)).strip()


def _next_tool_calling_candidate(
    candidates: tuple[ResolvedLlmProviderModel, ...],
    current_index: int,
) -> ResolvedLlmProviderModel | None:
    for candidate in candidates[current_index + 1:]:
        if candidate.supports_tool_calling:
            return candidate
    return None


def _activity_from_llm_event(event_type: str, payload: dict[str, Any]) -> tuple[str, str] | None:
    if event_type == "llm.request.started":
        return "llm_requesting", f"正在请求 {_model_label(payload)} · {_key_label(payload)}"
    if event_type == "llm.response.started":
        return "llm_responding", "模型已响应，正在分析"
    if event_type == "llm.thinking.started":
        return "llm_responding", "模型已响应，正在分析"
    if event_type == "llm.content.started":
        return "llm_responding", "模型已响应，正在分析"
    if event_type == "llm.tool_call.started":
        return "writing_result", "正在写入压缩结果"
    if event_type == "llm.request.failed":
        return _failed_activity(payload)
    if event_type == "llm.request.cancelled":
        return "cancelled", "已取消"
    return None


def _failed_activity(payload: dict[str, Any]) -> tuple[str, str]:
    if payload.get("will_retry") is True:
        return "llm_retrying", f"请求失败，正在重试（{_retry_target_label(payload)}）"
    return "llm_failed", _short_error_text(payload)


def _activity_details(details: dict[str, Any] | None) -> dict[str, Any]:
    if not details:
        return {}
    result: dict[str, Any] = {}
    for key in (
        "provider_id",
        "model_id",
        "model_alias",
        "api_key_index",
        "api_key_label",
        "retry_index",
        "attempt_count",
        "error_type",
        "error_message",
    ):
        value = details.get(key)
        if value is not None:
            result[key] = value
    return result


def _model_label(payload: dict[str, Any]) -> str:
    value = payload.get("model_alias") or payload.get("model_id")
    return str(value) if value else "模型"


def _key_label(payload: dict[str, Any]) -> str:
    value = payload.get("api_key_label")
    if value:
        return str(value)
    index = payload.get("api_key_index")
    if isinstance(index, int):
        return f"key-{index + 1}"
    return "key"


def _retry_target_label(payload: dict[str, Any]) -> str:
    provider = str(payload.get("next_provider_id") or payload.get("provider_id") or "服务商")
    model = str(
        payload.get("next_model_alias")
        or payload.get("next_model_id")
        or payload.get("model_alias")
        or payload.get("model_id")
        or "模型"
    )
    key_index = payload.get("next_api_key_index")
    if not isinstance(key_index, int):
        key_index = payload.get("api_key_index")
    if isinstance(key_index, int):
        key = f"key-{key_index + 1}"
    else:
        key = str(payload.get("api_key_label") or "key")
    return f"{provider} / {model} / {key}"


def _short_error_text(payload: dict[str, Any]) -> str:
    message = str(payload.get("error_message") or payload.get("error_type") or "请求失败")
    message = " ".join(message.split())
    if len(message) > 80:
        return f"{message[:77]}..."
    return message


def _worker_finished(job: CompressionJob, worker_role: str, output_section: str | None) -> bool:
    if worker_role == "section_worker" and output_section is not None:
        return output_section in job.completed_sections
    if worker_role == "final_assembler":
        return job.status == "assembled"
    return False


def _phase_for_worker_role(worker_role: str) -> str:
    if worker_role == "section_worker":
        return "workers"
    if worker_role == "final_verifier":
        return "verification"
    return "assembly"


def _base_payload(job: CompressionJob) -> dict[str, Any]:
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
        "status": job.status,
        "reason": range_plan.reason if range_plan else None,
        "compressed_range": range_plan.compressed_range_payload() if range_plan else None,
        "retained_tail_range": range_plan.retained_tail_range_payload() if range_plan else None,
    }

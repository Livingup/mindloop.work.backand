# -*- coding: utf-8 -*-
"""Standard OpenAI Responses bridge to the process-owned louyi-codex SDK."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from contextlib import suppress
from typing import Any, Callable

from agent_runtime.codex_oauth.diagnostics import (
    sdk_error_diagnostic,
    short_identifier,
    stable_fingerprint,
 )
from agent_runtime.codex_oauth.models import CODEX_PROVIDER_ID, CODEX_SESSION_HEADER_NAME
from agent_runtime.codex_oauth.service import CodexOAuthService
from agent_runtime.conversation_message.message.openai_protocol import OpenAIRenderResult
from agent_runtime.llm_api_protocol import API_FORMAT_CODEX_RESPONSES
from agent_runtime.llm_client.llm_protocol_clients import (
    CancellationChecker,
    LlmProviderResponseError,
    LlmRequestCancelled,
    StreamEventHandler,
 )
from agent_runtime.llm_http_trace.models import LlmHttpTraceContext
from agent_runtime.llm_http_trace.service import LlmHttpTraceService
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    ResolvedLlmProviderModel,
 )

logger = logging.getLogger("agent_runtime.codex_sdk")

_SUPPORTED_STANDARD_PARAMS = frozenset({
    "metadata",
    "top_logprobs",
    "temperature",
    "top_p",
    "user",
    "safety_identifier",
    "prompt_cache_key",
    "service_tier",
    "prompt_cache_retention",
    "prompt_cache_options",
    "background",
    "max_tool_calls",
    "text",
    "tools",
    "tool_choice",
    "prompt",
    "truncation",
    "reasoning",
    "include",
    "parallel_tool_calls",
    "store",
    "instructions",
    "moderation",
    "stream_options",
    "conversation",
    "context_management",
 })


class CodexSdkResponseError(LlmProviderResponseError):
    def __init__(self, message: str, *, sdk_error: BaseException | None = None, partial_output: bool = False) -> None:
        super().__init__(message)
        self.sdk_error = sdk_error
        self.partial_output = partial_output or bool(getattr(sdk_error, "partial_output", False))
        self.retryable = bool(getattr(sdk_error, "retryable", False))
        self.status_code = getattr(sdk_error, "http_status", None)
        self.code = getattr(sdk_error, "code", "codex_sdk_response_error")


class CodexResponsesProtocolClient:
    """Calls only the SDK Stable Responses surface; the SDK owns credentials and routing."""

    owns_credentials_and_retries = True

    def __init__(self, service: CodexOAuthService) -> None:
        self._service = service

    @property
    def api_format(self) -> str:
        return API_FORMAT_CODEX_RESPONSES

    async def request(
        self,
        *,
        resolved_model: ResolvedLlmProviderModel,
        conversation_id: str,
        rendered_messages: Any,
        system_prompt: str | None,
        tools: tuple[dict[str, Any], ...],
        request_params: dict[str, Any],
        api_key: str | None,
        stream_event_handler: StreamEventHandler | None = None,
        http_trace_service: LlmHttpTraceService | None = None,
        http_trace_context: LlmHttpTraceContext | None = None,
        cancellation_checker: CancellationChecker | None = None,
    ) -> Any:
        del api_key
        if resolved_model.provider_id != CODEX_PROVIDER_ID:
            raise CodexSdkResponseError(
                f"codex_responses 仅保留给托管 Provider {CODEX_PROVIDER_ID}。"
            )
        if not isinstance(rendered_messages, OpenAIRenderResult):
            raise TypeError("codex_responses 渲染结果类型不正确。")
        if not conversation_id:
            raise ValueError("Codex SDK 请求必须提供稳定的 Conversation.id。")

        sdk = _sdk_module()
        model, params = _build_standard_params(
            resolved_model=resolved_model,
            rendered_messages=rendered_messages,
            system_prompt=system_prompt,
            tools=tools,
            request_params=request_params,
        )
        cancellation_source = sdk.CancellationSource()
        options = sdk.RequestOptions(
            headers={CODEX_SESSION_HEADER_NAME: conversation_id},
            cancel_token=cancellation_source.token,
        )
        started_at = time.perf_counter()
        event_count = 0
        output_projected = False
        output_items: list[dict[str, Any]] = []
        response_body: dict[str, Any] | None = None
        stream: Any | None = None
        cancellation_task: asyncio.Task[None] | None = None
        trace_record = _trace_started(
            http_trace_service,
            http_trace_context,
            model=model,
            params=params,
            rendered_messages=rendered_messages,
            conversation_id=conversation_id,
            tools=tools,
        )
        logger.info(
            "codex_sdk.responses.request_started llm_request_id=%s model=%s session_fingerprint=%s standard_fields=%s input_items=%d tool_count=%d",
            short_identifier(getattr(http_trace_context, "llm_request_id", None)),
            model,
            stable_fingerprint(conversation_id),
            sorted(params.keys()),
            _item_count(rendered_messages.input),
            len(tools),
        )
        try:
            if cancellation_checker is not None:
                cancellation_task = asyncio.create_task(
                    _watch_cancellation(
                        cancellation_checker,
                        cancellation_source,
                        lambda: stream,
                    )
                )
            stream = await self._service.client.responses.create(
                model=model,
                input=rendered_messages.input,
                stream=True,
                request_options=options,
                **params,
            )
            metadata = stream.metadata
            logger.info(
                "codex_sdk.responses.stream_opened request_id=%s session_fingerprint=%s bound_account=%s current_account=%s",
                short_identifier(metadata.request_id),
                metadata.session_fingerprint,
                short_identifier(metadata.bound_account_id),
                short_identifier(metadata.current_account_id),
            )
            await _emit_stream_event(
                stream_event_handler,
                "llm.response.stream.started",
                {
                    "content_type": "text/event-stream",
                    "sdk_request_id": short_identifier(metadata.request_id),
                    "session_fingerprint": metadata.session_fingerprint,
                },
            )
            async with stream:
                async for event in stream:
                    event_count += 1
                    data = _event_to_dict(event)
                    event_type = str(data.get("type") or getattr(event, "type", "message"))
                    output_projected = output_projected or _event_projects_output(event_type, data)
                    await _emit_stream_event(
                        stream_event_handler,
                        "llm.response.stream.event",
                        {
                            "event": event_type,
                            "data_type": event_type,
                            "data": data,
                        },
                    )
                    if event_type == "response.output_item.done" and isinstance(data.get("item"), dict):
                        _upsert_output_item(output_items, copy.deepcopy(data["item"]))
                    if isinstance(data.get("response"), dict):
                        response_body = copy.deepcopy(data["response"])
                    if event_type == "response.completed":
                        if response_body is None:
                            response_body = {}
                        if not response_body.get("output") and output_items:
                            response_body["output"] = output_items
                    elif event_type in {"response.failed", "response.incomplete", "error"}:
                        raise CodexSdkResponseError(
                            f"louyi-codex Responses 流终止: event={event_type}",
                            partial_output=output_projected,
                        )
            outcome = await stream.wait_closed()
            metadata = stream.metadata
            if outcome.status != "completed" or response_body is None:
                raise CodexSdkResponseError(
                    f"louyi-codex Responses 未生成 completed 终态: status={outcome.status}",
                    partial_output=output_projected,
                )
            await _emit_stream_event(
                stream_event_handler,
                "llm.response.stream.completed",
                {
                    "event_count": event_count,
                    "sdk_request_id": short_identifier(metadata.request_id),
                    "current_account_id": short_identifier(metadata.current_account_id),
                },
            )
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            logger.info(
                "codex_sdk.responses.completed request_id=%s account_id=%s bound_account=%s terminal=%s event_count=%d attempts=%s duration_ms=%d",
                short_identifier(metadata.request_id),
                short_identifier(metadata.current_account_id),
                short_identifier(metadata.bound_account_id),
                metadata.terminal_event_type,
                event_count,
                _attempt_diagnostics(metadata.attempts),
                duration_ms,
            )
            _trace_completed(
                http_trace_service,
                http_trace_context,
                trace_record,
                metadata=metadata,
                event_count=event_count,
                duration_ms=duration_ms,
            )
            return response_body
        except asyncio.CancelledError:
            cancellation_source.cancel("Runtime LLM request task cancelled")
            if stream is not None:
                with suppress(Exception, asyncio.CancelledError):
                    await stream.cancel("Runtime LLM request task cancelled")
            if cancellation_checker is not None and cancellation_checker():
                with suppress(Exception):
                    _trace_failed(
                        http_trace_service, http_trace_context, trace_record,
                        error=LlmRequestCancelled("LLM request cancelled."),
                        event_count=event_count, started_at=started_at,
                    )
                raise LlmRequestCancelled("LLM request cancelled.") from None
            raise
        except Exception as exc:
            if isinstance(exc, getattr(sdk, "RequestCancelledError", ())):
                if cancellation_checker is not None and cancellation_checker():
                    exc = LlmRequestCancelled("LLM request cancelled.")
            if isinstance(exc, LlmRequestCancelled):
                with suppress(Exception):
                    _trace_failed(
                        http_trace_service, http_trace_context, trace_record,
                        error=exc, event_count=event_count, started_at=started_at,
                    )
                raise exc
            wrapped = exc if isinstance(exc, CodexSdkResponseError) else CodexSdkResponseError(
                _safe_sdk_failure_message(exc),
                sdk_error=exc,
                partial_output=output_projected or bool(getattr(exc, "partial_output", False)),
            )
            metadata = getattr(stream, "metadata", None)
            logger.error(
                "codex_sdk.responses.failed request_id=%s session_fingerprint=%s account_id=%s event_count=%d attempts=%s duration_ms=%d details=%s",
                short_identifier(getattr(metadata, "request_id", None)),
                getattr(metadata, "session_fingerprint", stable_fingerprint(conversation_id)),
                short_identifier(getattr(metadata, "current_account_id", None)),
                event_count,
                _attempt_diagnostics(getattr(metadata, "attempts", ())),
                int((time.perf_counter() - started_at) * 1000),
                sdk_error_diagnostic(getattr(wrapped, "sdk_error", None) or wrapped),
            )
            await _emit_stream_event(
                stream_event_handler,
                "llm.response.stream.failed",
                {
                    "event_count": event_count,
                    "error_type": wrapped.__class__.__name__,
                    "error_code": wrapped.code,
                    "partial_output": wrapped.partial_output,
                },
            )
            _trace_failed(
                http_trace_service, http_trace_context, trace_record,
                error=wrapped, event_count=event_count, started_at=started_at,
            )
            raise wrapped from None
        finally:
            if cancellation_task is not None:
                cancellation_task.cancel()
                await asyncio.gather(cancellation_task, return_exceptions=True)


def _build_standard_params(
    *,
    resolved_model: ResolvedLlmProviderModel,
    rendered_messages: OpenAIRenderResult,
    system_prompt: str | None,
    tools: tuple[dict[str, Any], ...],
    request_params: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    raw_params = dict(request_params)
    raw_model = str(raw_params.pop("model", resolved_model.model_alias) or resolved_model.model_alias)
    raw_params.pop("input", None)
    raw_params.pop("stream", None)
    unsupported = sorted(
        key for key, value in raw_params.items()
        if value is not None and key not in _SUPPORTED_STANDARD_PARAMS
    )
    if unsupported:
        raise CodexSdkResponseError(
            f"当前 louyi-codex 版本不支持这些标准 Responses 参数: {', '.join(unsupported)}"
        )
    params = {key: value for key, value in raw_params.items() if key in _SUPPORTED_STANDARD_PARAMS}
    params["store"] = False
    model = raw_model
    if model.endswith("-fast") and len(model) > len("-fast"):
        model = model[:-5]
        params.setdefault("service_tier", "priority")
    if rendered_messages.include:
        params.setdefault("include", rendered_messages.include)
    if rendered_messages.prompt_cache_key:
        params.setdefault("prompt_cache_key", rendered_messages.prompt_cache_key)
    if system_prompt:
        params.setdefault("instructions", system_prompt)
    if tools:
        params.setdefault("tools", list(tools))
        params.setdefault("parallel_tool_calls", True)
    params.setdefault("store", False)
    return model, params


async def _watch_cancellation(
    checker: CancellationChecker,
    source: Any,
    stream_getter: Callable[[], Any | None],
) -> None:
    try:
        while not checker():
            await asyncio.sleep(0.05)
        reason = "Runtime cancellation checker requested cancellation"
        source.cancel(reason)
        stream = stream_getter()
        if stream is not None:
            await stream.cancel(reason)
    except asyncio.CancelledError:
        return


def _event_to_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        value = event.model_dump(mode="json")
        return value if isinstance(value, dict) else {"type": getattr(event, "type", "message")}
    payload = getattr(event, "payload", None)
    if isinstance(payload, dict):
        return dict(payload)
    return {"type": getattr(event, "type", "message")}


def _response_projects_output(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    if isinstance(response.get("output_text"), str) and response["output_text"]:
        return True
    output = response.get("output")
    if not isinstance(output, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") in {"message", "reasoning", "function_call", "custom_tool_call"}
        for item in output
    )


def _event_projects_output(event_type: str, data: dict[str, Any]) -> bool:
    if _response_projects_output(data.get("response")):
        return True
    if event_type in {
        "response.output_text.delta",
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
        "response.refusal.delta",
        "response.function_call_arguments.delta",
        "response.custom_tool_call_input.delta",
    }:
        return bool(data.get("delta"))
    if event_type in {"response.function_call_arguments.done", "response.custom_tool_call_input.done"}:
        return bool(data.get("arguments") or data.get("input"))
    if event_type in {"response.output_text.done", "response.reasoning_summary_text.done", "response.reasoning_text.done"}:
        return bool(data.get("text"))
    if event_type == "response.refusal.done":
        return bool(data.get("refusal"))
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        item = data.get("item")
        return isinstance(item, dict) and item.get("type") in {"message", "reasoning", "function_call", "custom_tool_call"}
    return False


def _upsert_output_item(output_items: list[dict[str, Any]], item: dict[str, Any]) -> None:
    item_id = item.get("id")
    if item_id is not None:
        for index, current in enumerate(output_items):
            if current.get("id") == item_id:
                output_items[index] = item
                return
    output_items.append(item)


def _attempt_diagnostics(attempts: Any) -> list[dict[str, Any]]:
    return [
        {
            "attempt_id": short_identifier(attempt.attempt_id),
            "account_id": short_identifier(attempt.account_id),
            "kind": attempt.kind,
            "http_status": attempt.http_status,
            "upstream_request_id": short_identifier(attempt.upstream_request_id),
            "emitted_standard_event": attempt.emitted_standard_event,
            "duration_ms": (
                int((attempt.ended_at - attempt.started_at).total_seconds() * 1000)
                if attempt.ended_at is not None
                else None
            ),
        }
        for attempt in attempts or ()
    ]


async def _emit_stream_event(
    handler: StreamEventHandler | None, event_type: str, payload: dict[str, Any]
) -> None:
    if handler is None:
        return
    result = handler({"event_type": event_type, "payload": payload})
    if asyncio.iscoroutine(result):
        await result


def _trace_started(
    service: LlmHttpTraceService | None,
    context: LlmHttpTraceContext | None,
    *,
    model: str,
    params: dict[str, Any],
    rendered_messages: OpenAIRenderResult,
    conversation_id: str,
    tools: tuple[dict[str, Any], ...],
) -> Any:
    if service is None or context is None:
        return None
    shape = {
        "model": model,
        "stream": True,
        "standard_fields": sorted(params.keys()),
        "input_items": _item_count(rendered_messages.input),
        "tool_count": len(tools),
        "session_fingerprint": stable_fingerprint(conversation_id),
    }
    return service.record_started(
        context=context,
        method="PYTHON",
        url="louyi-codex://python-sdk/responses",
        request_headers={"session-id-fingerprint": stable_fingerprint(conversation_id) or ""},
        request_body_bytes=json.dumps(shape, ensure_ascii=False).encode("utf-8"),
        is_stream=True,
    )


def _trace_completed(
    service: LlmHttpTraceService | None,
    context: LlmHttpTraceContext | None,
    record: Any,
    *,
    metadata: Any,
    event_count: int,
    duration_ms: int,
) -> None:
    if service is None or context is None:
        return
    shape = {
        "status": "completed",
        "sdk_request_id": short_identifier(metadata.request_id),
        "account_id": short_identifier(metadata.current_account_id),
        "attempt_count": len(metadata.attempts),
        "event_count": event_count,
        "terminal_event_type": metadata.terminal_event_type,
    }
    service.record_completed(
        started_record=record,
        context=context,
        status_code=200,
        response_headers={},
        response_body_bytes=json.dumps(shape, ensure_ascii=False).encode("utf-8"),
        response_content_type="application/vnd.louyi-codex.diagnostic+json",
        duration_ms=duration_ms,
        sse_event_count=event_count,
    )


def _trace_failed(
    service: LlmHttpTraceService | None,
    context: LlmHttpTraceContext | None,
    record: Any,
    *,
    error: BaseException,
    event_count: int,
    started_at: float,
) -> None:
    if service is None or context is None:
        return
    service.record_failed(
        started_record=record,
        context=context,
        error_type=error.__class__.__name__,
        error_message=_safe_sdk_failure_message(error),
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        status_code=getattr(error, "status_code", None),
        response_headers={},
        response_body_bytes=json.dumps({
            "error_code": getattr(error, "code", None),
            "partial_output": bool(getattr(error, "partial_output", False)),
            "event_count": event_count,
        }, ensure_ascii=False).encode("utf-8"),
        response_content_type="application/vnd.louyi-codex.diagnostic+json",
        sse_event_count=event_count,
    )


def _item_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (list, tuple, dict)):
        return len(value)
    return 1


def _safe_sdk_failure_message(error: BaseException) -> str:
    code = getattr(error, "code", None)
    status = getattr(error, "http_status", None)
    partial = bool(getattr(error, "partial_output", False))
    return (
        f"louyi-codex request failed code={code or 'unknown'} "
        f"http_status={status!r} partial_output={partial}"
    )


def _sdk_module() -> Any:
    try:
        import louyi_codex
    except ImportError:
        raise CodexSdkResponseError(
            "未安装 louyi-codex SDK；请先从当前 SDK 源码构建并安装 wheel。",
        ) from None
    return louyi_codex

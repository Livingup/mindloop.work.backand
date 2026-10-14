# -*- coding: utf-8 -*-
"""Standard OpenAI Responses bridge to the process-owned louyi-codex SDK."""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from contextlib import suppress
from typing import Any, Callable, Mapping

from agent_runtime.codex_oauth.diagnostics import (
    safe_sdk_error_message,
    sdk_error_projection,
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
from agent_runtime.llm_http_trace.codex_sdk import CodexSdkHttpTraceSink
from agent_runtime.llm_http_trace.models import LlmHttpTraceContext
from agent_runtime.llm_http_trace.service import LlmHttpTraceService
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    ResolvedLlmProviderModel,
 )

logger = logging.getLogger("agent_runtime.codex_sdk")

_RUNTIME_OWNED_OR_PRIVATE_PARAMS = frozenset({
    "request_options",
    "extra_body",
    "account",
    "account_id",
    "accounts",
    "account_ids",
    "account_list",
    "account_pool",
    "candidate",
    "candidates",
    "route",
    "route_id",
    "routing",
    "routing_strategy",
    "proxy",
    "proxy_url",
    "proxy_config",
    "auth_proxy",
    "auth_proxy_url",
    "account_defaults",
    "authorization",
    "authorization_header",
    "chatgpt_account",
    "chatgpt_account_id",
    "headers",
    "header",
    "custom_headers",
    "request_headers",
    "http_headers",
    "private_headers",
    "private_request_headers",
    "body",
    "request_body",
    "private_body",
    "private_request_body",
    "api_key",
    "api_keys",
    "access_token",
    "refresh_token",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "client_metadata",
})


class CodexSdkResponseError(LlmProviderResponseError):
    def __init__(
        self,
        message: str,
        *,
        sdk_error: BaseException | None = None,
        partial_output: bool = False,
        stage: str = "responses_request",
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.sdk_error = sdk_error
        self.partial_output = partial_output or bool(getattr(sdk_error, "partial_output", False))
        self.retryable = bool(getattr(sdk_error, "retryable", False))
        self.status_code = getattr(sdk_error, "http_status", None)
        self.code = code or getattr(sdk_error, "code", "codex_sdk_response_error")
        self.details = details
        self.diagnostic = sdk_error_projection(
            self,
            stage=stage,
            partial_output=self.partial_output,
        )
        self.args = (_with_safe_validation_context(message, self.diagnostic),)


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
        try:
            if resolved_model.provider_id != CODEX_PROVIDER_ID:
                raise CodexSdkResponseError(
                    f"codex_responses 仅保留给托管 Provider {CODEX_PROVIDER_ID}。",
                    stage="responses_preflight",
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
            trace_sink = (
                CodexSdkHttpTraceSink(
                    service=http_trace_service,
                    context=http_trace_context,
                )
                if (
                    http_trace_service is not None
                    and http_trace_context is not None
                    and http_trace_service.enabled
                )
                else None
            )
            options_kwargs: dict[str, Any] = {
                "headers": _codex_request_headers(resolved_model.custom_headers, conversation_id),
                "cancel_token": cancellation_source.token,
            }
            if trace_sink is not None:
                options_kwargs.update({
                    "raw_event_sink": trace_sink,
                    "raw_event_mode": sdk.RawEventMode.ENABLED,
                })
            options = sdk.RequestOptions(**options_kwargs)
        except Exception as exc:
            wrapped = exc if isinstance(exc, CodexSdkResponseError) else CodexSdkResponseError(
                _safe_sdk_failure_message(exc),
                sdk_error=exc,
                stage="responses_preflight",
            )
            diagnostic = sdk_error_projection(wrapped, stage="responses_preflight")
            wrapped.diagnostic = diagnostic
            await _emit_stream_event(
                stream_event_handler,
                "llm.response.stream.failed",
                {
                    "event_count": 0,
                    "error_type": diagnostic["error_type"],
                    "error_code": diagnostic["code"],
                    "partial_output": False,
                    "diagnostic": diagnostic,
                },
            )
            raise wrapped from None
        started_at = time.perf_counter()
        event_count = 0
        output_projected = False
        output_items: list[dict[str, Any]] = []
        response_body: dict[str, Any] | None = None
        stream: Any | None = None
        cancellation_task: asyncio.Task[None] | None = None
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
                            safe_sdk_error_message("protocol_error"),
                            partial_output=output_projected,
                            stage="responses_stream",
                            code="protocol_error",
                            details={"reason_code": event_type},
                        )
            outcome = await stream.wait_closed()
            metadata = stream.metadata
            if outcome.status != "completed" or response_body is None:
                raise CodexSdkResponseError(
                    safe_sdk_error_message("protocol_error"),
                    partial_output=output_projected,
                    stage="responses_stream",
                    code="protocol_error",
                    details={"reason_code": str(outcome.status)},
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
            return response_body
        except asyncio.CancelledError:
            cancellation_source.cancel("Runtime LLM request task cancelled")
            if stream is not None:
                with suppress(Exception, asyncio.CancelledError):
                    await stream.cancel("Runtime LLM request task cancelled")
            if cancellation_checker is not None and cancellation_checker():
                raise LlmRequestCancelled("LLM request cancelled.") from None
            raise
        except Exception as exc:
            if isinstance(exc, getattr(sdk, "RequestCancelledError", ())):
                if cancellation_checker is not None and cancellation_checker():
                    exc = LlmRequestCancelled("LLM request cancelled.")
            if isinstance(exc, LlmRequestCancelled):
                raise exc
            wrapped = exc if isinstance(exc, CodexSdkResponseError) else CodexSdkResponseError(
                _safe_sdk_failure_message(exc),
                sdk_error=exc,
                partial_output=output_projected or bool(getattr(exc, "partial_output", False)),
                stage="responses_request",
            )
            metadata = getattr(stream, "metadata", None)
            attempts = tuple(getattr(metadata, "attempts", ()) or ())
            last_attempt = attempts[-1] if attempts else None
            diagnostic = sdk_error_projection(
                wrapped,
                stage="responses_request",
                request_id=getattr(metadata, "request_id", None),
                attempt_id=getattr(last_attempt, "attempt_id", None),
                account_id=getattr(metadata, "current_account_id", None),
                upstream_request_id=getattr(last_attempt, "upstream_request_id", None),
                partial_output=wrapped.partial_output,
            )
            wrapped.diagnostic = diagnostic
            logger.error(
                "codex_sdk.responses.failed request_id=%s session_fingerprint=%s account_id=%s event_count=%d attempts=%s duration_ms=%d details=%s",
                short_identifier(getattr(metadata, "request_id", None)),
                getattr(metadata, "session_fingerprint", stable_fingerprint(conversation_id)),
                short_identifier(getattr(metadata, "current_account_id", None)),
                event_count,
                _attempt_diagnostics(attempts),
                int((time.perf_counter() - started_at) * 1000),
                diagnostic,
            )
            await _emit_stream_event(
                stream_event_handler,
                "llm.response.stream.failed",
                {
                    "event_count": event_count,
                    "error_type": diagnostic["error_type"],
                    "error_code": diagnostic["code"],
                    "partial_output": wrapped.partial_output,
                    "diagnostic": diagnostic,
                },
            )
            raise wrapped from None
        finally:
            if cancellation_task is not None:
                cancellation_task.cancel()
                await asyncio.gather(cancellation_task, return_exceptions=True)
            if trace_sink is not None:
                try:
                    trace_sink.flush()
                except Exception:
                    logger.exception(
                        "codex_sdk.http_trace_final_flush_unhandled request_id=%s",
                        getattr(http_trace_context, "llm_request_id", None),
                    )


def _codex_request_headers(custom_headers: Mapping[str, str] | None, conversation_id: str) -> dict[str, str]:
    """Merge user headers while keeping the Runtime-owned session header authoritative."""
    headers: dict[str, str] = {}
    for name, value in (custom_headers or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise CodexSdkResponseError(
                safe_sdk_error_message("validation_error"),
                stage="responses_preflight",
                code="validation_error",
                details={"field": "custom_headers", "reason_code": "header_name_and_value_must_be_text"},
            )
        if name.strip().lower() == CODEX_SESSION_HEADER_NAME.lower():
            raise CodexSdkResponseError(
                safe_sdk_error_message("reserved_response_field"),
                stage="responses_preflight",
                code="reserved_response_field",
                details={"field": name, "reason_code": "runtime_owned_session_header"},
            )
        headers[name] = value
    headers[CODEX_SESSION_HEADER_NAME] = conversation_id
    return headers



def _build_standard_params(
    *,
    resolved_model: ResolvedLlmProviderModel,
    rendered_messages: OpenAIRenderResult,
    system_prompt: str | None,
    tools: tuple[dict[str, Any], ...],
    request_params: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    params = {"max_output_tokens": resolved_model.max_output_tokens, **request_params}
    for field_name in ("model", "input"):
        if field_name in params:
            raise CodexSdkResponseError(
                safe_sdk_error_message("reserved_response_field"),
                stage="responses_request",
                code="reserved_response_field",
                details={"field": field_name},
            )
    if "stream" in params:
        stream = params.pop("stream")
        if stream is not True:
            raise CodexSdkResponseError(
                safe_sdk_error_message("unsupported_response_parameter"),
                stage="responses_request",
                code="unsupported_response_parameter",
                details={"parameter": "stream", "reason_code": "stream_required"},
            )
    for field_name in params:
        if not isinstance(field_name, str):
            raise CodexSdkResponseError(
                safe_sdk_error_message("unsupported_response_parameter"),
                stage="responses_request",
                code="unsupported_response_parameter",
                details={"reason_code": "parameter_name_must_be_string"},
            )
        normalized = field_name.strip().lower().replace("-", "_")
        if normalized in _RUNTIME_OWNED_OR_PRIVATE_PARAMS:
            raise CodexSdkResponseError(
                safe_sdk_error_message("reserved_response_field"),
                stage="responses_request",
                code="reserved_response_field",
                details={"field": normalized},
            )
    params["store"] = False
    params.pop("previous_response_id", None)
    params["include"] = _merge_stateless_include(
        rendered_messages.include,
        params.get("include"),
    )
    if rendered_messages.prompt_cache_key:
        params["prompt_cache_key"] = rendered_messages.prompt_cache_key
    if system_prompt:
        params.setdefault("instructions", system_prompt)
    if tools:
        params.setdefault("tools", list(tools))
        params.setdefault("parallel_tool_calls", True)
    return str(resolved_model.model_alias), params


def _merge_stateless_include(rendered_value: Any, requested_value: Any) -> list[Any]:
    """Merge include values without splitting strings and retain stateless reasoning metadata."""
    def normalize(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return list(value)
        if isinstance(value, tuple):
            return list(value)
        return [value]

    items = normalize(rendered_value)
    required_item = "reasoning.encrypted_content"
    if required_item not in items:
        items.append(required_item)
    for item in normalize(requested_value):
        if item not in items:
            items.append(item)
    return items


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


def _item_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (list, tuple, dict)):
        return len(value)
    return 1


def _with_safe_validation_context(message: str, diagnostic: dict[str, Any]) -> str:
    if diagnostic.get("code") != "validation_error":
        return message
    details = diagnostic.get("details")
    if not isinstance(details, dict):
        return message
    parts: list[str] = []
    field = details.get("field")
    if isinstance(field, str) and field:
        parts.append(f"field={field}")
    path = details.get("path")
    if isinstance(path, list) and path:
        path_text = ".".join(str(item) for item in path if isinstance(item, (str, int)) and not isinstance(item, bool))
        if path_text:
            parts.append(f"path={path_text}")
    validator = details.get("validator")
    if isinstance(validator, str) and validator:
        parts.append(f"validator={validator}")
    if not parts:
        return message
    return f"{message} Validation context: {', '.join(parts)}."


def _safe_sdk_failure_message(error: BaseException) -> str:
    return sdk_error_projection(
        error,
        stage="responses_request",
        partial_output=bool(getattr(error, "partial_output", False)),
    )["message"]


def _sdk_module() -> Any:
    try:
        import louyi_codex
    except ImportError:
        raise CodexSdkResponseError(
            "未安装 louyi-codex SDK；请先从当前 SDK 源码构建并安装 wheel。",
        ) from None
    return louyi_codex

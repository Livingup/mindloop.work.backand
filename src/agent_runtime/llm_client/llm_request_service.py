# -*- coding: utf-8 -*-
"""通用 LLM 请求领域服务。"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import time
import uuid
from dataclasses import replace
from typing import Any, Callable

from agent_runtime.conversation_message.message.operations import (
    ingest_response,
    render_messages,
)
from agent_runtime.conversation_message.message.models import ThinkingPart, ToolUsePart
from agent_runtime.communication.runtime_event_publisher import (
    NoopRuntimeEventPublisher,
    RuntimeEventPublisher,
)
from agent_runtime.llm_api_protocol import (
    API_FORMAT_ANTHROPIC_MESSAGES,
    API_FORMAT_CODEX_RESPONSES,
    API_FORMAT_GEMINI_GENERATE_CONTENT,
    API_FORMAT_OPENAI_CHAT_COMPLETIONS,
    API_FORMAT_OPENAI_RESPONSES,
    API_FORMAT_ZHIPU_CHAT_COMPLETIONS,
)
from agent_runtime.llm_client.llm_protocol_clients import (
    LlmHttpRequestError,
    LlmProviderResponseError,
    LlmProtocolClient,
    LlmRequestCancelled,
)
from agent_runtime.llm_client.llm_request_models import (
    LlmPromptRequest,
    LlmPromptResponse,
)
from agent_runtime.llm_client.llm_stream_events import LlmSemanticStreamParser
from agent_runtime.llm_http_trace.models import LlmHttpTraceContext
from agent_runtime.llm_http_trace.service import LlmHttpTraceService

_ENV_VAR_PATTERN = re.compile(r"^\$\{([^}:]+)(?::-([^}]*))?\}$")
_SUPPRESSED_FRONTEND_SEMANTIC_EVENTS = {
    "llm.response.completed",
    "llm.response.failed",
    "llm.thinking.delta",
    "llm.tool_call.arguments.delta",
    "llm.tool_call.arguments.completed",
}


class LlmRequestService:
    """通用 LLM 请求服务。

    该服务负责一次 LLM 调用的公共编排：
    - 使用 conversation_message 渲染目标 api_format 的消息体；
    - 合并 LLM 注册模块中的 custom_request_params / thinking / tool_choice；
    - 按 provider/binding 的 API Key 列表轮换请求；
    - 按 retry 策略处理临时 HTTP 错误；
    - 将成功响应通过 ingest_response 写回统一 Conversation。
    """

    def __init__(
        self,
        protocol_clients: tuple[LlmProtocolClient, ...],
        resource_resolver: Any,
        event_publisher: RuntimeEventPublisher | None = None,
        http_trace_service: LlmHttpTraceService | None = None,
    ) -> None:
        # 具体 API 协议客户端表。key 是 api_format，如 openai_responses。
        self._protocol_clients = {client.api_format: client for client in protocol_clients}

        # 多模态资源解析器。文本对话不会触发资源读取；图片/PDF 渲染时才会使用。
        self._resource_resolver = resource_resolver

        # 运行时事件发布器。用于把 LLM 请求关键节点推送到通用通信流。
        self._event_publisher = event_publisher or NoopRuntimeEventPublisher()

        self._http_trace_service = http_trace_service

    async def send_prompt(self, request: LlmPromptRequest) -> LlmPromptResponse:
        """发送一次 LLM 请求，并把 assistant 响应追加到 Conversation。"""
        resolved = _with_default_relay_session_header(request)
        client = self._protocol_clients.get(resolved.api_format)
        if client is None:
            raise ValueError(
                f"当前 LLM 请求客户端不支持 api_format={resolved.api_format}。"
            )

        request_params = _merge_request_params(request)
        render_options = {
            **request.render_options,
            "request_params": request_params,
            "supported_input_modalities": resolved.supported_input_modalities,
        }
        rendered_messages = await render_messages(
            request.conversation,
            resolved.api_format,
            self._resource_resolver,
            render_options,
        )
        tool_schemas = tuple(tool.schema for tool in request.tools)
        api_key_items = list(resolved.api_keys)
        api_keys: list[str | None] = [item.key for item in api_key_items]
        if not api_keys:
            api_keys = [None]

        llm_call_id = request.llm_call_id or f"llm-call-{uuid.uuid4().hex}"
        call_started_at = time.perf_counter()
        attempt_count = 0
        last_error: Exception | None = None
        last_service_call_error: Exception | None = None
        for key_index, raw_api_key in enumerate(api_keys):
            api_key_name = api_key_items[key_index].name if key_index < len(api_key_items) else None
            request_attempt_started_at = time.perf_counter()
            attempt_count += 1
            retry_index = 0
            llm_request_id = _build_llm_request_id(
                llm_call_id=llm_call_id,
                candidate_index=request.candidate_index,
                api_key_index=key_index,
                retry_index=retry_index,
            )
            api_key_label = _api_key_label(raw_api_key, api_key_name)
            try:
                api_key = _resolve_api_key(raw_api_key) if raw_api_key is not None else None
                api_key_label = _api_key_label(api_key, api_key_name)
            except Exception as exc:
                last_error = exc
                request_payload = _build_request_attempt_payload(
                    llm_call_id=llm_call_id,
                    llm_request_id=llm_request_id,
                    resolved=resolved,
                    candidate_index=request.candidate_index,
                    api_key_index=key_index,
                    api_key_label=api_key_label,
                    retry_index=retry_index,
                    attempt_count=attempt_count,
                    tool_count=len(tool_schemas),
                    api_key_count=len(api_keys),
                )
                await self._event_publisher.publish(
                    request.communication_context,
                    "llm.request.started",
                    request_payload,
                    source="llm_client",
                    metadata={"conversation_id": request.conversation.id},
                )
                retry_target = _build_retry_target_payload(
                    resolved=resolved,
                    candidate_index=request.candidate_index,
                    api_key_index=key_index,
                    retry_index=retry_index,
                    max_retries=0,
                    api_key_count=len(api_keys),
                    next_resolved_model=request.next_resolved_model,
                    retryable=False,
                )
                await self._event_publisher.publish(
                    request.communication_context,
                    "llm.request.failed",
                    {
                        **request_payload,
                        "attempt_count": attempt_count,
                        "error_type": exc.__class__.__name__,
                        "error_message": str(exc),
                        "failed_stage": "api_key_resolution",
                        "partial_response": False,
                        "duration_ms": int((time.perf_counter() - request_attempt_started_at) * 1000),
                        **retry_target,
                    },
                    source="llm_client",
                    metadata={"conversation_id": request.conversation.id},
                )
                continue
            request_payload = _build_request_attempt_payload(
                llm_call_id=llm_call_id,
                llm_request_id=llm_request_id,
                resolved=resolved,
                candidate_index=request.candidate_index,
                api_key_index=key_index,
                api_key_label=api_key_label,
                retry_index=retry_index,
                attempt_count=attempt_count,
                tool_count=len(tool_schemas),
                api_key_count=len(api_keys),
            )
            await self._event_publisher.publish(
                request.communication_context,
                "llm.request.started",
                request_payload,
                source="llm_client",
                metadata={"conversation_id": request.conversation.id},
            )
            if request.request_attempt_observer is not None:
                request.request_attempt_observer(dict(request_payload))
            max_retries = resolved.retry.max_retries_per_key
            for retry_index in range(max_retries + 1):
                if retry_index > 0:
                    request_attempt_started_at = time.perf_counter()
                    attempt_count += 1
                    llm_request_id = _build_llm_request_id(
                        llm_call_id=llm_call_id,
                        candidate_index=request.candidate_index,
                        api_key_index=key_index,
                        retry_index=retry_index,
                    )
                    request_payload = _build_request_attempt_payload(
                        llm_call_id=llm_call_id,
                        llm_request_id=llm_request_id,
                        resolved=resolved,
                        candidate_index=request.candidate_index,
                        api_key_index=key_index,
                        api_key_label=api_key_label,
                        retry_index=retry_index,
                        attempt_count=attempt_count,
                        tool_count=len(tool_schemas),
                        api_key_count=len(api_keys),
                    )
                    await self._event_publisher.publish(
                        request.communication_context,
                        "llm.request.started",
                        request_payload,
                        source="llm_client",
                        metadata={"conversation_id": request.conversation.id},
                    )
                    if request.request_attempt_observer is not None:
                        request.request_attempt_observer(dict(request_payload))
                stream_state: dict[str, bool] = {"response_started": False, "response_failed": False}
                api_response_received = False
                messages_snapshot = len(request.conversation.messages)
                provider_state_snapshot = copy.deepcopy(request.conversation.provider_state)
                conversation_state_snapshot = copy.deepcopy(request.conversation.state)
                try:
                    stream_event_handler = _build_stream_event_handler(
                        publisher=self._event_publisher,
                        context=request.communication_context,
                        event_loop=asyncio.get_running_loop(),
                        conversation_id=request.conversation.id,
                        llm_call_id=llm_call_id,
                        llm_request_id=llm_request_id,
                        provider_id=resolved.provider_id,
                        binding_id=resolved.binding_id,
                        model_id=resolved.model_id,
                        model_alias=resolved.model_alias,
                        api_format=resolved.api_format,
                        candidate_index=request.candidate_index,
                        api_key_index=key_index,
                        api_key_label=api_key_label,
                        retry_index=retry_index,
                        attempt_count=attempt_count,
                        stream_state=stream_state,
                    )
                    raw_response = await client.request(
                        resolved_model=resolved,
                        rendered_messages=rendered_messages,
                        system_prompt=request.system_prompt,
                        tools=tool_schemas,
                        request_params=request_params,
                        api_key=api_key,
                        stream_event_handler=stream_event_handler,
                        http_trace_service=self._http_trace_service,
                        http_trace_context=_build_http_trace_context(
                            request=request,
                            resolved=resolved,
                            llm_call_id=llm_call_id,
                            llm_request_id=llm_request_id,
                            api_key_index=key_index,
                            retry_index=retry_index,
                            attempt_count=attempt_count,
                        ),
                        cancellation_checker=request.cancellation_checker,
                    )
                    api_response_received = True
                    if not stream_state.get("response_started"):
                        stream_state["response_started"] = True
                        await self._event_publisher.publish(
                            request.communication_context,
                            "llm.response.started",
                            {**request_payload, "content_type": None},
                            source="llm_client",
                            metadata={"conversation_id": request.conversation.id},
                        )
                    _apply_render_side_effects(
                        request.conversation,
                        resolved.api_format,
                        rendered_messages,
                    )
                    assistant_message = ingest_response(
                        request.conversation,
                        resolved.api_format,
                        raw_response,
                        {**render_options, "vendor": resolved.provider_id},
                    )
                    _ensure_complete_assistant_message(assistant_message)
                    token_usage = _extract_token_usage(resolved.api_format, raw_response)
                except LlmRequestCancelled as exc:
                    _restore_conversation_snapshot(
                        request.conversation,
                        messages_snapshot,
                        provider_state_snapshot,
                        conversation_state_snapshot,
                    )
                    await self._event_publisher.publish(
                        request.communication_context,
                        "llm.request.cancelled",
                        {
                            **request_payload,
                            "attempt_count": attempt_count,
                            "cancelled_by": "user",
                            "cancelled_stage": "response_stream" if stream_state.get("response_started") else "before_response",
                            "partial_response": stream_state.get("response_started") is True,
                            "duration_ms": int((time.perf_counter() - request_attempt_started_at) * 1000),
                            "reason": str(exc) or "LLM request cancelled.",
                        },
                        source="llm_client",
                        metadata={"conversation_id": request.conversation.id},
                    )
                    raise
                except Exception as exc:  # noqa: BLE001 - 需要统一处理不同 SDK/HTTP 异常
                    _restore_conversation_snapshot(
                        request.conversation,
                        messages_snapshot,
                        provider_state_snapshot,
                        conversation_state_snapshot,
                    )
                    service_failure = (
                        api_response_received
                        or stream_state.get("response_started") is True
                        or _is_llm_service_call_failure(exc)
                    )
                    if not service_failure:
                        raise
                    service_error = _as_llm_service_failure(exc)
                    last_error = service_error
                    last_service_call_error = service_error
                    retry_target = _build_retry_target_payload(
                        resolved=resolved,
                        candidate_index=request.candidate_index,
                        api_key_index=key_index,
                        retry_index=retry_index,
                        max_retries=max_retries,
                        api_key_count=len(api_keys),
                        next_resolved_model=request.next_resolved_model,
                        retryable=_should_retry(service_error, resolved.retry.retryable_status_codes),
                    )
                    await self._event_publisher.publish(
                        request.communication_context,
                        "llm.request.failed",
                        {
                            **request_payload,
                            "attempt_count": attempt_count,
                            "error_type": service_error.__class__.__name__,
                            "error_message": str(service_error),
                            "failed_stage": "response_stream" if stream_state.get("response_started") else "before_response",
                            "partial_response": stream_state.get("response_started") is True,
                            "duration_ms": int((time.perf_counter() - request_attempt_started_at) * 1000),
                            **retry_target,
                        },
                        source="llm_client",
                        metadata={"conversation_id": request.conversation.id},
                    )
                    if not _should_retry(service_error, resolved.retry.retryable_status_codes):
                        break
                    if retry_index >= max_retries:
                        break
                    await asyncio.sleep(_retry_delay(resolved.retry.initial_delay_seconds, resolved.retry.max_delay_seconds, retry_index))
                else:
                    await self._event_publisher.publish(
                        request.communication_context,
                        "llm.response.completed",
                        {
                            **request_payload,
                            "assistant_part_count": len(assistant_message.content),
                            "thinking_part_count": _count_parts(assistant_message.content, ThinkingPart),
                            "tool_use_part_count": _count_parts(assistant_message.content, ToolUsePart),
                            "duration_ms": int((time.perf_counter() - request_attempt_started_at) * 1000),
                            "call_duration_ms": int((time.perf_counter() - call_started_at) * 1000),
                            "token_usage": token_usage,
                            "context_window_max": resolved.context_window,
                        },
                        source="llm_client",
                        metadata={"conversation_id": request.conversation.id},
                    )
                    return LlmPromptResponse(
                        raw_response=raw_response,
                        assistant_message=assistant_message,
                        api_key_index=key_index,
                        attempt_count=attempt_count,
                        token_usage=token_usage,
                        context_window_max=resolved.context_window,
                        provider_id=resolved.provider_id,
                        binding_id=resolved.binding_id,
                        model_id=resolved.model_id,
                        model_alias=resolved.model_alias,
                    )
        if last_error is not None:
            if request.next_resolved_model is None:
                await self._event_publisher.publish(
                    request.communication_context,
                    "llm.call.failed",
                    {
                        "llm_call_id": llm_call_id,
                        "candidate_index": request.candidate_index,
                        "attempt_count": attempt_count,
                        "error_type": last_error.__class__.__name__,
                        "error_message": str(last_error),
                        "duration_ms": int((time.perf_counter() - call_started_at) * 1000),
                    },
                    source="llm_client",
                    metadata={"conversation_id": request.conversation.id},
                )
            if last_service_call_error is not None or request.next_resolved_model is not None:
                cause = last_service_call_error or last_error
                raise LlmProviderModelServiceUnavailable(
                    provider_id=resolved.provider_id,
                    binding_id=resolved.binding_id,
                    attempt_count=attempt_count,
                    cause=cause,
                ) from cause
            raise last_error
        raise RuntimeError("LLM 请求未执行。")


class LlmProviderModelServiceUnavailable(RuntimeError):
    def __init__(self, *, provider_id: str, binding_id: str, attempt_count: int, cause: Exception) -> None:
        self.provider_id = provider_id
        self.binding_id = binding_id
        self.attempt_count = attempt_count
        self.cause = cause
        super().__init__(
            f"LLM 服务商模型调用未成功响应: provider={provider_id}, binding={binding_id}, "
            f"attempt_count={attempt_count}, cause={cause.__class__.__name__}: {cause}"
        )


def _merge_request_params(request: LlmPromptRequest) -> dict[str, Any]:
    """合并 registry 自定义 JSON 字段和本轮覆盖参数。"""
    resolved = request.resolved_model
    merged: dict[str, Any] = {}
    _merge_json_field(merged, resolved.custom_request_params, "custom_request_params")
    _merge_json_field(merged, resolved.thinking, "thinking")
    _merge_json_field(merged, resolved.tool_choice, "tool_choice")
    merged.update(request.request_overrides)
    merged["stream"] = True
    return merged


def _with_default_relay_session_header(request: LlmPromptRequest):
    headers = dict(request.resolved_model.custom_headers)
    if not any(key.lower() == "x-relay-session-id" for key in headers):
        headers["x-relay-session-id"] = request.conversation.id
    return replace(request.resolved_model, custom_headers=headers)


def _build_llm_request_id(
    *,
    llm_call_id: str,
    candidate_index: int,
    api_key_index: int,
    retry_index: int,
) -> str:
    return f"{llm_call_id}:candidate-{candidate_index}:key-{api_key_index}:retry-{retry_index}"


def _build_request_attempt_payload(
    *,
    llm_call_id: str,
    llm_request_id: str,
    resolved: Any,
    candidate_index: int,
    api_key_index: int,
    api_key_label: str | None,
    retry_index: int,
    attempt_count: int,
    tool_count: int,
    api_key_count: int,
) -> dict[str, Any]:
    return {
        "llm_call_id": llm_call_id,
        "llm_request_id": llm_request_id,
        "candidate_index": candidate_index,
        "provider_id": resolved.provider_id,
        "binding_id": resolved.binding_id,
        "model_id": resolved.model_id,
        "model_alias": resolved.model_alias,
        "api_format": resolved.api_format,
        "api_key_index": api_key_index,
        "api_key_label": api_key_label,
        "retry_index": retry_index,
        "attempt_count": attempt_count,
        "tool_count": tool_count,
        "api_key_count": api_key_count,
    }


def _build_retry_target_payload(
    *,
    resolved: Any,
    candidate_index: int,
    api_key_index: int,
    retry_index: int,
    max_retries: int,
    api_key_count: int,
    next_resolved_model: Any | None,
    retryable: bool,
) -> dict[str, Any]:
    retry_same_key = retryable and retry_index < max_retries
    retry_next_key = not retry_same_key and api_key_index < api_key_count - 1
    retry_next_candidate = not retry_same_key and not retry_next_key and next_resolved_model is not None
    payload: dict[str, Any] = {
        "retryable": retryable,
        "will_retry": retry_same_key or retry_next_key or retry_next_candidate,
    }
    payload["final"] = not payload["will_retry"]
    if retry_same_key:
        payload.update({
            "next_candidate_index": candidate_index,
            "next_provider_id": resolved.provider_id,
            "next_binding_id": resolved.binding_id,
            "next_model_id": resolved.model_id,
            "next_model_alias": resolved.model_alias,
            "next_api_format": resolved.api_format,
            "next_api_key_index": api_key_index,
            "next_retry_index": retry_index + 1,
        })
    elif retry_next_key:
        payload.update({
            "next_candidate_index": candidate_index,
            "next_provider_id": resolved.provider_id,
            "next_binding_id": resolved.binding_id,
            "next_model_id": resolved.model_id,
            "next_model_alias": resolved.model_alias,
            "next_api_format": resolved.api_format,
            "next_api_key_index": api_key_index + 1,
            "next_retry_index": 0,
        })
    elif retry_next_candidate:
        payload.update({
            "next_candidate_index": candidate_index + 1,
            "next_provider_id": next_resolved_model.provider_id,
            "next_binding_id": next_resolved_model.binding_id,
            "next_model_id": next_resolved_model.model_id,
            "next_model_alias": next_resolved_model.model_alias,
            "next_api_format": next_resolved_model.api_format,
            "next_api_key_index": 0,
            "next_retry_index": 0,
        })
    return payload


def _build_http_trace_context(
    *,
    request: LlmPromptRequest,
    resolved: Any,
    llm_call_id: str,
    llm_request_id: str,
    api_key_index: int,
    retry_index: int,
    attempt_count: int,
) -> LlmHttpTraceContext:
    communication_context = request.communication_context
    metadata = communication_context.metadata if communication_context is not None else {}
    return LlmHttpTraceContext(
        llm_request_id=llm_request_id,
        llm_call_id=llm_call_id,
        conversation_id=request.conversation.id,
        task_id=_text_or_none(metadata.get("task_id")),
        instance_id=_text_or_none(metadata.get("agent_instance_id")),
        trace_id=communication_context.trace_id if communication_context is not None else None,
        provider_id=resolved.provider_id,
        binding_id=resolved.binding_id,
        model_id=resolved.model_id,
        model_alias=resolved.model_alias,
        api_format=resolved.api_format,
        candidate_index=request.candidate_index,
        api_key_index=api_key_index,
        retry_index=retry_index,
        attempt_count=attempt_count,
    )


def _text_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _merge_json_field(target: dict[str, Any], json_text: str | None, fallback_key: str) -> None:
    """把用户自定义 JSON 字符串合并到请求参数。"""
    if json_text is None:
        return
    value = json.loads(json_text)
    if isinstance(value, dict):
        target.update(value)
    else:
        target[fallback_key] = value


def _build_stream_event_handler(
    *,
    publisher: RuntimeEventPublisher,
    context: Any,
    event_loop: asyncio.AbstractEventLoop,
    conversation_id: str,
    llm_call_id: str,
    llm_request_id: str,
    provider_id: str,
    binding_id: str,
    model_id: str,
    model_alias: str,
    api_format: str,
    candidate_index: int,
    api_key_index: int,
    api_key_label: str | None,
    retry_index: int,
    attempt_count: int,
    stream_state: dict[str, bool],
) -> Callable[[dict[str, Any]], None]:
    semantic_parser = LlmSemanticStreamParser(api_format)

    def handle(event: dict[str, Any]) -> None:
        event_type = str(event.get("event_type") or "llm.response.stream.event")
        event_payload = dict(event.get("payload") or {})
        base_payload = {
            "llm_call_id": llm_call_id,
            "llm_request_id": llm_request_id,
            "candidate_index": candidate_index,
            "provider_id": provider_id,
            "binding_id": binding_id,
            "model_id": model_id,
            "model_alias": model_alias,
            "api_format": api_format,
            "api_key_index": api_key_index,
            "api_key_label": api_key_label,
            "retry_index": retry_index,
            "attempt_count": attempt_count,
        }
        events_to_publish: list[tuple[str, dict[str, Any]]] = []
        if event_type == "llm.response.stream.started":
            if not stream_state.get("response_started"):
                stream_state["response_started"] = True
                events_to_publish.append(("llm.response.started", {**base_payload, "content_type": event_payload.get("content_type")}))
        elif event_type == "llm.response.stream.event":
            semantic_events = semantic_parser.parse_provider_event(
                event_name=str(event_payload.get("event") or "message"),
                data=event_payload.get("data") if isinstance(event_payload.get("data"), dict) else None,
                done=event_payload.get("done") is True,
            )
            events_to_publish.extend(
                (semantic.event_type, {**base_payload, **semantic.payload})
                for semantic in semantic_events
                if semantic.event_type not in _SUPPRESSED_FRONTEND_SEMANTIC_EVENTS
            )
        elif event_type == "llm.response.stream.failed":
            stream_state["response_failed"] = True
        elif event_type == "llm.response.stream.completed":
            semantic_events = semantic_parser.parse_provider_event(
                event_name="stream.completed",
                data=None,
                done=True,
            )
            events_to_publish.extend(
                (semantic.event_type, {**base_payload, **semantic.payload})
                for semantic in semantic_events
                if semantic.event_type not in _SUPPRESSED_FRONTEND_SEMANTIC_EVENTS
            )
        for publish_event_type, publish_payload in events_to_publish:
            future = asyncio.run_coroutine_threadsafe(
                publisher.publish(
                    context,
                    publish_event_type,
                    publish_payload,
                    source="llm_client",
                    metadata={"conversation_id": conversation_id},
                ),
                event_loop,
            )
            try:
                future.result()
            except Exception:
                pass

    return handle


def _apply_render_side_effects(conversation: Any, api_format: str, rendered_messages: Any) -> None:
    """把部分 render 结果中的会话状态写回 Conversation。"""
    applied = getattr(rendered_messages, "applied_cache_breakpoint_indexes", None)
    if applied is not None:
        conversation.provider_state[api_format] = {
            **(conversation.provider_state.get(api_format) or {}),
            "cache_breakpoint_indexes": list(applied),
        }
    last_claude_assistant_message_index = getattr(
        rendered_messages,
        "last_claude_assistant_message_index",
        None,
    )
    if last_claude_assistant_message_index is not None:
        conversation.provider_state[api_format] = {
            **(conversation.provider_state.get(api_format) or {}),
            "last_claude_assistant_message_index": last_claude_assistant_message_index,
        }


def _restore_conversation_snapshot(
    conversation: Any,
    messages_snapshot: int,
    provider_state_snapshot: dict[str, Any],
    conversation_state_snapshot: Any,
) -> None:
    del conversation.messages[messages_snapshot:]
    conversation.provider_state.clear()
    conversation.provider_state.update(provider_state_snapshot)
    conversation.state = conversation_state_snapshot


def _ensure_complete_assistant_message(assistant_message: Any) -> None:
    if getattr(assistant_message, "role", None) != "assistant":
        raise LlmProviderResponseError("LLM 响应未生成 assistant 消息。")
    content = getattr(assistant_message, "content", None)
    if not isinstance(content, list) or not content:
        raise LlmProviderResponseError("LLM 响应未生成有效 assistant 内容。")


def _should_retry(exc: Exception, retryable_status_codes: tuple[int, ...]) -> bool:
    """判断异常是否应该按重试策略重试。"""
    return _is_llm_service_call_failure(exc)


def _is_llm_service_call_failure(exc: Exception) -> bool:
    return isinstance(exc, (LlmHttpRequestError, LlmProviderResponseError, TimeoutError, ConnectionError, OSError))


def _as_llm_service_failure(exc: Exception) -> Exception:
    if _is_llm_service_call_failure(exc):
        return exc
    service_error = LlmProviderResponseError(
        f"LLM API 响应未成功生成完整 assistant 回复: {exc.__class__.__name__}: {exc}"
    )
    service_error.__cause__ = exc
    return service_error


def _retry_delay(initial: float, maximum: float, retry_index: int) -> float:
    """计算指数退避等待秒数。"""
    delay = initial * (2 ** retry_index)
    if maximum > 0:
        return min(delay, maximum)
    return delay


def _resolve_api_key(key: str) -> str:
    """解析 API Key 中的 ${ENV_VAR} 或 ${ENV_VAR:-default} 占位符。"""
    match = _ENV_VAR_PATTERN.match(key.strip())
    if match is None:
        return key
    env_key = match.group(1)
    default = match.group(2)
    env_value = os.environ.get(env_key)
    if env_value is not None:
        return env_value
    if default is not None:
        return default
    raise ValueError(f"LLM API Key 环境变量未设置: {env_key}")


def _api_key_label(key: str | None, name: str | None = None) -> str | None:
    if name is not None and name.strip():
        return name.strip()
    if key is None:
        return None
    normalized = key.strip()
    if not normalized:
        return None
    if _ENV_VAR_PATTERN.match(normalized):
        return normalized
    if len(normalized) <= 8:
        return f"{normalized[:2]}****{normalized[-1:]}"
    return f"{normalized[:3]}****{normalized[-4:]}"


def _count_parts(parts: Any, part_type: type) -> int:
    """统计 assistant 响应中指定类型内容片段数量。"""
    return sum(1 for part in parts if isinstance(part, part_type))


def _extract_token_usage(api_format: str, raw_response: Any) -> dict[str, int]:
    """按协议归一化抽取 token 用量。

    各协议字段差异：
    - OpenAI Responses：usage.input_tokens / output_tokens / total_tokens；
      input_tokens_details.cached_tokens、output_tokens_details.reasoning_tokens。
    - OpenAI Chat Completions：usage.prompt_tokens / completion_tokens / total_tokens；
      prompt_tokens_details.cached_tokens。
    - Anthropic Messages：usage.input_tokens / output_tokens；
      cache_read_input_tokens（命中缓存）、cache_creation_input_tokens（写入缓存）。
    - Gemini generateContent：usageMetadata.promptTokenCount / candidatesTokenCount /
      totalTokenCount / cachedContentTokenCount / thoughtsTokenCount。
    - 智谱 GLM：兼容 OpenAI Chat Completions 字段，部分版本含 prompt_cache_hit_tokens。

    统一返回：input_tokens / output_tokens / total_tokens / cached_tokens（缺失为 0），
    可选 reasoning_tokens、cache_creation_tokens 用于补充展示，缺失时省略。
    """
    if not isinstance(raw_response, dict):
        return {}
    if api_format in (API_FORMAT_OPENAI_CHAT_COMPLETIONS, API_FORMAT_ZHIPU_CHAT_COMPLETIONS):
        usage = raw_response.get("usage")
        if not isinstance(usage, dict):
            return {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        cached_tokens = 0
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached_tokens = int(details.get("cached_tokens") or 0)
        if cached_tokens == 0:
            cached_tokens = int(usage.get("prompt_cache_hit_tokens") or 0)
        return {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": cached_tokens,
        }
    if api_format in (API_FORMAT_OPENAI_RESPONSES, API_FORMAT_CODEX_RESPONSES):
        usage = raw_response.get("usage")
        if not isinstance(usage, dict):
            return {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
        cached_tokens = 0
        input_details = usage.get("input_tokens_details")
        if isinstance(input_details, dict):
            cached_tokens = int(input_details.get("cached_tokens") or 0)
        reasoning_tokens = 0
        output_details = usage.get("output_tokens_details")
        if isinstance(output_details, dict):
            reasoning_tokens = int(output_details.get("reasoning_tokens") or 0)
        result = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": cached_tokens,
        }
        if reasoning_tokens:
            result["reasoning_tokens"] = reasoning_tokens
        return result
    if api_format == API_FORMAT_ANTHROPIC_MESSAGES:
        usage = raw_response.get("usage")
        if not isinstance(usage, dict):
            return {}
        base_input = int(usage.get("input_tokens") or 0)
        cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        input_tokens_total = base_input + cache_creation + cache_read
        result = {
            "input_tokens": input_tokens_total,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens_total + output_tokens,
            "cached_tokens": cache_read,
        }
        if cache_creation:
            result["cache_creation_tokens"] = cache_creation
        return result
    if api_format == API_FORMAT_GEMINI_GENERATE_CONTENT:
        usage = raw_response.get("usageMetadata")
        if not isinstance(usage, dict):
            return {}
        prompt_tokens = int(usage.get("promptTokenCount") or 0)
        candidates_tokens = int(usage.get("candidatesTokenCount") or 0)
        total_tokens = int(usage.get("totalTokenCount") or (prompt_tokens + candidates_tokens))
        cached_tokens = int(usage.get("cachedContentTokenCount") or 0)
        thoughts_tokens = int(usage.get("thoughtsTokenCount") or 0)
        result = {
            "input_tokens": prompt_tokens,
            "output_tokens": candidates_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": cached_tokens,
        }
        if thoughts_tokens:
            result["reasoning_tokens"] = thoughts_tokens
        return result
    return {}

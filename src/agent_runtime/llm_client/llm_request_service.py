# -*- coding: utf-8 -*-
"""通用 LLM 请求领域服务。"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import replace
from typing import Any, Awaitable, Callable

import httpx

from agent_runtime.conversation_message.message.operations import (
    ingest_response,
    render_messages,
)
from agent_runtime.conversation_message.message.models import (
    Conversation,
    ThinkingPart,
    ToolUsePart,
    build_current_request_configuration_view,
    build_request_configuration_view_from_timeline,
)
from agent_runtime.conversation_message.message.openai_protocol import (
    OpenAIAdapter,
    OpenAIRenderResult,
)
from agent_runtime.communication.runtime_event_publisher import (
    NoopRuntimeEventPublisher,
    RuntimeEventPublisher,
)
from agent_runtime.codex_oauth.models import CODEX_PROVIDER_ID
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
from agent_runtime.llm_client.llm_runtime_event_publish_queue import LlmRuntimeEventPublishQueue
from agent_runtime.llm_client.llm_stream_events import LlmSemanticStreamParser
from agent_runtime.llm_http_trace.models import LlmHttpTraceContext
from agent_runtime.llm_http_trace.service import LlmHttpTraceService

_ENV_VAR_PATTERN = re.compile(r"^\$\{([^}:]+)(?::-([^}]*))?\}$")
_SUPPRESSED_FRONTEND_SEMANTIC_EVENTS = {
    # llm.response.completed 需要发给前端更新 token/context 与已有 execution bubble。
    "llm.response.failed",
    "llm.thinking.delta",
    "llm.tool_call.arguments.delta",
    "llm.tool_call.arguments.completed",
}
_PROJECTED_OUTPUT_SEMANTIC_EVENTS = {
    "llm.thinking.started",
    "llm.thinking.delta",
    "llm.thinking.completed",
    "llm.content.started",
    "llm.content.delta",
    "llm.content.completed",
    "llm.tool_call.started",
    "llm.tool_call.arguments.delta",
    "llm.tool_call.arguments.completed",
    "llm.tool_call.completed",
}
logger = logging.getLogger(__name__)
_DEFAULT_LLM_REQUEST_ATTEMPT_TIMEOUT_SECONDS = 15 * 60
_OPENAI_RESPONSES_INPUT_CONFIGURATION_OPT_IN = "openai_responses_input_configuration"
_MISSING = object()


def _responses_render_options(
    request_params: dict[str, Any],
    render_options: dict[str, Any],
) -> dict[str, Any]:
    """从当次参数副本提取 Responses input 配置控制项。"""
    options = dict(render_options)
    if "prompt_cache_breakpoint" in request_params:
        marker = request_params.pop("prompt_cache_breakpoint")
        options.setdefault("prompt_cache_breakpoint", marker)
    request_opt_in = request_params.pop(_OPENAI_RESPONSES_INPUT_CONFIGURATION_OPT_IN, None)
    option_opt_in = options.pop(_OPENAI_RESPONSES_INPUT_CONFIGURATION_OPT_IN, None)
    if request_opt_in is not None and not isinstance(request_opt_in, bool):
        raise TypeError(f"{_OPENAI_RESPONSES_INPUT_CONFIGURATION_OPT_IN} 必须是 bool。")
    if option_opt_in is not None and not isinstance(option_opt_in, bool):
        raise TypeError(f"{_OPENAI_RESPONSES_INPUT_CONFIGURATION_OPT_IN} 必须是 bool。")
    if request_opt_in is not None and option_opt_in is not None and request_opt_in != option_opt_in:
        raise ValueError("Responses input 配置能力声明在 request_overrides 与 render_options 中冲突。")
    options["_openai_responses_input_configuration_enabled"] = bool(
        request_opt_in if request_opt_in is not None else option_opt_in
    )
    return options


def _normalize_openai_tools(value: Any, *, field_name: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, dict) for item in value):
        raise TypeError(f"{field_name} 必须是工具 schema 字典序列。")
    return tuple(copy.deepcopy(item) for item in value)


def _resolve_openai_request_configuration(
    *,
    conversation_id: str,
    system_prompt: str | None,
    tools: tuple[dict[str, Any], ...],
    request_params: dict[str, Any],
    render_options: dict[str, Any],
    configuration_view: Any = None,
    conversation: Any = None,
    force_input_configuration: bool = False,
 ) -> tuple[dict[str, Any], dict[str, Any], str | None, tuple[dict[str, Any], ...], Any]:
    """Resolve effective system/tools without leaking legacy top-level fields."""
    params = request_params
    options = render_options
    instructions_override = params.pop("instructions", _MISSING)
    tools_override = params.pop("tools", _MISSING)
    if configuration_view is not None and (
        instructions_override is not _MISSING or tools_override is not _MISSING
    ):
        raise ValueError("显式 configuration_view 不能与 request_overrides.instructions/tools 同时使用。")
    if tools_override is _MISSING:
        effective_tools = tuple(copy.deepcopy(item) for item in tools)
    elif tools_override is None:
        effective_tools = ()
    else:
        effective_tools = _normalize_openai_tools(tools_override, field_name="request_overrides.tools")
    effective_system = (
        system_prompt
        if instructions_override is _MISSING
        else instructions_override
    )
    if effective_system is not None and not isinstance(effective_system, str):
        raise TypeError("OpenAI Responses 的 instructions 必须是字符串或 None。")

    if configuration_view is None and conversation is not None:
        timeline = getattr(conversation, "timeline", None)
        if timeline:
            configuration_view = build_request_configuration_view_from_timeline(
                conversation_id=conversation_id,
                timeline=timeline,
                system_prompt=effective_system,
                tools=effective_tools,
            )

    if configuration_view is not None:
        current_prompt = getattr(configuration_view, "current_prompt", None)
        current_tools = getattr(configuration_view, "current_tools", None)
        if current_prompt is None:
            if effective_system is not None:
                raise ValueError("显式 configuration_view 与 system_prompt 内容冲突。")
        elif current_prompt.resolved_content != effective_system:
            raise ValueError("显式 configuration_view 与 system_prompt 内容冲突。")
        if current_tools is None:
            if effective_tools:
                raise ValueError("显式 configuration_view 与 tools 内容冲突。")
        elif tuple(current_tools.ordered_definitions) != effective_tools:
            raise ValueError("显式 configuration_view 与 tools 内容冲突。")
        return params, options, effective_system, effective_tools, configuration_view
    enabled = force_input_configuration or options.pop(
        "_openai_responses_input_configuration_enabled", False
    )
    if enabled and (effective_system is not None or effective_tools):
        configuration_view = build_current_request_configuration_view(
            conversation_id=conversation_id,
            system_prompt=effective_system,
            tools=effective_tools,
        )
    return params, options, effective_system, effective_tools, configuration_view


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

    async def request_standard_responses(
        self,
        *,
        resolved_model: Any,
        conversation_id: str,
        input_items: list[Any],
        system_prompt: str | None,
        tools: tuple[dict[str, Any], ...],
        request_params: dict[str, Any],
        api_key: str | None,
        input_configuration_capability: bool = False,
    ) -> Any:
        """Dispatch a raw standard Responses request through the configured protocol client."""
        resolved = resolved_model
        if resolved.api_format not in {API_FORMAT_OPENAI_RESPONSES, API_FORMAT_CODEX_RESPONSES}:
            raise ValueError(
                "Standard Responses dispatch only supports "
                f"{API_FORMAT_OPENAI_RESPONSES} or {API_FORMAT_CODEX_RESPONSES}; "
                f"received {resolved.api_format}."
            )
        if resolved.api_format == API_FORMAT_CODEX_RESPONSES and resolved.provider_id != CODEX_PROVIDER_ID:
            raise ValueError(
                f"api_format={API_FORMAT_CODEX_RESPONSES} 仅保留给托管 Provider {CODEX_PROVIDER_ID}。"
            )
        client = self._protocol_clients.get(resolved.api_format)
        if client is None:
            raise ValueError(
                f"当前 LLM 请求客户端不支持 api_format={resolved.api_format}。"
            )
        owns_credentials_and_retries = bool(
            getattr(client, "owns_credentials_and_retries", False)
        )
        if not owns_credentials_and_retries:
            headers = dict(resolved.custom_headers)
            if not any(key.lower() == "x-relay-session-id" for key in headers):
                headers["x-relay-session-id"] = conversation_id
            resolved = replace(resolved, custom_headers=headers)
        request_params = dict(request_params)
        client_system_prompt = system_prompt
        client_tools = tools
        if resolved.api_format == API_FORMAT_OPENAI_RESPONSES:
            if not isinstance(input_configuration_capability, bool):
                raise TypeError("input_configuration_capability 必须是 bool。")
            if input_configuration_capability:
                request_params[_OPENAI_RESPONSES_INPUT_CONFIGURATION_OPT_IN] = True
            render_options = _responses_render_options(request_params, {})
            (
                request_params,
                render_options,
                client_system_prompt,
                client_tools,
                configuration_view,
            ) = _resolve_openai_request_configuration(
                conversation_id=conversation_id,
                system_prompt=system_prompt,
                tools=tools,
                request_params=request_params,
                render_options=render_options,
            )
            render_options["openai_responses_configuration_view"] = configuration_view
            rendered_messages = await OpenAIAdapter().render(
                Conversation(id=conversation_id),
                self._resource_resolver,
                render_options,
            )
            rendered_messages = replace(
                rendered_messages,
                input=[*rendered_messages.input, *copy.deepcopy(input_items)],
            )
        else:
            rendered_messages = OpenAIRenderResult(
                input=copy.deepcopy(input_items),
                prompt_cache_key=conversation_id,
            )
        client_request_kwargs: dict[str, Any] = {
            "resolved_model": resolved,
            "rendered_messages": rendered_messages,
            "system_prompt": client_system_prompt,
            "tools": client_tools,
            "request_params": dict(request_params),
            "api_key": None if owns_credentials_and_retries else api_key,
            "http_trace_service": self._http_trace_service,
        }
        if owns_credentials_and_retries:
            client_request_kwargs["conversation_id"] = conversation_id
        return await client.request(**client_request_kwargs)

    async def send_prompt(self, request: LlmPromptRequest) -> LlmPromptResponse:
        """发送一次 LLM 请求，并把 assistant 响应追加到 Conversation。"""
        resolved = request.resolved_model
        if resolved.provider_id == CODEX_PROVIDER_ID and resolved.api_format != API_FORMAT_CODEX_RESPONSES:
            logger.warning(
                "Managed Codex binding protocol normalized to SDK bridge: provider_id=%s binding_id=%s configured_api_format=%s",
                resolved.provider_id,
                resolved.binding_id,
                resolved.api_format,
            )
            resolved = replace(resolved, api_format=API_FORMAT_CODEX_RESPONSES)
        if resolved.api_format == API_FORMAT_CODEX_RESPONSES and resolved.provider_id != CODEX_PROVIDER_ID:
            raise ValueError(
                f"api_format={API_FORMAT_CODEX_RESPONSES} 仅保留给托管 Provider {CODEX_PROVIDER_ID}。"
            )
        client = self._protocol_clients.get(resolved.api_format)
        if client is None:
            raise ValueError(
                f"当前 LLM 请求客户端不支持 api_format={resolved.api_format}。"
            )
        owns_credentials_and_retries = bool(
            getattr(client, "owns_credentials_and_retries", False)
        )
        if not owns_credentials_and_retries:
            resolved = _with_default_relay_session_header(request)

        request_params = _merge_request_params(request)
        tool_schemas = tuple(tool.schema for tool in request.tools)
        if not tool_schemas and getattr(request.conversation, "tools", None):
            tool_schemas = tuple(request.conversation.tools)
        raw_system_prompt = (
            request.system_prompt
            if request.system_prompt is not None
            else getattr(request.conversation, "system_prompt", None)
        )
        client_system_prompt = raw_system_prompt
        client_tools = tool_schemas
        render_options = {
            **request.render_options,
            "request_params": request_params,
            "supported_input_modalities": resolved.supported_input_modalities,
        }
        if resolved.api_format == API_FORMAT_OPENAI_RESPONSES:
            render_options = _responses_render_options(request_params, render_options)
            (
                request_params,
                render_options,
                client_system_prompt,
                client_tools,
                configuration_view,
            ) = _resolve_openai_request_configuration(
                conversation_id=request.conversation.id,
                system_prompt=raw_system_prompt,
                tools=tool_schemas,
                request_params=request_params,
                render_options=render_options,
                configuration_view=request.configuration_view,
                conversation=request.conversation,
            )
            if configuration_view is not None:
                render_options["openai_responses_configuration_view"] = configuration_view
        else:
            render_options.pop("openai_responses_configuration_view", None)
        rendered_messages = await render_messages(
            request.conversation,
            resolved.api_format,
            self._resource_resolver,
            render_options,
        )
        api_key_items = [] if owns_credentials_and_retries else list(resolved.api_keys)
        api_keys: list[str | None] = [item.key for item in api_key_items]
        if owns_credentials_and_retries or not api_keys:
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
            api_key_label = None if owns_credentials_and_retries else _api_key_label(raw_api_key, api_key_name)
            try:
                api_key = None if owns_credentials_and_retries else (
                    _resolve_api_key(raw_api_key) if raw_api_key is not None else None
                )
                api_key_label = None if owns_credentials_and_retries else _api_key_label(api_key, api_key_name)
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
                _notify_request_event(request, "llm.request.started", request_payload)
                if request.request_attempt_observer is not None:
                    request.request_attempt_observer(dict(request_payload))
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
                failed_payload = {
                    **request_payload,
                    "attempt_count": attempt_count,
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                    "failed_stage": "api_key_resolution",
                    "partial_response": False,
                    "duration_ms": int((time.perf_counter() - request_attempt_started_at) * 1000),
                    **retry_target,
                }
                await self._event_publisher.publish(
                    request.communication_context,
                    "llm.request.failed",
                    failed_payload,
                    source="llm_client",
                    metadata={"conversation_id": request.conversation.id},
                )
                _notify_request_event(request, "llm.request.failed", failed_payload)
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
            _notify_request_event(request, "llm.request.started", request_payload)
            if request.request_attempt_observer is not None:
                request.request_attempt_observer(dict(request_payload))
            max_retries = 0 if owns_credentials_and_retries else resolved.retry.max_retries_per_key
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
                    _notify_request_event(request, "llm.request.started", request_payload)
                    if request.request_attempt_observer is not None:
                        request.request_attempt_observer(dict(request_payload))
                stream_state: dict[str, bool] = {
                    "response_started": False,
                    "response_failed": False,
                    "output_projected": False,
                }
                api_response_received = False
                messages_snapshot = len(request.conversation.messages)
                provider_state_snapshot = copy.deepcopy(request.conversation.provider_state)
                conversation_state_snapshot = copy.deepcopy(request.conversation.state)
                try:
                    stream_event_handler = _build_stream_event_handler(
                        publisher=self._event_publisher,
                        context=request.communication_context,
                        conversation_id=request.conversation.id,
                        request_event_observer=request.request_event_observer,
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
                    client_request_kwargs: dict[str, Any] = {
                        "resolved_model": resolved,
                        "rendered_messages": rendered_messages,
                        "system_prompt": client_system_prompt,
                        "tools": client_tools,
                        "request_params": request_params,
                        "api_key": api_key,
                        "stream_event_handler": stream_event_handler,
                        "http_trace_service": self._http_trace_service,
                        "http_trace_context": _build_http_trace_context(
                            request=request,
                            resolved=resolved,
                            llm_call_id=llm_call_id,
                            llm_request_id=llm_request_id,
                            api_key_index=key_index,
                            retry_index=retry_index,
                            attempt_count=attempt_count,
                        ),
                        "cancellation_checker": request.cancellation_checker,
                    }
                    if owns_credentials_and_retries:
                        client_request_kwargs["conversation_id"] = request.conversation.id
                    raw_response = await _await_client_request(
                        client.request(**client_request_kwargs),
                        request,
                    )
                    if request.cancellation_checker is not None and request.cancellation_checker():
                        raise LlmRequestCancelled("LLM request cancelled.")
                    api_response_received = True
                    if not stream_state.get("response_started"):
                        stream_state["response_started"] = True
                        response_started_payload = {**request_payload, "content_type": None}
                        await self._event_publisher.publish(
                            request.communication_context,
                            "llm.response.started",
                            response_started_payload,
                            source="llm_client",
                            metadata={"conversation_id": request.conversation.id},
                        )
                        _notify_request_event(request, "llm.response.started", response_started_payload)
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
                    cancelled_payload = {
                        **request_payload,
                        "attempt_count": attempt_count,
                        "cancelled_by": "user",
                        "cancelled_stage": "response_stream" if stream_state.get("response_started") else "before_response",
                        "partial_response": stream_state.get("output_projected") is True,
                        "duration_ms": int((time.perf_counter() - request_attempt_started_at) * 1000),
                        "reason": str(exc) or "LLM request cancelled.",
                    }
                    await self._event_publisher.publish(
                        request.communication_context,
                        "llm.request.cancelled",
                        cancelled_payload,
                        source="llm_client",
                        metadata={"conversation_id": request.conversation.id},
                    )
                    _notify_request_event(request, "llm.request.cancelled", cancelled_payload)
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
                    partial_output = bool(
                        getattr(service_error, "partial_output", False)
                        or (owns_credentials_and_retries and stream_state.get("output_projected") is True)
                    )
                    if partial_output and not bool(getattr(service_error, "partial_output", False)):
                        setattr(service_error, "partial_output", True)
                    retry_target = _build_retry_target_payload(
                        resolved=resolved,
                        candidate_index=request.candidate_index,
                        api_key_index=key_index,
                        retry_index=retry_index,
                        max_retries=max_retries,
                        api_key_count=len(api_keys),
                        next_resolved_model=None if partial_output else request.next_resolved_model,
                        retryable=(
                            False
                            if partial_output or owns_credentials_and_retries
                            else _should_retry(service_error, resolved.retry.retryable_status_codes)
                        ),
                    )
                    failed_payload = {
                        **request_payload,
                        "attempt_count": attempt_count,
                        "error_type": service_error.__class__.__name__,
                        "error_message": str(service_error),
                        "failed_stage": "response_stream" if stream_state.get("response_started") else "before_response",
                        "partial_response": partial_output,
                        "duration_ms": int((time.perf_counter() - request_attempt_started_at) * 1000),
                        **retry_target,
                    }
                    diagnostic = _explicit_error_diagnostic(
                        service_error,
                        provider_id=resolved.provider_id,
                        api_format=resolved.api_format,
                    )
                    if diagnostic is not None:
                        failed_payload["diagnostic"] = diagnostic
                    await self._event_publisher.publish(
                        request.communication_context,
                        "llm.request.failed",
                        failed_payload,
                        source="llm_client",
                        metadata={"conversation_id": request.conversation.id},
                    )
                    _notify_request_event(request, "llm.request.failed", failed_payload)
                    if partial_output or not _should_retry(service_error, resolved.retry.retryable_status_codes):
                        break
                    if retry_index >= max_retries:
                        break
                    await asyncio.sleep(_retry_delay(resolved.retry.initial_delay_seconds, resolved.retry.max_delay_seconds, retry_index))
                else:
                    completed_payload = {
                        **request_payload,
                        "assistant_part_count": len(assistant_message.content),
                        "thinking_part_count": _count_parts(assistant_message.content, ThinkingPart),
                        "tool_use_part_count": _count_parts(assistant_message.content, ToolUsePart),
                        "duration_ms": int((time.perf_counter() - request_attempt_started_at) * 1000),
                        "call_duration_ms": int((time.perf_counter() - call_started_at) * 1000),
                        "token_usage": token_usage,
                        "context_window_max": resolved.context_window,
                    }
                    await self._event_publisher.publish(
                        request.communication_context,
                        "llm.response.completed",
                        completed_payload,
                        source="llm_client",
                        metadata={"conversation_id": request.conversation.id},
                    )
                    _notify_request_event(request, "llm.response.completed", completed_payload)
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
                call_failed_payload = {
                    "llm_call_id": llm_call_id,
                    "candidate_index": request.candidate_index,
                    "attempt_count": attempt_count,
                    "error_type": last_error.__class__.__name__,
                    "error_message": str(last_error),
                    "duration_ms": int((time.perf_counter() - call_started_at) * 1000),
                }
                diagnostic = _explicit_error_diagnostic(
                    last_error,
                    provider_id=resolved.provider_id,
                    api_format=resolved.api_format,
                )
                if diagnostic is not None:
                    call_failed_payload["diagnostic"] = diagnostic
                await self._event_publisher.publish(
                    request.communication_context,
                    "llm.call.failed",
                    call_failed_payload,
                    source="llm_client",
                    metadata={"conversation_id": request.conversation.id},
                )
            partial_output_error = bool(getattr(last_error, "partial_output", False))
            if partial_output_error:
                raise last_error
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
        diagnostic = _explicit_error_diagnostic(
            cause,
            provider_id=provider_id,
        )
        if diagnostic is not None:
            self.diagnostic = diagnostic
        super().__init__(
            f"LLM 服务商模型调用未成功响应: provider={provider_id}, binding={binding_id}, "
            f"attempt_count={attempt_count}, cause={cause.__class__.__name__}: {cause}"
        )


class LlmRequestTimeoutError(TimeoutError):
    """单次 LLM 请求 attempt 超过业务级总耗时上限。"""

    def __init__(self, *, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(f"LLM 请求单次 attempt 超过业务级总超时 {timeout_seconds:g} 秒。")


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


def _notify_request_event(request: LlmPromptRequest, event_type: str, payload: dict[str, Any]) -> None:
    if request.request_event_observer is None:
        return
    try:
        request.request_event_observer(event_type, dict(payload))
    except Exception:
        return


def _notify_request_observer(
    observer: Callable[[str, dict[str, Any]], None] | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    if observer is None:
        return
    try:
        observer(event_type, dict(payload))
    except Exception:
        return


def _response_payload_projects_output(response: Any) -> bool:
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


def _provider_event_projects_output(
    api_format: str,
    event_name: str,
    data: dict[str, Any] | None,
) -> bool:
    if api_format not in {API_FORMAT_OPENAI_RESPONSES, API_FORMAT_CODEX_RESPONSES}:
        return False
    if not isinstance(data, dict):
        return False
    event_type = str(data.get("type") or event_name)
    if _response_payload_projects_output(data.get("response")):
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
    if event_type in {
        "response.output_text.done",
        "response.reasoning_summary_text.done",
        "response.reasoning_text.done",
    }:
        return bool(data.get("text"))
    if event_type == "response.refusal.done":
        return bool(data.get("refusal"))
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        item = data.get("item")
        return isinstance(item, dict) and item.get("type") in {
            "message",
            "reasoning",
            "function_call",
            "custom_tool_call",
        }
    return False


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
    conversation_id: str,
    request_event_observer: Callable[[str, dict[str, Any]], None] | None,
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
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    semantic_parser = LlmSemanticStreamParser(api_format)

    async def publish_semantic_event(event_type: str, payload: dict[str, Any]) -> None:
        await publisher.publish(
            context,
            event_type,
            payload,
            source="llm_client",
            metadata={"conversation_id": conversation_id},
        )

    publish_queue = LlmRuntimeEventPublishQueue(
        publish=publish_semantic_event,
        conversation_id=conversation_id,
        llm_request_id=llm_request_id,
    )

    async def handle(event: dict[str, Any]) -> None:
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
            provider_event_name = str(event_payload.get("event") or "message")
            provider_event_data = event_payload.get("data") if isinstance(event_payload.get("data"), dict) else None
            if _provider_event_projects_output(api_format, provider_event_name, provider_event_data):
                stream_state["output_projected"] = True
            semantic_events = semantic_parser.parse_provider_event(
                event_name=provider_event_name,
                data=provider_event_data,
                done=event_payload.get("done") is True,
            )
            if any(semantic.event_type in _PROJECTED_OUTPUT_SEMANTIC_EVENTS for semantic in semantic_events):
                stream_state["output_projected"] = True
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
        try:
            for publish_event_type, publish_payload in events_to_publish:
                _notify_request_observer(request_event_observer, publish_event_type, publish_payload)
                await publish_queue.submit(publish_event_type, publish_payload)
        finally:
            if event_type in {"llm.response.stream.completed", "llm.response.stream.failed"}:
                await publish_queue.close()

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
    return isinstance(
        exc,
        (LlmHttpRequestError, LlmProviderResponseError, TimeoutError, ConnectionError, OSError, httpx.HTTPError),
    )


def _as_llm_service_failure(exc: Exception) -> Exception:
    if _is_llm_service_call_failure(exc):
        return exc
    service_error = LlmProviderResponseError(
        f"LLM API 响应未成功生成完整 assistant 回复: {exc.__class__.__name__}: {exc}"
    )
    service_error.__cause__ = exc
    return service_error


def _explicit_error_diagnostic(
    exc: BaseException,
    *,
    provider_id: str,
    api_format: str | None = None,
) -> dict[str, Any] | None:
    if provider_id != CODEX_PROVIDER_ID:
        return None
    if api_format is not None and api_format != API_FORMAT_CODEX_RESPONSES:
        return None
    from agent_runtime.codex_oauth.diagnostics import sdk_error_projection
    from agent_runtime.llm_client.codex_sdk_protocol_client import CodexSdkResponseError

    if not isinstance(exc, CodexSdkResponseError):
        return None
    if not isinstance(getattr(exc, "diagnostic", None), Mapping):
        return None
    return sdk_error_projection(
        exc,
        stage="responses_request",
        partial_output=bool(getattr(exc, "partial_output", False)),
    )


async def _await_client_request(
    awaitable: Any,
    request: LlmPromptRequest,
    *,
    attempt_timeout_seconds: float = _DEFAULT_LLM_REQUEST_ATTEMPT_TIMEOUT_SECONDS,
) -> Any:
    task = asyncio.create_task(awaitable)
    observer = request.request_canceller_observer
    unregister: Callable[[], None] | None = None
    if observer is not None:
        event_loop = asyncio.get_running_loop()

        def cancel_request() -> None:
            event_loop.call_soon_threadsafe(task.cancel)

        unregister = observer(cancel_request)
    try:
        if attempt_timeout_seconds <= 0:
            return await task
        done, _pending = await asyncio.wait({task}, timeout=attempt_timeout_seconds)
        if task in done:
            return await task
        task.cancel()
        try:
            await task
        except LlmRequestCancelled:
            if request.cancellation_checker is not None and request.cancellation_checker():
                raise
        except asyncio.CancelledError:
            pass
        raise LlmRequestTimeoutError(timeout_seconds=attempt_timeout_seconds)
    except asyncio.CancelledError:
        task.cancel()
        with suppress(Exception, asyncio.CancelledError):
            await task
        if request.cancellation_checker is not None and request.cancellation_checker():
            raise LlmRequestCancelled("LLM request cancelled.") from None
        raise
    finally:
        if unregister is not None:
            unregister()


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

# -*- coding: utf-8 -*-
"""通用 LLM 请求客户端的南向协议适配器。"""
from __future__ import annotations

import json
import copy
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, is_dataclass
from typing import Any, Callable, Protocol

from agent_runtime.conversation_message.message.claude_protocol import ClaudeRenderResult
from agent_runtime.conversation_message.message.gemini_protocol import GeminiRenderResult
from agent_runtime.conversation_message.message.openai_chat_protocol import OpenAIChatRenderResult
from agent_runtime.conversation_message.message.openai_protocol import OpenAIRenderResult
from agent_runtime.llm_api_protocol import (
    API_FORMAT_ANTHROPIC_MESSAGES,
    API_FORMAT_GEMINI_GENERATE_CONTENT,
    API_FORMAT_OPENAI_CHAT_COMPLETIONS,
    API_FORMAT_OPENAI_RESPONSES,
    API_FORMAT_ZHIPU_CHAT_COMPLETIONS,
)
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    ResolvedLlmProviderModel,
)
from agent_runtime.llm_http_trace.models import LlmHttpTraceContext
from agent_runtime.llm_http_trace.service import LlmHttpTraceService


StreamEventHandler = Callable[[dict[str, Any]], None]
CancellationChecker = Callable[[], bool]
_DEFAULT_USER_AGENT = "mindloop.ai"


class LlmRequestCancelled(Exception):
    pass


class LlmProtocolClient(Protocol):
    """具体 API 协议请求客户端。"""

    @property
    def api_format(self) -> str:
        """客户端支持的具体 API 协议格式。"""
        ...

    async def request(
        self,
        *,
        resolved_model: ResolvedLlmProviderModel,
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
        """发送一次 LLM 请求并返回 JSON 响应。"""
        ...


class OpenAIResponsesProtocolClient:
    """OpenAI Responses API 请求客户端。"""

    @property
    def api_format(self) -> str:
        return API_FORMAT_OPENAI_RESPONSES

    async def request(
        self,
        *,
        resolved_model: ResolvedLlmProviderModel,
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
        if not isinstance(rendered_messages, OpenAIRenderResult):
            raise TypeError("openai_responses 渲染结果类型不正确。")
        body: dict[str, Any] = {
            "model": resolved_model.model_alias,
            "input": rendered_messages.input,
            "max_output_tokens": resolved_model.max_output_tokens,
            "include": rendered_messages.include,
            **request_params,
        }
        body["store"] = False
        body["include"] = _ensure_include_item(body.get("include"), "reasoning.encrypted_content")
        if system_prompt:
            body.setdefault("instructions", system_prompt)
        body.pop("previous_response_id", None)
        if rendered_messages.prompt_cache_key:
            body["prompt_cache_key"] = rendered_messages.prompt_cache_key
        if tools:
            body.setdefault("tools", list(tools))
            # 应用层默认启用并发工具调用；provider 通过 request_params.parallel_tool_calls=False 可显式关闭。
            body.setdefault("parallel_tool_calls", True)
        url = _join_url(resolved_model.api_base_url, "responses")
        headers = _json_headers(resolved_model.custom_headers)
        if api_key:
            headers.setdefault("Authorization", f"Bearer {api_key}")
        return await _post_json(
            url,
            headers,
            body,
            sse_aggregator=_aggregate_openai_responses_sse,
            stream_event_handler=stream_event_handler,
            http_trace_service=http_trace_service,
            http_trace_context=http_trace_context,
            cancellation_checker=cancellation_checker,
        )


class OpenAIChatCompletionsProtocolClient:
    """OpenAI Chat Completions API 请求客户端。"""

    @property
    def api_format(self) -> str:
        return API_FORMAT_OPENAI_CHAT_COMPLETIONS

    async def request(
        self,
        *,
        resolved_model: ResolvedLlmProviderModel,
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
        if not isinstance(rendered_messages, OpenAIChatRenderResult):
            raise TypeError("openai_chat_completions 渲染结果类型不正确。")
        messages = list(rendered_messages.messages)
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})
        body: dict[str, Any] = {
            "model": resolved_model.model_alias,
            "messages": messages,
            "max_tokens": resolved_model.max_output_tokens,
            **request_params,
        }
        if tools:
            body.setdefault("tools", list(tools))
            # 应用层默认启用并发工具调用；provider 通过 request_params.parallel_tool_calls=False 可显式关闭。
            body.setdefault("parallel_tool_calls", True)
        url = _join_url(resolved_model.api_base_url, "chat/completions")
        headers = _json_headers(resolved_model.custom_headers)
        if api_key:
            headers.setdefault("Authorization", f"Bearer {api_key}")
        return await _post_json(
            url,
            headers,
            body,
            sse_aggregator=_aggregate_chat_completions_sse,
            stream_event_handler=stream_event_handler,
            http_trace_service=http_trace_service,
            http_trace_context=http_trace_context,
            cancellation_checker=cancellation_checker,
        )


class AnthropicMessagesProtocolClient:
    """Anthropic Messages API 请求客户端。"""

    @property
    def api_format(self) -> str:
        return API_FORMAT_ANTHROPIC_MESSAGES

    async def request(
        self,
        *,
        resolved_model: ResolvedLlmProviderModel,
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
        if not isinstance(rendered_messages, ClaudeRenderResult):
            raise TypeError("anthropic_messages 渲染结果类型不正确。")
        body: dict[str, Any] = {
            "model": resolved_model.model_alias,
            "messages": rendered_messages.messages,
            "max_tokens": resolved_model.max_output_tokens,
            **request_params,
        }
        if system_prompt:
            body.setdefault("system", _build_claude_cached_system(system_prompt))
        if tools:
            body.setdefault("tools", _build_claude_cached_tools(tools))
            # Anthropic Messages 协议默认开启并发工具调用，无需主动写字段；如需关闭，
            # provider 在 request_params 中设置 tool_choice = {"type": "auto", "disable_parallel_tool_use": True}。
        url = _join_url(resolved_model.api_base_url, "v1/messages")
        headers = _json_headers(resolved_model.custom_headers)
        if api_key:
            headers.setdefault("x-api-key", api_key)
        headers.setdefault("anthropic-version", "2023-06-01")
        return await _post_json(
            url,
            headers,
            body,
            sse_aggregator=_aggregate_anthropic_messages_sse,
            stream_event_handler=stream_event_handler,
            http_trace_service=http_trace_service,
            http_trace_context=http_trace_context,
            cancellation_checker=cancellation_checker,
        )


class GeminiGenerateContentProtocolClient:
    """Gemini generateContent API 请求客户端。"""

    @property
    def api_format(self) -> str:
        return API_FORMAT_GEMINI_GENERATE_CONTENT

    async def request(
        self,
        *,
        resolved_model: ResolvedLlmProviderModel,
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
        if not isinstance(rendered_messages, GeminiRenderResult):
            raise TypeError("gemini_generate_content 渲染结果类型不正确。")
        body: dict[str, Any] = {
            "contents": rendered_messages.contents,
            **request_params,
        }
        stream_requested = body.pop("stream", False) is True
        if system_prompt:
            body.setdefault("systemInstruction", {"parts": [{"text": system_prompt}]})
        if rendered_messages.cached_content:
            body.setdefault("cachedContent", rendered_messages.cached_content)
        if tools:
            body.setdefault("tools", [{"functionDeclarations": list(tools)}])
            # Gemini generateContent 默认支持并发 functionCall（响应里多个 functionCall part 会平铺到 candidates[0].content.parts），
            # 协议未提供并发开关，无字段可设；如需限制为单工具调用需在 system prompt 中要求模型每轮只输出一个 functionCall。
        method_name = "streamGenerateContent" if stream_requested else "generateContent"
        url = _join_url(
            resolved_model.api_base_url,
            f"v1beta/models/{urllib.parse.quote(resolved_model.model_alias, safe='')}:{method_name}",
        )
        if stream_requested and "alt=" not in url:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}alt=sse"
        if api_key and "key=" not in url:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}key={urllib.parse.quote(api_key)}"
        headers = _json_headers(resolved_model.custom_headers)
        if api_key:
            headers.setdefault("x-goog-api-key", api_key)
        return await _post_json(
            url,
            headers,
            body,
            sse_aggregator=_aggregate_gemini_generate_content_sse,
            stream_event_handler=stream_event_handler,
            expect_stream=stream_requested,
            http_trace_service=http_trace_service,
            http_trace_context=http_trace_context,
            cancellation_checker=cancellation_checker,
        )


class ZhipuChatCompletionsProtocolClient:
    """智谱 GLM Chat Completions API 请求客户端。"""

    @property
    def api_format(self) -> str:
        return API_FORMAT_ZHIPU_CHAT_COMPLETIONS

    async def request(
        self,
        *,
        resolved_model: ResolvedLlmProviderModel,
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
        from agent_runtime.conversation_message.message.zhipu_protocol import (
            ZhipuChatRenderResult,
        )

        if not isinstance(rendered_messages, ZhipuChatRenderResult):
            raise TypeError("zhipu_chat_completions 渲染结果类型不正确。")
        messages = list(rendered_messages.messages)
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})
        body: dict[str, Any] = {
            "model": resolved_model.model_alias,
            "messages": messages,
            "max_tokens": resolved_model.max_output_tokens,
            **request_params,
        }
        body.setdefault("thinking", {"type": "enabled", "clear_thinking": False})
        if tools:
            body.setdefault("tools", list(tools))
            # 智谱 GLM 兼容 OpenAI Chat Completions 字段，应用层默认启用并发工具调用；
            # provider 通过 request_params.parallel_tool_calls=False 可显式关闭。
            body.setdefault("parallel_tool_calls", True)
            body.setdefault("tool_stream", True)
        url = _join_url(resolved_model.api_base_url, "chat/completions")
        headers = _json_headers(resolved_model.custom_headers)
        if api_key:
            headers.setdefault("Authorization", f"Bearer {api_key}")
        return await _post_json(
            url,
            headers,
            body,
            sse_aggregator=_aggregate_chat_completions_sse,
            stream_event_handler=stream_event_handler,
            http_trace_service=http_trace_service,
            http_trace_context=http_trace_context,
            cancellation_checker=cancellation_checker,
        )


def serialize_render_result(value: Any) -> dict[str, Any]:
    """把协议渲染结果转换为普通字典，用于调试和 Trace 事件记录。"""
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return value
    return {"value": str(value)}


def _ensure_include_item(value: Any, required_item: str) -> list[Any]:
    """确保 OpenAI Responses include 参数包含 stateless reasoning 回灌必需项。"""
    if value is None:
        return [required_item]
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = list(value)
    elif isinstance(value, tuple):
        items = list(value)
    else:
        items = [value]
    if required_item not in items:
        items.append(required_item)
    return items


def _build_claude_cached_system(system_prompt: str) -> list[dict[str, Any]]:
    """构建带静态缓存断点的 Claude system 内容块。"""
    return [{
        "type": "text",
        "text": system_prompt,
        "cache_control": {"type": "ephemeral"},
    }]


def _build_claude_cached_tools(tools: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """构建带静态缓存断点的 Claude tools 列表。"""
    cached_tools = [copy.deepcopy(tool) for tool in tools]
    if cached_tools:
        cached_tools[-1]["cache_control"] = {"type": "ephemeral"}
    return cached_tools


def _json_headers(custom_headers: dict[str, str]) -> dict[str, str]:
    """构建 JSON 请求头，保留用户自定义 header。"""
    headers = dict(custom_headers)
    _setdefault_header(headers, "Content-Type", "application/json")
    _setdefault_header(headers, "User-Agent", _DEFAULT_USER_AGENT)
    return headers


def _setdefault_header(headers: dict[str, str], name: str, value: str) -> None:
    if not any(key.lower() == name.lower() for key in headers):
        headers[name] = value


def _join_url(base_url: str, path: str) -> str:
    """拼接 API base URL 和相对路径。"""
    return base_url.rstrip("/") + "/" + path.lstrip("/")


async def _post_json(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    sse_aggregator: Callable[[str], Any] | None = None,
    stream_event_handler: StreamEventHandler | None = None,
    expect_stream: bool = False,
    http_trace_service: LlmHttpTraceService | None = None,
    http_trace_context: LlmHttpTraceContext | None = None,
    cancellation_checker: CancellationChecker | None = None,
) -> Any:
    """?????????? JSON POST??????? HTTP ??????"""
    import asyncio

    return await asyncio.to_thread(
        _post_json_sync,
        url,
        headers,
        body,
        sse_aggregator,
        stream_event_handler,
        expect_stream,
        http_trace_service,
        http_trace_context,
        cancellation_checker,
    )


def _post_json_sync(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    sse_aggregator: Callable[[str], Any] | None = None,
    stream_event_handler: StreamEventHandler | None = None,
    expect_stream: bool = False,
    http_trace_service: LlmHttpTraceService | None = None,
    http_trace_context: LlmHttpTraceContext | None = None,
    cancellation_checker: CancellationChecker | None = None,
) -> Any:
    """?? JSON POST??? HTTP ?????? status_code ????"""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request_headers = dict(headers)
    if body.get("stream") is True or expect_stream:
        request_headers.setdefault("Accept", "text/event-stream")
    request = urllib.request.Request(url=url, data=payload, headers=request_headers, method="POST")
    trace_started_at = time.perf_counter()
    trace_record = None
    trace_enabled = http_trace_service is not None and http_trace_context is not None
    if trace_enabled:
        trace_record = http_trace_service.record_started(
            context=http_trace_context,
            method="POST",
            url=url,
            request_headers=request_headers,
            request_body_bytes=payload,
            is_stream=(body.get("stream") is True or expect_stream),
        )
    try:
        _raise_if_cancelled(cancellation_checker)
        with urllib.request.urlopen(request, timeout=300) as response:
            _raise_if_cancelled(cancellation_checker)
            status_code = int(getattr(response, "status", response.getcode()))
            response_headers = dict(response.headers.items())
            content_type = response.headers.get("Content-Type", "")
            if "text/event-stream" in content_type or expect_stream:
                if sse_aggregator is None:
                    raise RuntimeError("??????????? text/event-stream ?????")
                text = _read_sse_response_text(response, content_type, stream_event_handler, cancellation_checker)
                response_bytes = text.encode("utf-8")
                if trace_enabled:
                    http_trace_service.record_completed(
                        started_record=trace_record,
                        context=http_trace_context,
                        status_code=status_code,
                        response_headers=response_headers,
                        response_body_bytes=response_bytes,
                        response_content_type=content_type,
                        duration_ms=int((time.perf_counter() - trace_started_at) * 1000),
                        sse_event_count=_count_sse_events(text),
                    )
                return sse_aggregator(text)
            _raise_if_cancelled(cancellation_checker)
            response_bytes = response.read()
            if trace_enabled:
                http_trace_service.record_completed(
                    started_record=trace_record,
                    context=http_trace_context,
                    status_code=status_code,
                    response_headers=response_headers,
                    response_body_bytes=response_bytes,
                    response_content_type=content_type,
                    duration_ms=int((time.perf_counter() - trace_started_at) * 1000),
                )
            text = response_bytes.decode("utf-8")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        error_bytes = exc.read()
        if trace_enabled:
            http_trace_service.record_failed(
                started_record=trace_record,
                context=http_trace_context,
                error_type=exc.__class__.__name__,
                error_message=str(exc),
                duration_ms=int((time.perf_counter() - trace_started_at) * 1000),
                status_code=exc.code,
                response_headers=dict(exc.headers.items()) if exc.headers is not None else {},
                response_body_bytes=error_bytes,
                response_content_type=exc.headers.get("Content-Type") if exc.headers is not None else None,
            )
        error_text = error_bytes.decode("utf-8", errors="replace")
        raise LlmHttpRequestError(exc.code, error_text) from exc
    except LlmRequestCancelled:
        if trace_enabled:
            http_trace_service.record_failed(
                started_record=trace_record,
                context=http_trace_context,
                error_type="LlmRequestCancelled",
                error_message="LLM request cancelled.",
                duration_ms=int((time.perf_counter() - trace_started_at) * 1000),
            )
        raise
    except Exception as exc:
        if trace_enabled:
            http_trace_service.record_failed(
                started_record=trace_record,
                context=http_trace_context,
                error_type=exc.__class__.__name__,
                error_message=str(exc),
                duration_ms=int((time.perf_counter() - trace_started_at) * 1000),
            )
        raise


def _aggregate_openai_responses_sse(text: str) -> dict[str, Any]:
    response_body: dict[str, Any] | None = None
    output_items: list[dict[str, Any]] = []
    for event_type, data_text in _iter_sse_events(text):
        if data_text == "[DONE]":
            continue
        data = json.loads(data_text)
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("response"), dict):
            response_body = copy.deepcopy(data["response"])
        data_type = data.get("type") or event_type
        if data_type == "response.output_item.done" and isinstance(data.get("item"), dict):
            _upsert_output_item(output_items, copy.deepcopy(data["item"]))
        elif data_type == "response.completed":
            if response_body is None:
                response_body = {}
            if not response_body.get("output") and output_items:
                response_body["output"] = output_items
            return response_body
        elif data_type in {"response.failed", "response.incomplete", "error"}:
            raise RuntimeError(f"OpenAI Responses 流式响应失败: {data_text[:500]}")
    if response_body is None:
        response_body = {}
    if not response_body.get("output") and output_items:
        response_body["output"] = output_items
    return response_body


def _read_sse_response_text(
    response: Any,
    content_type: str,
    handler: StreamEventHandler | None,
    cancellation_checker: CancellationChecker | None,
) -> str:
    _emit_stream_event(handler, "llm.response.stream.started", {"content_type": content_type})
    event_count = 0
    lines: list[str] = []
    event_lines: list[str] = []
    try:
        while True:
            _raise_if_cancelled(cancellation_checker)
            raw_line = response.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace")
            lines.append(line)
            if line in ("\n", "\r\n", "\r"):
                event_count += _emit_sse_block_event(handler, event_lines)
                event_lines = []
            else:
                event_lines.append(line)
        if event_lines:
            event_count += _emit_sse_block_event(handler, event_lines)
        _emit_stream_event(handler, "llm.response.stream.completed", {"event_count": event_count})
        return "".join(lines)
    except Exception as exc:
        _emit_stream_event(
            handler,
            "llm.response.stream.failed",
            {"event_count": event_count, "error_type": exc.__class__.__name__, "error_message": str(exc)},
        )
        raise


def _raise_if_cancelled(cancellation_checker: CancellationChecker | None) -> None:
    if cancellation_checker is not None and cancellation_checker():
        raise LlmRequestCancelled("LLM request cancelled.")


def _emit_sse_block_event(handler: StreamEventHandler | None, event_lines: list[str]) -> int:
    block = "".join(event_lines)
    parsed = list(_iter_sse_events(block + "\n\n"))
    emitted = 0
    for event_name, data_text in parsed:
        payload: dict[str, Any] = {
            "event": event_name,
            "data_preview": data_text[:500],
        }
        if data_text == "[DONE]":
            payload["done"] = True
        else:
            try:
                data = json.loads(data_text)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                payload["data_type"] = data.get("type")
                payload["data"] = data
        _emit_stream_event(handler, "llm.response.stream.event", payload)
        emitted += 1
    return emitted


def _emit_stream_event(
    handler: StreamEventHandler | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    if handler is not None:
        handler({"event_type": event_type, "payload": payload})


def _count_sse_events(text: str) -> int:
    return sum(1 for _event_type, _data_text in _iter_sse_events(text))


def _aggregate_chat_completions_sse(text: str) -> dict[str, Any]:
    response: dict[str, Any] = {"choices": [{"index": 0, "message": {"role": "assistant"}}]}
    message = response["choices"][0]["message"]
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls_by_index: dict[int, dict[str, Any]] = {}
    finish_reason: Any = None
    for _, data_text in _iter_sse_events(text):
        if data_text == "[DONE]":
            continue
        data = json.loads(data_text)
        if not isinstance(data, dict):
            continue
        for key in ("id", "object", "created", "model", "system_fingerprint"):
            if key in data:
                response[key] = data[key]
        if data.get("usage") is not None:
            response["usage"] = data["usage"]
        choices = data.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        if choice.get("finish_reason") is not None:
            finish_reason = choice.get("finish_reason")
        delta = choice.get("delta") or {}
        if delta.get("role"):
            message["role"] = delta["role"]
        if isinstance(delta.get("content"), str):
            content_parts.append(delta["content"])
        if isinstance(delta.get("reasoning_content"), str):
            reasoning_parts.append(delta["reasoning_content"])
        for tool_call_delta in delta.get("tool_calls") or []:
            index = int(tool_call_delta.get("index", len(tool_calls_by_index)))
            tool_call = tool_calls_by_index.setdefault(index, {"function": {}})
            if tool_call_delta.get("id"):
                tool_call["id"] = tool_call_delta["id"]
            if tool_call_delta.get("type"):
                tool_call["type"] = tool_call_delta["type"]
            function_delta = tool_call_delta.get("function") or {}
            function = tool_call.setdefault("function", {})
            if function_delta.get("name"):
                function["name"] = function_delta["name"]
            if isinstance(function_delta.get("arguments"), str):
                function["arguments"] = function.get("arguments", "") + function_delta["arguments"]
    message["content"] = "".join(content_parts)
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls_by_index:
        message["tool_calls"] = [tool_calls_by_index[index] for index in sorted(tool_calls_by_index)]
    response["choices"][0]["finish_reason"] = finish_reason
    return response


def _aggregate_anthropic_messages_sse(text: str) -> dict[str, Any]:
    response: dict[str, Any] = {}
    content_blocks: dict[int, dict[str, Any]] = {}
    input_json_parts: dict[int, list[str]] = {}
    for _, data_text in _iter_sse_events(text):
        if data_text == "[DONE]":
            continue
        data = json.loads(data_text)
        if not isinstance(data, dict):
            continue
        data_type = data.get("type")
        if data_type == "message_start" and isinstance(data.get("message"), dict):
            response = copy.deepcopy(data["message"])
        elif data_type == "content_block_start" and isinstance(data.get("content_block"), dict):
            content_blocks[int(data.get("index", 0))] = copy.deepcopy(data["content_block"])
        elif data_type == "content_block_delta" and isinstance(data.get("delta"), dict):
            index = int(data.get("index", 0))
            block = content_blocks.setdefault(index, {"type": "text", "text": ""})
            delta = data["delta"]
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif delta_type == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif delta_type == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.get("signature", "")
            elif delta_type == "input_json_delta":
                input_json_parts.setdefault(index, []).append(delta.get("partial_json", ""))
        elif data_type == "message_delta":
            response.update(data.get("delta") or {})
            if data.get("usage") is not None:
                response["usage"] = data["usage"]
    for index, parts in input_json_parts.items():
        block = content_blocks.setdefault(index, {"type": "tool_use"})
        partial_json = "".join(parts)
        try:
            block["input"] = json.loads(partial_json) if partial_json else {}
        except json.JSONDecodeError:
            block["input"] = partial_json
    response["content"] = [content_blocks[index] for index in sorted(content_blocks)]
    return response


def _aggregate_gemini_generate_content_sse(text: str) -> dict[str, Any]:
    response: dict[str, Any] = {"candidates": [{"content": {"role": "model", "parts": []}}]}
    output_candidate = response["candidates"][0]
    output_content = output_candidate["content"]
    output_parts = output_content["parts"]
    for _, data_text in _iter_sse_events(text):
        if data_text == "[DONE]":
            continue
        data = json.loads(data_text)
        if not isinstance(data, dict):
            continue
        if data.get("usageMetadata") is not None:
            response["usageMetadata"] = data["usageMetadata"]
        candidates = data.get("candidates") or []
        if not candidates:
            continue
        candidate = candidates[0]
        for key, value in candidate.items():
            if key != "content":
                output_candidate[key] = value
        content = candidate.get("content") or {}
        if content.get("role"):
            output_content["role"] = content["role"]
        for part in content.get("parts") or []:
            _append_gemini_part(output_parts, part)
    return response


def _append_gemini_part(output_parts: list[dict[str, Any]], part: dict[str, Any]) -> None:
    if "text" in part and not any(key in part for key in ("functionCall", "functionResponse", "inlineData", "fileData")):
        thought = part.get("thought") is True
        if output_parts and "text" in output_parts[-1] and (output_parts[-1].get("thought") is True) is thought:
            output_parts[-1]["text"] = output_parts[-1].get("text", "") + part.get("text", "")
            if part.get("thoughtSignature"):
                output_parts[-1]["thoughtSignature"] = part["thoughtSignature"]
            return
    output_parts.append(copy.deepcopy(part))


def _iter_sse_events(text: str):
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for block in normalized.split("\n\n"):
        event_type = "message"
        data_lines: list[str] = []
        for raw_line in block.split("\n"):
            if not raw_line or raw_line.startswith(":"):
                continue
            field, separator, value = raw_line.partition(":")
            if not separator:
                continue
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event_type = value
            elif field == "data":
                data_lines.append(value)
        if data_lines:
            yield event_type, "\n".join(data_lines)


def _upsert_output_item(output_items: list[dict[str, Any]], item: dict[str, Any]) -> None:
    item_id = item.get("id")
    if item_id is not None:
        for index, current in enumerate(output_items):
            if current.get("id") == item_id:
                output_items[index] = item
                return
    output_items.append(item)


class LlmHttpRequestError(RuntimeError):
    """LLM HTTP 请求失败。"""

    def __init__(self, status_code: int, response_text: str) -> None:
        self.status_code = status_code
        self.response_text = response_text
        super().__init__(f"LLM HTTP 请求失败: status={status_code}, body={response_text[:500]}")

# -*- coding: utf-8 -*-
"""通用 LLM 请求领域服务。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import replace
from typing import Any

from agent_runtime.conversation_message.message.operations import (
    ingest_response,
    render_messages,
)
from agent_runtime.conversation_message.message.models import ThinkingPart, ToolUsePart
from agent_runtime.communication.runtime_event_publisher import (
    NoopRuntimeEventPublisher,
    RuntimeEventPublisher,
)
from agent_runtime.llm_client.llm_protocol_clients import (
    LlmHttpRequestError,
    LlmProtocolClient,
)
from agent_runtime.llm_client.llm_request_models import (
    LlmPromptRequest,
    LlmPromptResponse,
)

_ENV_VAR_PATTERN = re.compile(r"^\$\{([^}:]+)(?::-([^}]*))?\}$")


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
    ) -> None:
        # 具体 API 协议客户端表。key 是 api_format，如 openai_responses。
        self._protocol_clients = {client.api_format: client for client in protocol_clients}

        # 多模态资源解析器。文本对话不会触发资源读取；图片/PDF 渲染时才会使用。
        self._resource_resolver = resource_resolver

        # 运行时事件发布器。用于把 LLM 请求关键节点推送到通用通信流。
        self._event_publisher = event_publisher or NoopRuntimeEventPublisher()

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
        }
        rendered_messages = await render_messages(
            request.conversation,
            resolved.api_format,
            self._resource_resolver,
            render_options,
        )
        tool_schemas = tuple(tool.schema for tool in request.tools)
        api_keys: list[str | None] = [item.key for item in resolved.api_keys]
        if not api_keys:
            api_keys = [None]

        request_started_at = time.perf_counter()
        await self._event_publisher.publish(
            request.communication_context,
            "llm.request.started",
            {
                "provider_id": resolved.provider_id,
                "binding_id": resolved.binding_id,
                "model_id": resolved.model_id,
                "model_alias": resolved.model_alias,
                "api_format": resolved.api_format,
                "tool_count": len(tool_schemas),
                "api_key_count": len(api_keys),
            },
            source="llm_client",
            metadata={"conversation_id": request.conversation.id},
        )

        attempt_count = 0
        last_error: Exception | None = None
        last_service_call_error: Exception | None = None
        for key_index, raw_api_key in enumerate(api_keys):
            try:
                api_key = _resolve_api_key(raw_api_key) if raw_api_key is not None else None
            except Exception as exc:
                last_error = exc
                await self._event_publisher.publish(
                    request.communication_context,
                    "llm.request.api_key.failed",
                    {
                        "provider_id": resolved.provider_id,
                        "binding_id": resolved.binding_id,
                        "model_alias": resolved.model_alias,
                        "api_key_index": key_index,
                        "attempt_count": attempt_count,
                        "error_type": exc.__class__.__name__,
                        "error_message": str(exc),
                        "will_try_next_key": key_index < len(api_keys) - 1,
                    },
                    source="llm_client",
                    metadata={"conversation_id": request.conversation.id},
                )
                continue
            max_retries = resolved.retry.max_retries_per_key
            for retry_index in range(max_retries + 1):
                attempt_count += 1
                try:
                    await self._event_publisher.publish(
                        request.communication_context,
                        "llm.request.attempt.started",
                        {
                            "provider_id": resolved.provider_id,
                            "binding_id": resolved.binding_id,
                            "model_alias": resolved.model_alias,
                            "api_key_index": key_index,
                            "retry_index": retry_index,
                            "attempt_count": attempt_count,
                        },
                        source="llm_client",
                        metadata={"conversation_id": request.conversation.id},
                    )
                    raw_response = await client.request(
                        resolved_model=resolved,
                        rendered_messages=rendered_messages,
                        system_prompt=request.system_prompt,
                        tools=tool_schemas,
                        request_params=request_params,
                        api_key=api_key,
                    )
                except Exception as exc:  # noqa: BLE001 - 需要统一处理不同 SDK/HTTP 异常
                    if not _is_llm_service_call_failure(exc):
                        raise
                    last_error = exc
                    last_service_call_error = exc
                    await self._event_publisher.publish(
                        request.communication_context,
                        "llm.request.attempt.failed",
                        {
                            "provider_id": resolved.provider_id,
                            "binding_id": resolved.binding_id,
                            "model_alias": resolved.model_alias,
                            "api_key_index": key_index,
                            "retry_index": retry_index,
                            "attempt_count": attempt_count,
                            "error_type": exc.__class__.__name__,
                            "error_message": str(exc),
                        },
                        source="llm_client",
                        metadata={"conversation_id": request.conversation.id},
                    )
                    if not _should_retry(exc, resolved.retry.retryable_status_codes):
                        await self._event_publisher.publish(
                            request.communication_context,
                            "llm.request.api_key.failed",
                            {
                                "provider_id": resolved.provider_id,
                                "binding_id": resolved.binding_id,
                                "model_alias": resolved.model_alias,
                                "api_key_index": key_index,
                                "attempt_count": attempt_count,
                                "error_type": exc.__class__.__name__,
                                "error_message": str(exc),
                                "will_try_next_key": key_index < len(api_keys) - 1,
                            },
                            source="llm_client",
                            metadata={"conversation_id": request.conversation.id},
                        )
                        break
                    if retry_index >= max_retries:
                        await self._event_publisher.publish(
                            request.communication_context,
                            "llm.request.api_key.failed",
                            {
                                "provider_id": resolved.provider_id,
                                "binding_id": resolved.binding_id,
                                "model_alias": resolved.model_alias,
                                "api_key_index": key_index,
                                "attempt_count": attempt_count,
                                "error_type": exc.__class__.__name__,
                                "error_message": str(exc),
                                "will_try_next_key": key_index < len(api_keys) - 1,
                            },
                            source="llm_client",
                            metadata={"conversation_id": request.conversation.id},
                        )
                        break
                    await asyncio.sleep(_retry_delay(resolved.retry.initial_delay_seconds, resolved.retry.max_delay_seconds, retry_index))
                else:
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
                    await self._event_publisher.publish(
                        request.communication_context,
                        "llm.request.completed",
                        {
                            "provider_id": resolved.provider_id,
                            "binding_id": resolved.binding_id,
                            "model_alias": resolved.model_alias,
                            "api_key_index": key_index,
                            "attempt_count": attempt_count,
                            "assistant_part_count": len(assistant_message.content),
                            "thinking_part_count": _count_parts(assistant_message.content, ThinkingPart),
                            "tool_use_part_count": _count_parts(assistant_message.content, ToolUsePart),
                            "duration_ms": int((time.perf_counter() - request_started_at) * 1000),
                        },
                        source="llm_client",
                        metadata={"conversation_id": request.conversation.id},
                    )
                    if _count_parts(assistant_message.content, ThinkingPart) > 0:
                        await self._event_publisher.publish(
                            request.communication_context,
                            "llm.thinking.received",
                            {
                                "provider_id": resolved.provider_id,
                                "binding_id": resolved.binding_id,
                                "model_alias": resolved.model_alias,
                                "thinking_part_count": _count_parts(assistant_message.content, ThinkingPart),
                            },
                            source="llm_client",
                            metadata={"conversation_id": request.conversation.id},
                        )
                    return LlmPromptResponse(
                        raw_response=raw_response,
                        assistant_message=assistant_message,
                        api_key_index=key_index,
                        attempt_count=attempt_count,
                    )
        if last_error is not None:
            await self._event_publisher.publish(
                request.communication_context,
                "llm.request.failed",
                {
                    "provider_id": resolved.provider_id,
                    "binding_id": resolved.binding_id,
                    "model_alias": resolved.model_alias,
                    "attempt_count": attempt_count,
                    "error_type": last_error.__class__.__name__,
                    "error_message": str(last_error),
                },
                source="llm_client",
                metadata={"conversation_id": request.conversation.id},
            )
            if last_service_call_error is not None:
                raise LlmProviderModelServiceUnavailable(
                    provider_id=resolved.provider_id,
                    binding_id=resolved.binding_id,
                    attempt_count=attempt_count,
                    cause=last_service_call_error,
                ) from last_service_call_error
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
    return merged


def _with_default_relay_session_header(request: LlmPromptRequest):
    headers = dict(request.resolved_model.custom_headers)
    if not any(key.lower() == "x-relay-session-id" for key in headers):
        headers["x-relay-session-id"] = request.conversation.id
    return replace(request.resolved_model, custom_headers=headers)


def _merge_json_field(target: dict[str, Any], json_text: str | None, fallback_key: str) -> None:
    """把用户自定义 JSON 字符串合并到请求参数。"""
    if json_text is None:
        return
    value = json.loads(json_text)
    if isinstance(value, dict):
        target.update(value)
    else:
        target[fallback_key] = value


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


def _should_retry(exc: Exception, retryable_status_codes: tuple[int, ...]) -> bool:
    """判断异常是否应该按重试策略重试。"""
    if isinstance(exc, LlmHttpRequestError):
        return exc.status_code in retryable_status_codes
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


def _is_llm_service_call_failure(exc: Exception) -> bool:
    return isinstance(exc, (LlmHttpRequestError, TimeoutError, ConnectionError, OSError))


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


def _count_parts(parts: Any, part_type: type) -> int:
    """统计 assistant 响应中指定类型内容片段数量。"""
    return sum(1 for part in parts if isinstance(part, part_type))

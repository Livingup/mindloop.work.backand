# -*- coding: utf-8 -*-
"""通用 LLM 请求客户端的南向协议适配器。"""
from __future__ import annotations

import json
import copy
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, is_dataclass
from typing import Any, Protocol

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
        url = _join_url(resolved_model.api_base_url, "responses")
        headers = _json_headers(resolved_model.custom_headers)
        if api_key:
            headers.setdefault("Authorization", f"Bearer {api_key}")
        return await _post_json(url, headers, body)


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
        url = _join_url(resolved_model.api_base_url, "chat/completions")
        headers = _json_headers(resolved_model.custom_headers)
        if api_key:
            headers.setdefault("Authorization", f"Bearer {api_key}")
        return await _post_json(url, headers, body)


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
        url = _join_url(resolved_model.api_base_url, "v1/messages")
        headers = _json_headers(resolved_model.custom_headers)
        if api_key:
            headers.setdefault("x-api-key", api_key)
        headers.setdefault("anthropic-version", "2023-06-01")
        return await _post_json(url, headers, body)


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
    ) -> Any:
        if not isinstance(rendered_messages, GeminiRenderResult):
            raise TypeError("gemini_generate_content 渲染结果类型不正确。")
        body: dict[str, Any] = {
            "contents": rendered_messages.contents,
            **request_params,
        }
        if system_prompt:
            body.setdefault("systemInstruction", {"parts": [{"text": system_prompt}]})
        if rendered_messages.cached_content:
            body.setdefault("cachedContent", rendered_messages.cached_content)
        if tools:
            body.setdefault("tools", [{"functionDeclarations": list(tools)}])
        url = _join_url(
            resolved_model.api_base_url,
            f"v1beta/models/{urllib.parse.quote(resolved_model.model_alias, safe='')}:generateContent",
        )
        if api_key and "key=" not in url:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}key={urllib.parse.quote(api_key)}"
        headers = _json_headers(resolved_model.custom_headers)
        if api_key:
            headers.setdefault("x-goog-api-key", api_key)
        return await _post_json(url, headers, body)


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
        url = _join_url(resolved_model.api_base_url, "chat/completions")
        headers = _json_headers(resolved_model.custom_headers)
        if api_key:
            headers.setdefault("Authorization", f"Bearer {api_key}")
        return await _post_json(url, headers, body)


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
    headers.setdefault("Content-Type", "application/json")
    return headers


def _join_url(base_url: str, path: str) -> str:
    """拼接 API base URL 和相对路径。"""
    return base_url.rstrip("/") + "/" + path.lstrip("/")


async def _post_json(url: str, headers: dict[str, str], body: dict[str, Any]) -> Any:
    """在线程池中执行阻塞式 JSON POST，避免额外引入 HTTP 客户端依赖。"""
    import asyncio

    return await asyncio.to_thread(_post_json_sync, url, headers, body)


def _post_json_sync(url: str, headers: dict[str, str], body: dict[str, Any]) -> Any:
    """同步 JSON POST，并把 HTTP 错误转换为带 status_code 的异常。"""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url=url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            text = response.read().decode("utf-8")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        raise LlmHttpRequestError(exc.code, error_text) from exc


class LlmHttpRequestError(RuntimeError):
    """LLM HTTP 请求失败。"""

    def __init__(self, status_code: int, response_text: str) -> None:
        self.status_code = status_code
        self.response_text = response_text
        super().__init__(f"LLM HTTP 请求失败: status={status_code}, body={response_text[:500]}")

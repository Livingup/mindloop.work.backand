from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from agent_runtime.llm_api_protocol import (
    API_FORMAT_ANTHROPIC_MESSAGES,
    API_FORMAT_CODEX_RESPONSES,
    API_FORMAT_GEMINI_GENERATE_CONTENT,
    API_FORMAT_OPENAI_RESPONSES,
    API_FORMAT_ZHIPU_CHAT_COMPLETIONS,
)
from agent_runtime.web_tools.models import ReadUrlContextRequest, WebSearchRequest, WebToolOperation


class WebNativeToolAdapter(Protocol):
    @property
    def api_format(self) -> str:
        ...

    def build_request_overrides(
        self,
        *,
        operation: WebToolOperation,
        request: WebSearchRequest | ReadUrlContextRequest,
    ) -> dict[str, Any]:
        ...


DEFAULT_OPENAI_USER_LOCATION: dict[str, Any] = {
    "type": "approximate",
    "country": "US",
    "region": "California",
    "city": "Los Angeles",
    "timezone": "America/Los_Angeles",
}


@dataclass(frozen=True, slots=True)
class OpenAIWebNativeToolAdapter:
    api_format: str = API_FORMAT_OPENAI_RESPONSES
    tool_type: str = "web_search"
    external_web_access: bool = True
    indexed_web_access: bool = True
    search_context_size: str = "medium"
    search_content_types: tuple[str, ...] = ("text", "image")

    def build_request_overrides(
        self,
        *,
        operation: WebToolOperation,
        request: WebSearchRequest | ReadUrlContextRequest,
    ) -> dict[str, Any]:
        tool: dict[str, Any] = {
            "type": self.tool_type,
            "external_web_access": self.external_web_access,
            "indexed_web_access": self.indexed_web_access,
            "search_context_size": self.search_context_size,
            "search_content_types": list(self.search_content_types),
            "user_location": dict(DEFAULT_OPENAI_USER_LOCATION),
        }
        if isinstance(request, WebSearchRequest):
            filters = _domain_filters(request.allowed_domains, request.blocked_domains)
            if filters:
                tool["filters"] = filters
        overrides = {
            "tools": [tool],
            "tool_choice": {"type": self.tool_type},
            "include": ["web_search_call.action.sources"],
        }
        if self.api_format == API_FORMAT_OPENAI_RESPONSES:
            overrides["openai_responses_input_configuration"] = True
        return overrides


@dataclass(frozen=True, slots=True)
class ClaudeWebNativeToolAdapter:
    api_format: str = API_FORMAT_ANTHROPIC_MESSAGES

    def build_request_overrides(
        self,
        *,
        operation: WebToolOperation,
        request: WebSearchRequest | ReadUrlContextRequest,
    ) -> dict[str, Any]:
        tool: dict[str, Any] = {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
        }
        if isinstance(request, WebSearchRequest):
            if request.allowed_domains:
                tool["allowed_domains"] = list(request.allowed_domains)
            if request.blocked_domains:
                tool["blocked_domains"] = list(request.blocked_domains)
            tool["max_uses"] = _claude_max_uses(request.max_results)
        return {
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": "web_search"},
        }


@dataclass(frozen=True, slots=True)
class GeminiWebNativeToolAdapter:
    api_format: str = API_FORMAT_GEMINI_GENERATE_CONTENT

    def build_request_overrides(
        self,
        *,
        operation: WebToolOperation,
        request: WebSearchRequest | ReadUrlContextRequest,
    ) -> dict[str, Any]:
        tools: list[dict[str, Any]] = [{"googleSearch": {}}]
        if operation == "read_url_context":
            tools.append({"urlContext": {}})
        return {"tools": tools}


@dataclass(frozen=True, slots=True)
class ZhipuWebNativeToolAdapter:
    api_format: str = API_FORMAT_ZHIPU_CHAT_COMPLETIONS

    def build_request_overrides(
        self,
        *,
        operation: WebToolOperation,
        request: WebSearchRequest | ReadUrlContextRequest,
    ) -> dict[str, Any]:
        web_search: dict[str, Any] = {
            "enable": True,
            "search_engine": "search_pro",
            "search_result": True,
            "content_size": "high",
            "require_search": True,
        }
        if isinstance(request, WebSearchRequest):
            web_search["count"] = _bounded_count(request.max_results)
            if request.allowed_domains:
                web_search["search_domain_filter"] = ",".join(request.allowed_domains)
            if request.recency:
                web_search["search_recency_filter"] = request.recency
        return {
            "tools": [{"type": "web_search", "web_search": web_search}],
            "tool_choice": "auto",
        }


class WebNativeToolAdapterRegistry:
    def __init__(self, adapters: tuple[WebNativeToolAdapter, ...] | None = None) -> None:
        self._adapters = {
            adapter.api_format: adapter
            for adapter in (
                adapters
                or (
                    OpenAIWebNativeToolAdapter(),
                    OpenAIWebNativeToolAdapter(api_format=API_FORMAT_CODEX_RESPONSES),
                    ClaudeWebNativeToolAdapter(),
                    GeminiWebNativeToolAdapter(),
                    ZhipuWebNativeToolAdapter(),
                )
            )
        }

    def get(self, api_format: str) -> WebNativeToolAdapter:
        adapter = self._adapters.get(api_format)
        if adapter is None:
            raise ValueError(f"当前网络工具不支持 api_format={api_format}。")
        return adapter

    def supported_api_formats(self) -> tuple[str, ...]:
        return tuple(self._adapters.keys())


def build_web_tool_system_prompt(operation: WebToolOperation) -> str:
    if operation == "web_search":
        task = "Perform a live web search for the user query and synthesize the most relevant current information."
    else:
        task = "Read the target URL with live web or URL context capability and synthesize the page context relevant to the user request."
    schema = {
        "summary": "concise answer in the user's language",
        "results": [
            {
                "title": "source title",
                "url": "source URL",
                "snippet": "short source-grounded snippet",
                "source": "site or publisher name if known",
                "published_at": "publication date if known, otherwise null",
            }
        ],
        "images": [
            {
                "url": "direct image URL",
                "title": "image title or caption if known",
                "source_url": "page URL where the image was found if known",
            }
        ],
        "key_points": ["important source-grounded facts"],
        "citations": [
            {
                "url": "cited URL",
                "title": "cited title if known",
            }
        ],
    }
    return (
        f"{task}\n"
        "Use only information obtained from the provider's native web search or URL context capability when available. "
        "Return accurate search findings, citing real URLs and relevant images when available. "
        "When returning JSON, output exactly one JSON object with this shape: "
        f"{json.dumps(schema, ensure_ascii=False)}"
    )


def build_web_search_user_prompt(request: WebSearchRequest) -> str:
    payload = {
        "query": request.query,
        "max_results": _bounded_count(request.max_results),
        "allowed_domains": list(request.allowed_domains),
        "blocked_domains": list(request.blocked_domains),
        "recency": request.recency,
    }
    return "Search the web for this request and return the required JSON object: " + json.dumps(payload, ensure_ascii=False)


def build_read_url_user_prompt(request: ReadUrlContextRequest) -> str:
    payload = {
        "url": request.url,
        "question": request.question,
        "max_summary_tokens": request.max_summary_tokens,
    }
    return "Read this URL and return the required JSON object: " + json.dumps(payload, ensure_ascii=False)


def _domain_filters(allowed_domains: tuple[str, ...], blocked_domains: tuple[str, ...]) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if allowed_domains:
        filters["allowed_domains"] = list(allowed_domains)
    if blocked_domains:
        filters["blocked_domains"] = list(blocked_domains)
    return filters


def _bounded_count(value: int) -> int:
    return max(1, min(50, int(value)))


def _openai_context_size(max_results: int) -> str:
    if max_results <= 3:
        return "low"
    if max_results <= 10:
        return "medium"
    return "high"


def _claude_max_uses(max_results: int) -> int:
    if max_results <= 3:
        return 2
    if max_results <= 10:
        return 5
    return 8

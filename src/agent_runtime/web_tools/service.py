from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent_runtime.conversation_message.message.models import TextPart, TextPartMeta
from agent_runtime.conversation_message.message.operations import create_conversation
from agent_runtime.llm_client.llm_request_models import LlmPromptRequest
from agent_runtime.llm_client.llm_request_service import LlmRequestService
from agent_runtime.llm_model_registry.llm_model_registry_service import ResolvedLlmProviderModel
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import LlmModelRegistryGateway
from agent_runtime.web_tools.adapters import (
    WebNativeToolAdapterRegistry,
    build_read_url_user_prompt,
    build_web_search_user_prompt,
    build_web_tool_system_prompt,
)
from agent_runtime.web_tools.models import (
    ReadUrlContextRequest,
    WebSearchRequest,
    WebToolOperation,
    WebToolPolicyCandidate,
    WebToolResult,
    WebToolsRuntimeSettings,
)
from agent_runtime.web_tools.settings_repository import WebToolsSettingsRepository


class WebToolsConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedWebToolCandidate:
    candidate: WebToolPolicyCandidate
    resolved_model: ResolvedLlmProviderModel


class WebToolsService:
    def __init__(
        self,
        *,
        settings_repository: WebToolsSettingsRepository,
        llm_registry_gateway: LlmModelRegistryGateway,
        llm_request_service: LlmRequestService,
        adapter_registry: WebNativeToolAdapterRegistry | None = None,
    ) -> None:
        self._settings_repository = settings_repository
        self._llm_registry_gateway = llm_registry_gateway
        self._llm_request_service = llm_request_service
        self._adapter_registry = adapter_registry or WebNativeToolAdapterRegistry()

    def get_settings(self) -> WebToolsRuntimeSettings:
        return self._settings_repository.get_settings()

    def save_settings(
        self,
        *,
        candidates: tuple[WebToolPolicyCandidate, ...],
    ) -> WebToolsRuntimeSettings:
        for candidate in candidates:
            self._require_supported_candidate(candidate)
        return self._settings_repository.save_settings(candidates=candidates)

    def validate_settings(self) -> None:
        self._resolve_enabled_candidates()

    def supported_api_formats(self) -> tuple[str, ...]:
        return self._adapter_registry.supported_api_formats()

    async def web_search(self, request: WebSearchRequest) -> WebToolResult:
        query = request.query.strip()
        if not query:
            raise ValueError("query must not be empty.")
        normalized = WebSearchRequest(
            query=query,
            max_results=max(1, min(50, int(request.max_results))),
            allowed_domains=_normalize_domains(request.allowed_domains),
            blocked_domains=_normalize_domains(request.blocked_domains),
            recency=request.recency.strip() if isinstance(request.recency, str) and request.recency.strip() else None,
        )
        return await self._execute("web_search", normalized)

    async def read_url_context(self, request: ReadUrlContextRequest) -> WebToolResult:
        url = request.url.strip()
        if not url:
            raise ValueError("url must not be empty.")
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://.")
        normalized = ReadUrlContextRequest(
            url=url,
            question=request.question.strip() if isinstance(request.question, str) and request.question.strip() else None,
            max_summary_tokens=request.max_summary_tokens,
        )
        return await self._execute("read_url_context", normalized)

    async def _execute(
        self,
        operation: WebToolOperation,
        request: WebSearchRequest | ReadUrlContextRequest,
    ) -> WebToolResult:
        resolved_candidates = self._resolve_enabled_candidates()
        last_error: Exception | None = None
        for candidate_index, item in enumerate(resolved_candidates):
            resolved = item.resolved_model
            try:
                adapter = self._adapter_registry.get(resolved.api_format)
                request_overrides = adapter.build_request_overrides(operation=operation, request=request)
                conversation = create_conversation()
                conversation.add_user_message(
                    [TextPart(text=_build_user_prompt(operation, request))],
                    [TextPartMeta()],
                )
                response = await self._llm_request_service.send_prompt(
                    LlmPromptRequest(
                        conversation=conversation,
                        resolved_model=resolved,
                        system_prompt=build_web_tool_system_prompt(operation),
                        request_overrides=request_overrides,
                        communication_context=None,
                        llm_call_id=None,
                        candidate_index=candidate_index,
                        next_resolved_model=(
                            resolved_candidates[candidate_index + 1].resolved_model
                            if candidate_index < len(resolved_candidates) - 1
                            else None
                        ),
                    )
                )
                content = _assistant_text(response.assistant_message.content)
                data = _parse_result_data(content)
                _enrich_sources(data, response.raw_response)
                return WebToolResult(
                    operation=operation,
                    content=content,
                    data=data,
                    provider_id=resolved.provider_id,
                    binding_id=resolved.binding_id,
                    model_id=resolved.model_id,
                    model_alias=resolved.model_alias,
                    api_format=resolved.api_format,
                    candidate_index=candidate_index,
                    attempt_count=response.attempt_count,
                    api_key_index=response.api_key_index,
                )
            except Exception as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise RuntimeError(f"Web tool {operation} failed after {len(resolved_candidates)} candidates: {last_error}") from last_error
        raise WebToolsConfigurationError("Web tools provider policy has no usable candidates.")

    def _resolve_enabled_candidates(self) -> tuple[ResolvedWebToolCandidate, ...]:
        settings = self._settings_repository.get_settings()
        enabled = settings.enabled_candidates()
        if not enabled:
            raise WebToolsConfigurationError("Web tools provider policy is not configured.")
        resolved_items: list[ResolvedWebToolCandidate] = []
        for candidate in enabled:
            resolved = self._llm_registry_gateway.resolve_provider_model(
                candidate.provider_id,
                candidate.model_binding_id,
            )
            self._adapter_registry.get(resolved.api_format)
            resolved_items.append(ResolvedWebToolCandidate(candidate=candidate, resolved_model=resolved))
        return tuple(resolved_items)

    def _require_supported_candidate(self, candidate: WebToolPolicyCandidate) -> None:
        resolved = self._llm_registry_gateway.resolve_provider_model(
            candidate.provider_id,
            candidate.model_binding_id,
        )
        self._adapter_registry.get(resolved.api_format)


def _build_user_prompt(
    operation: WebToolOperation,
    request: WebSearchRequest | ReadUrlContextRequest,
) -> str:
    if operation == "web_search" and isinstance(request, WebSearchRequest):
        return build_web_search_user_prompt(request)
    if operation == "read_url_context" and isinstance(request, ReadUrlContextRequest):
        return build_read_url_user_prompt(request)
    raise TypeError(f"Invalid web tool request for operation={operation}.")


def _assistant_text(parts: list[Any]) -> str:
    texts: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            texts.append(text)
    return "\n".join(texts).strip()


def _parse_result_data(content: str) -> dict[str, Any]:
    extracted = _extract_json_object(content)
    if extracted is not None:
        try:
            value = json.loads(extracted)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    return {
        "summary": content,
        "results": [],
        "key_points": [],
        "citations": [],
    }


def _extract_json_object(text: str) -> str | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    return None


def _enrich_sources(data: dict[str, Any], raw_response: Any) -> None:
    citations = data.get("citations")
    if not isinstance(citations, list):
        citations = []
        data["citations"] = citations
    existing = {item.get("url") for item in citations if isinstance(item, dict)}
    for url in _collect_urls(raw_response):
        if url in existing:
            continue
        citations.append({"url": url, "title": None})
        existing.add(url)


def _collect_urls(value: Any) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, sub_value in item.items():
                if key.lower() in {"url", "uri", "link"} and isinstance(sub_value, str) and sub_value.startswith(("http://", "https://")):
                    if sub_value not in seen:
                        seen.add(sub_value)
                        urls.append(sub_value)
                visit(sub_value)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return urls


def _normalize_domains(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip().lower()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return tuple(normalized)

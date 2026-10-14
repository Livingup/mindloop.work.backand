from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass
from typing import Any

from agent_runtime.conversation_message.message.models import TextPart, TextPartMeta, ThinkingPart
from agent_runtime.conversation_message.message.operations import create_conversation
from agent_runtime.llm_client.llm_protocol_clients import LlmRequestCancelled
from agent_runtime.llm_client.llm_request_models import LlmPromptRequest
from agent_runtime.llm_client.llm_request_service import (
    LlmProviderModelServiceUnavailable,
    LlmRequestService,
 )
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


WEB_TOOL_BATCH_CONCURRENCY = 3
WEB_SEARCH_MAX_BATCH_QUERIES = 10
READ_URL_CONTEXT_MAX_BATCH_URLS = 10


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
        queries = _normalize_text_batch(
            request.query,
            request.additional_queries,
            max_items=WEB_SEARCH_MAX_BATCH_QUERIES,
        )
        if not queries:
            raise ValueError("query must not be empty.")
        max_results = max(1, min(50, int(request.max_results)))
        allowed_domains = _normalize_domains(request.allowed_domains)
        blocked_domains = _normalize_domains(request.blocked_domains)
        recency = request.recency.strip() if isinstance(request.recency, str) and request.recency.strip() else None
        requests = tuple(
            WebSearchRequest(
                query=query,
                max_results=max_results,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                recency=recency,
            )
            for query in queries
        )
        if len(requests) == 1:
            return await self._execute_single("web_search", requests[0])
        return await self._execute_batch(
            "web_search",
            WebSearchRequest(
                query=requests[0].query,
                additional_queries=tuple(item.query for item in requests[1:]),
                max_results=max_results,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
                recency=recency,
            ),
            requests,
        )

    async def read_url_context(self, request: ReadUrlContextRequest) -> WebToolResult:
        urls = _normalize_text_batch(
            request.url,
            request.additional_urls,
            max_items=READ_URL_CONTEXT_MAX_BATCH_URLS,
        )
        if not urls:
            raise ValueError("url must not be empty.")
        for url in urls:
            if not url.lower().startswith(("http://", "https://")):
                raise ValueError("url must start with http:// or https://.")
        question = request.question.strip() if isinstance(request.question, str) and request.question.strip() else None
        requests = tuple(
            ReadUrlContextRequest(
                url=url,
                question=question,
                max_summary_tokens=request.max_summary_tokens,
            )
            for url in urls
        )
        if len(requests) == 1:
            return await self._execute_single("read_url_context", requests[0])
        return await self._execute_batch(
            "read_url_context",
            ReadUrlContextRequest(
                url=requests[0].url,
                additional_urls=tuple(item.url for item in requests[1:]),
                question=question,
                max_summary_tokens=request.max_summary_tokens,
            ),
            requests,
        )

    async def _execute_batch(
        self,
        operation: WebToolOperation,
        batch_request: WebSearchRequest | ReadUrlContextRequest,
        requests: tuple[WebSearchRequest | ReadUrlContextRequest, ...],
    ) -> WebToolResult:
        semaphore = asyncio.Semaphore(WEB_TOOL_BATCH_CONCURRENCY)

        async def run_one(item: WebSearchRequest | ReadUrlContextRequest) -> WebToolResult:
            async with semaphore:
                return await self._execute_single(operation, item)

        outputs = await asyncio.gather(*(run_one(item) for item in requests), return_exceptions=True)
        for output in outputs:
            if isinstance(output, (LlmRequestCancelled, asyncio.CancelledError)) or bool(
                getattr(output, "partial_output", False)
            ):
                raise output
        return _merge_batch_results(operation, batch_request, requests, outputs)

    async def _execute_single(
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
                raw_content = _assistant_text(response.assistant_message.content)
                data = _parse_result_data(raw_content)
                _enrich_sources(data, response.raw_response, raw_content=raw_content)
                content = _render_html_document(operation, request, data)
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
                if isinstance(exc, LlmRequestCancelled) or bool(getattr(exc, "partial_output", False)):
                    raise
                if not isinstance(exc, LlmProviderModelServiceUnavailable):
                    raise
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
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            if isinstance(part, TextPart):
                text_parts.append(text)
            elif isinstance(part, ThinkingPart):
                thinking_parts.append(text)
            else:
                text_parts.append(text)
    if text_parts:
        return "\n".join(text_parts).strip()
    return "\n".join(thinking_parts).strip()


def _parse_result_data(content: str) -> dict[str, Any]:
    extracted = _extract_json_object(content)
    if extracted is not None:
        try:
            value = json.loads(extracted)
            if isinstance(value, dict):
                if "images" not in value or not isinstance(value["images"], list):
                    value["images"] = []
                return value
        except json.JSONDecodeError:
            pass
    return {
        "summary": content,
        "results": [],
        "images": [],
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


IMAGE_EXTENSIONS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp")


def _is_image_url(url: str) -> bool:
    if not url:
        return False
    clean = url.split("?")[0].split("#")[0].strip().lower()
    if any(clean.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return True
    if "upload.wikimedia.org" in clean and any(ext in clean for ext in IMAGE_EXTENSIONS):
        return True
    return False


def _clean_trailing_url_punct(url: str) -> str:
    while url and url[-1] in ".,;:!?'\"":
        url = url[:-1]
    if url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    return url


def _enrich_sources(data: dict[str, Any], raw_response: Any, *, raw_content: str = "") -> None:
    raw_sources, raw_images = _extract_raw_sources_and_images(raw_response)

    results = data.get("results")
    if not isinstance(results, list):
        results = []
        data["results"] = results
    existing_result_urls = {str(item.get("url") or "") for item in results if isinstance(item, dict)}
    for s in raw_sources:
        if s["url"] not in existing_result_urls:
            results.append(s)
            existing_result_urls.add(s["url"])

    citations = data.get("citations")
    if not isinstance(citations, list):
        citations = []
        data["citations"] = citations
    existing_citations = {str(item.get("url") or "") for item in citations if isinstance(item, dict)}
    for s in results:
        url = str(s.get("url") or "")
        if url and url not in existing_citations:
            citations.append({"url": url, "title": s.get("title")})
            existing_citations.add(url)
    for url in _collect_urls(raw_response):
        if url not in existing_citations:
            citations.append({"url": url, "title": None})
            existing_citations.add(url)

    if raw_content:
        for match in re.finditer(r"(?<!\!)\[([^\]]+)\]\((https?://[^\s\)]+)\)", raw_content):
            caption, link_url = match.groups()
            link_url = _clean_trailing_url_punct(link_url.strip())
            if link_url and link_url not in existing_citations:
                citations.append({"url": link_url, "title": caption.strip() or None})
                existing_citations.add(link_url)

    images = data.get("images")
    if not isinstance(images, list):
        images = []
        data["images"] = images
    existing_img_urls = {str(item.get("url") or "") for item in images if isinstance(item, dict)}
    for img in raw_images:
        if img["url"] not in existing_img_urls:
            images.append(img)
            existing_img_urls.add(img["url"])
    if raw_content:
        for img in _extract_markdown_images(raw_content):
            if img["url"] not in existing_img_urls:
                images.append(img)
                existing_img_urls.add(img["url"])


def _extract_raw_sources_and_images(raw_response: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sources: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_images: set[str] = set()

    def add_source(title: Any, url: Any, snippet: Any = None, source: Any = None, published_at: Any = None, img: Any = None) -> None:
        u = str(url or "").strip()
        if not u or not u.startswith(("http://", "https://")) or u in seen_urls:
            return
        seen_urls.add(u)
        t = str(title or "").strip() or u
        sources.append({
            "title": t,
            "url": u,
            "snippet": str(snippet or "").strip(),
            "source": str(source or "").strip(),
            "published_at": str(published_at or "").strip() or None,
        })
        if img:
            add_image(img, title=t, source_url=u)
        elif _is_image_url(u):
            add_image(u, title=t, source_url=u)

    def add_image(img_val: Any, title: Any = None, source_url: Any = None) -> None:
        if isinstance(img_val, list):
            for item in img_val:
                add_image(item, title=title, source_url=source_url)
            return
        if isinstance(img_val, dict):
            u = str(img_val.get("url") or img_val.get("image_url") or "").strip()
            t = str(img_val.get("title") or img_val.get("caption") or title or "").strip()
            s = str(img_val.get("source_url") or source_url or "").strip()
            add_image(u, title=t, source_url=s)
            return
        u = _clean_trailing_url_punct(str(img_val or "").strip())
        if not u or not u.startswith(("http://", "https://")) or u in seen_images:
            return
        seen_images.add(u)
        images.append({
            "url": u,
            "title": str(title or "").strip() or "Image",
            "source_url": str(source_url or "").strip(),
        })

    def scan(node: Any, parent_url: str | None = None) -> None:
        if isinstance(node, dict):
            node_type = str(node.get("type") or "")
            if node_type == "url_citation":
                c_url = node.get("url")
                c_title = node.get("title")
                if c_url:
                    add_source(title=c_title, url=c_url)
                    if _is_image_url(c_url) or (c_title and any(ext in str(c_title).lower() for ext in IMAGE_EXTENSIONS)):
                        add_image(c_url, title=c_title, source_url=c_url)
            elif node_type in ("web_search_call", "web_search"):
                action = node.get("action")
                if isinstance(action, dict):
                    if action.get("type") == "open_page" and action.get("url"):
                        p_url = action.get("url")
                        add_source(title=p_url, url=p_url)
                        if _is_image_url(p_url):
                            add_image(p_url, title=p_url, source_url=p_url)
                    if isinstance(action.get("sources"), list):
                        for s in action["sources"]:
                            if isinstance(s, dict):
                                s_url = s.get("url") or s.get("link")
                                add_source(
                                    title=s.get("title"),
                                    url=s_url,
                                    snippet=s.get("snippet") or s.get("text") or s.get("content"),
                                    source=s.get("source") or s.get("site"),
                                    published_at=s.get("published_at") or s.get("date"),
                                    img=s.get("image") or s.get("images"),
                                )
            if "sources" in node and isinstance(node["sources"], list):
                for s in node["sources"]:
                    if isinstance(s, dict) and (s.get("url") or s.get("link")):
                        add_source(
                            title=s.get("title"),
                            url=s.get("url") or s.get("link"),
                            snippet=s.get("snippet") or s.get("text"),
                            source=s.get("source"),
                            published_at=s.get("published_at"),
                            img=s.get("image") or s.get("images"),
                        )
            for img_key in ("image", "images", "image_url", "thumbnail", "img_url"):
                if img_key in node and node[img_key]:
                    add_image(node[img_key], title=node.get("title"), source_url=node.get("url") or parent_url)
            current_url = node.get("url") or node.get("link") or parent_url
            for v in node.values():
                scan(v, parent_url=str(current_url) if current_url else None)
        elif isinstance(node, list):
            for item in node:
                scan(item, parent_url=parent_url)

    scan(raw_response)
    return sources, images


def _extract_markdown_images(text: str) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(url: str, title: str = "", source_url: str = "") -> None:
        url = _clean_trailing_url_punct(url.strip())
        if not url or not url.startswith(("http://", "https://")) or url in seen:
            return
        seen.add(url)
        images.append({"url": url, "title": title.strip() or "Image", "source_url": source_url.strip()})

    for match in re.finditer(r"!\[([^\]]*)\]\((https?://[^\s\)]+)\)", text):
        caption, url = match.groups()
        add(url, title=caption)

    for match in re.finditer(r"(?<!\!)\[([^\]]+)\]\((https?://[^\s\)]+)\)", text):
        caption, url = match.groups()
        if _is_image_url(url):
            add(url, title=caption)

    for match in re.finditer(r"(https?://[^\s\"'<>]+)", text):
        raw_url = match.group(1)
        if _is_image_url(raw_url):
            add(raw_url)

    return images


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


def _normalize_text_batch(primary: str, additional: tuple[str, ...], *, max_items: int) -> tuple[str, ...]:
    items: list[str] = []
    seen: set[str] = set()
    for value in (primary, *additional):
        text = str(value).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        items.append(text)
        if len(items) >= max_items:
            break
    return tuple(items)


def _merge_batch_results(
    operation: WebToolOperation,
    batch_request: WebSearchRequest | ReadUrlContextRequest,
    requests: tuple[WebSearchRequest | ReadUrlContextRequest, ...],
    outputs: list[WebToolResult | BaseException],
) -> WebToolResult:
    items: list[dict[str, Any]] = []
    merged_results: list[Any] = []
    merged_images: list[dict[str, Any]] = []
    image_urls: set[str] = set()
    merged_key_points: list[str] = []
    merged_citations: list[dict[str, Any]] = []
    citation_urls: set[str] = set()
    first_success: WebToolResult | None = None
    success_count = 0

    for request, output in zip(requests, outputs, strict=True):
        target = _request_target(operation, request)
        if isinstance(output, WebToolResult):
            success_count += 1
            if first_success is None:
                first_success = output
            images = output.data.get("images") if isinstance(output.data.get("images"), list) else []
            item = {
                "target": target,
                "success": True,
                "summary": output.data.get("summary"),
                "results": output.data.get("results") if isinstance(output.data.get("results"), list) else [],
                "images": images,
                "key_points": output.data.get("key_points") if isinstance(output.data.get("key_points"), list) else [],
                "citations": output.data.get("citations") if isinstance(output.data.get("citations"), list) else [],
                "provider": _result_provider(output),
            }
            items.append(item)
            if isinstance(item["results"], list):
                merged_results.extend(item["results"])
            if isinstance(images, list):
                for img in images:
                    if isinstance(img, dict):
                        url = str(img.get("url") or "").strip()
                        if url and url not in image_urls:
                            image_urls.add(url)
                            merged_images.append(img)
            if isinstance(item["key_points"], list):
                merged_key_points.extend(str(value) for value in item["key_points"] if str(value).strip())
            if isinstance(item["citations"], list):
                for citation in item["citations"]:
                    if not isinstance(citation, dict):
                        continue
                    url = citation.get("url")
                    if not isinstance(url, str) or not url or url in citation_urls:
                        continue
                    citation_urls.add(url)
                    merged_citations.append(citation)
        else:
            items.append(
                {
                    "target": target,
                    "success": False,
                    "error_type": output.__class__.__name__,
                    "error_message": str(output),
                }
            )

    if first_success is None:
        first_error = next((item.get("error_message") for item in items if item.get("error_message")), "all batch items failed")
        raise RuntimeError(f"Web tool {operation} batch failed: {first_error}")

    data = {
        "summary": f"{operation} completed {success_count}/{len(requests)} batch items.",
        "results": merged_results,
        "images": merged_images,
        "key_points": merged_key_points,
        "citations": merged_citations,
        "items": items,
        "batch": {
            "operation": operation,
            "requested_count": len(requests),
            "success_count": success_count,
            "failure_count": len(requests) - success_count,
            "targets": [_request_target(operation, request) for request in requests],
        },
    }
    return WebToolResult(
        operation=operation,
        content=_render_html_document(operation, batch_request, data),
        data=data,
        provider_id=first_success.provider_id,
        binding_id=first_success.binding_id,
        model_id=first_success.model_id,
        model_alias=first_success.model_alias,
        api_format=first_success.api_format,
        candidate_index=first_success.candidate_index,
        attempt_count=sum(output.attempt_count for output in outputs if isinstance(output, WebToolResult)),
        api_key_index=first_success.api_key_index,
    )


def _result_provider(result: WebToolResult) -> dict[str, Any]:
    return {
        "provider_id": result.provider_id,
        "binding_id": result.binding_id,
        "model_id": result.model_id,
        "model_alias": result.model_alias,
        "api_format": result.api_format,
        "candidate_index": result.candidate_index,
        "attempt_count": result.attempt_count,
        "api_key_index": result.api_key_index,
    }


def _request_target(operation: WebToolOperation, request: WebSearchRequest | ReadUrlContextRequest) -> str:
    if operation == "web_search" and isinstance(request, WebSearchRequest):
        return request.query
    if operation == "read_url_context" and isinstance(request, ReadUrlContextRequest):
        return request.url
    return ""


def _normalize_image_url(url: str) -> str:
    url = url.strip()
    if "commons.wikimedia.org/wiki/File:" in url:
        file_name = url.split("commons.wikimedia.org/wiki/File:")[-1].split("?")[0].split("#")[0]
        return f"https://commons.wikimedia.org/wiki/Special:FilePath/{file_name}"
    if "/wiki/File:" in url and "wikipedia.org" in url:
        domain_part = url.split("/wiki/File:")[0]
        file_name = url.split("/wiki/File:")[-1].split("?")[0].split("#")[0]
        return f"{domain_part}/wiki/Special:FilePath/{file_name}"
    return url


def _render_html_document(
    operation: WebToolOperation,
    request: WebSearchRequest | ReadUrlContextRequest,
    data: dict[str, Any],
) -> str:
    title = "Web Search Result" if operation == "web_search" else "URL Context Result"
    target_label = "Query" if operation == "web_search" else "URL"
    results = data.get("results") if isinstance(data.get("results"), list) else []
    citations = data.get("citations") if isinstance(data.get("citations"), list) else []
    reference_sources = _collect_reference_sources(results, citations)
    reference_markers = _render_reference_markers(reference_sources)

    items = data.get("items") if isinstance(data.get("items"), list) else []

    if items:
        batch = data.get("batch") if isinstance(data.get("batch"), dict) else {}
        success_count = batch.get("success_count", sum(1 for it in items if isinstance(it, dict) and it.get("success") is True))
        total_count = batch.get("requested_count", len(items))
        unit = "queries" if operation == "web_search" else "URLs"

        sections = [
            "<article>",
            "<header>",
            f"<h1>{html.escape(title)}</h1>",
            f'<div style="font-size:12px;color:#64748b;margin-top:4px;">Completed {success_count}/{total_count} {unit}</div>',
            "</header>",
        ]

        rendered_image_urls: set[str] = set()
        for idx, item in enumerate(items, 1):
            if not isinstance(item, dict):
                continue
            item_target = str(item.get("target") or f"Item {idx}")
            item_success = item.get("success") is True
            item_sections = [
                "<section>",
                f"<h2>{idx}. {html.escape(item_target)}</h2>",
            ]
            if item_success:
                item_summary = str(item.get("summary") or "").strip()
                if item_summary:
                    item_sections.append(f'<p style="white-space: pre-wrap;">{html.escape(item_summary)}</p>')
                item_key_points = item.get("key_points") if isinstance(item.get("key_points"), list) else []
                if item_key_points:
                    item_sections.append(_render_text_list(item_key_points))
                item_images = item.get("images") if isinstance(item.get("images"), list) else []
                if item_images:
                    for img in item_images:
                        if isinstance(img, dict) and img.get("url"):
                            rendered_image_urls.add(str(img["url"]).strip())
                    rendered_imgs = _render_images(item_images)
                    if rendered_imgs:
                        item_sections.append(rendered_imgs)
            else:
                error_msg = str(item.get("error_message") or "Failed.")
                item_sections.append(f'<p style="color:#ef4444;">Failed: {html.escape(error_msg)}</p>')
            item_sections.append("</section>")
            sections.extend(item_sections)

        remaining_images = [
            img for img in (data.get("images") or [])
            if isinstance(img, dict) and str(img.get("url") or "").strip() not in rendered_image_urls
        ]
        if remaining_images:
            rem_rendered = _render_images(remaining_images)
            if rem_rendered:
                sections.extend(["<section>", "<h2>Additional Images</h2>", rem_rendered, "</section>"])

        if reference_sources:
            sections.extend(["<footer>", "<h2>Sources</h2>", _render_sources(reference_sources), "</footer>"])
        sections.append("</article>")
        return "\n".join(sections)

    # Single-target mode (default)
    targets = _document_targets(operation, request, data)
    summary = str(data.get("summary") or "").strip()
    key_points = data.get("key_points") if isinstance(data.get("key_points"), list) else []
    images = data.get("images") if isinstance(data.get("images"), list) else []

    sections = [
        "<article>",
        "<header>",
        f"<h1>{html.escape(title)}</h1>",
        "<dl>",
        "<div>",
        f"<dt>{html.escape(target_label)}</dt>",
        "<dd>" + _render_target_list(targets) + "</dd>",
        "</div>",
        "</dl>",
        "</header>",
    ]
    if summary:
        sections.extend(
            [
                "<section>",
                "<h2>Summary</h2>",
                f'<p style="white-space: pre-wrap;">{html.escape(summary)}{reference_markers}</p>',
                "</section>",
            ]
        )
    if images:
        rendered_images = _render_images(images)
        if rendered_images:
            sections.extend(
                [
                    "<section>",
                    "<h2>Images</h2>",
                    rendered_images,
                    "</section>",
                ]
            )
    if key_points:
        sections.extend(
            [
                "<section>",
                "<h2>Key Points</h2>",
                _render_text_list(key_points, reference_sources),
                "</section>",
            ]
        )
    if reference_sources:
        sections.extend(["<footer>", "<h2>Sources</h2>", _render_sources(reference_sources), "</footer>"])
    sections.append("</article>")
    return "\n".join(sections)


def _render_images(images: list[Any]) -> str:
    rendered: list[str] = []
    for item in images:
        if not isinstance(item, dict):
            continue
        raw_url = _clean_string(item.get("url"))
        if not raw_url or not raw_url.startswith(("http://", "https://")):
            continue
        url = _normalize_image_url(raw_url)
        title = _clean_string(item.get("title")) or "Image"
        source_url = _clean_string(item.get("source_url"))
        caption_link = (
            f'<a href="{html.escape(source_url, quote=True)}" target="_blank" rel="noreferrer">{html.escape(title)}</a>'
            if source_url
            else html.escape(title)
        )
        rendered.append(
            f'<figure style="margin:8px 0;display:inline-block;max-width:100%;">'
            f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noreferrer">'
            f'<img src="{html.escape(url, quote=True)}" alt="{html.escape(title, quote=True)}" referrerpolicy="no-referrer" style="max-width:100%;max-height:300px;border-radius:8px;object-fit:cover;" loading="lazy" />'
            f'</a>'
            f'<figcaption style="font-size:12px;color:#64748b;margin-top:4px;">{caption_link}</figcaption>'
            f'</figure>'
        )
    if not rendered:
        return ""
    return '<div style="display:flex;flex-wrap:wrap;gap:12px;margin-top:8px;">' + "".join(rendered) + "</div>"


def _document_targets(operation: WebToolOperation, request: WebSearchRequest | ReadUrlContextRequest, data: dict[str, Any]) -> tuple[str, ...]:
    batch = data.get("batch") if isinstance(data.get("batch"), dict) else None
    if batch and isinstance(batch.get("targets"), list):
        return tuple(str(item) for item in batch["targets"] if str(item).strip())
    if operation == "web_search" and isinstance(request, WebSearchRequest):
        return _normalize_text_batch(request.query, request.additional_queries, max_items=WEB_SEARCH_MAX_BATCH_QUERIES)
    if operation == "read_url_context" and isinstance(request, ReadUrlContextRequest):
        return _normalize_text_batch(request.url, request.additional_urls, max_items=READ_URL_CONTEXT_MAX_BATCH_URLS)
    return tuple()


def _render_target_list(targets: tuple[str, ...]) -> str:
    if not targets:
        return ""
    if len(targets) == 1:
        return html.escape(targets[0])
    return "<ol>" + "".join(f"<li>{html.escape(target)}</li>" for target in targets) + "</ol>"


def _render_text_list(values: list[Any], reference_sources: list[dict[str, str]] | None = None) -> str:
    reference_markers = _render_reference_markers(reference_sources or [])
    return "<ol>" + "".join(f"<li>{html.escape(str(value))}{reference_markers}</li>" for value in values if str(value).strip()) + "</ol>"


def _render_batch_items(items: list[Any]) -> str:
    rendered: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        target = html.escape(str(item.get("target") or ""))
        if item.get("success") is True:
            summary = html.escape(str(item.get("summary") or ""))
            rendered.append(f"<li><h3>{target}</h3><p>{summary}</p></li>")
        else:
            error = html.escape(str(item.get("error_message") or "Failed."))
            rendered.append(f"<li><h3>{target}</h3><p>Failed: {error}</p></li>")
    return "<ol>" + "".join(rendered) + "</ol>"


def _collect_reference_sources(results: list[Any], citations: list[Any]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    seen: dict[str, dict[str, str]] = {}

    def add(item: Any) -> None:
        if not isinstance(item, dict):
            return
        url = _clean_string(item.get("url"))
        title = _clean_string(item.get("title")) or _clean_string(item.get("source")) or url or "Untitled source"
        snippet = _clean_string(item.get("snippet"))
        source = _clean_string(item.get("source"))
        published_at = _clean_string(item.get("published_at"))
        key = url or f"{title}|{snippet}|{source}"
        if not key:
            return
        existing = seen.get(key)
        if existing is not None:
            if not existing.get("title") and title:
                existing["title"] = title
            if not existing.get("snippet") and snippet:
                existing["snippet"] = snippet
            if not existing.get("source") and source:
                existing["source"] = source
            if not existing.get("published_at") and published_at:
                existing["published_at"] = published_at
            return
        source_ref = {
            "index": str(len(sources) + 1),
            "title": title,
            "url": url,
            "snippet": snippet,
            "source": source,
            "published_at": published_at,
        }
        sources.append(source_ref)
        seen[key] = source_ref

    for item in results:
        add(item)
    for item in citations:
        add(item)
    return sources


def _clean_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _render_reference_markers(reference_sources: list[dict[str, str]], *, limit: int = 6) -> str:
    if not reference_sources:
        return ""
    rendered: list[str] = []
    for source in reference_sources[:limit]:
        index = html.escape(source["index"])
        title = html.escape(source.get("title") or f"Source {index}", quote=True)
        url = source.get("url") or ""
        label = f"[{index}]"
        if url:
            rendered.append(f'<a href="{html.escape(url, quote=True)}" title="{title}" target="_blank" rel="noreferrer">{label}</a>')
        else:
            rendered.append(f"<span title=\"{title}\">{label}</span>")
    if len(reference_sources) > limit:
        rendered.append(f"<span>+{len(reference_sources) - limit}</span>")
    return '<sup aria-label="Sources">' + "".join(rendered) + "</sup>"


def _render_sources(reference_sources: list[dict[str, str]]) -> str:
    rendered: list[str] = []
    for source_ref in reference_sources:
        index = source_ref["index"]
        title = source_ref.get("title") or source_ref.get("url") or "Untitled source"
        url = source_ref.get("url") or ""
        snippet = source_ref.get("snippet") or ""
        source = source_ref.get("source") or ""
        published_at = source_ref.get("published_at") or ""
        link = (
            f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noreferrer">{html.escape(title)}</a>'
            if url
            else html.escape(title)
        )
        meta = " · ".join(part for part in (source, published_at) if part)
        rendered.append(
            "<li>"
            f"<h3>{html.escape(index)}. {link}</h3>"
            + (f"<p>{html.escape(snippet)}</p>" if snippet else "")
            + (f"<p><small>{html.escape(meta)}</small></p>" if meta else "")
            + "</li>"
        )
    return "<ol>" + "".join(rendered) + "</ol>"

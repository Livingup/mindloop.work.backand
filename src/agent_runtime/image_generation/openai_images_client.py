from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from agent_runtime.llm_model_registry.llm_model_registry_service import ResolvedLlmProviderModel


_DEFAULT_OPENAI_IMAGES_TIMEOUT_SECONDS = 300.0
_DEFAULT_OPENAI_IMAGES_CONNECT_TIMEOUT_SECONDS = 30.0


class OpenAIImagesGenerationError(RuntimeError):
    pass


class OpenAIImagesGenerationClient:
    async def generate(
        self,
        *,
        resolved_model: ResolvedLlmProviderModel,
        api_key: str | None,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        request_body = dict(body)
        request_body["model"] = resolved_model.model_alias
        request_body["stream"] = False
        url = _join_url(resolved_model.api_base_url, "images/generations")
        headers = _json_headers(resolved_model.custom_headers)
        if api_key:
            headers.setdefault("Authorization", f"Bearer {api_key}")
        return await _post_json(url, headers, request_body)


async def _post_json(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    timeout = httpx.Timeout(
        _DEFAULT_OPENAI_IMAGES_TIMEOUT_SECONDS,
        connect=_DEFAULT_OPENAI_IMAGES_CONNECT_TIMEOUT_SECONDS,
    )
    started_at = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, content=payload, headers=headers)
        if response.status_code >= 400:
            raise OpenAIImagesGenerationError(
                f"OpenAI Images API returned HTTP {response.status_code}: {response.text}"
            )
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise OpenAIImagesGenerationError(
                f"OpenAI Images API returned invalid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise OpenAIImagesGenerationError("OpenAI Images API response must be a JSON object.")
        return data
    except asyncio.CancelledError:
        raise
    except OpenAIImagesGenerationError:
        raise
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        raise OpenAIImagesGenerationError(
            f"OpenAI Images API request failed after {duration_ms}ms: {exc.__class__.__name__}: {exc}"
        ) from exc


def _json_headers(custom_headers: dict[str, str]) -> dict[str, str]:
    headers = dict(custom_headers)
    _setdefault_header(headers, "Content-Type", "application/json")
    _setdefault_header(headers, "Accept", "application/json")
    _setdefault_header(headers, "User-Agent", "mindloop.ai")
    return headers


def _setdefault_header(headers: dict[str, str], key: str, value: str) -> None:
    if not any(existing.lower() == key.lower() for existing in headers):
        headers[key] = value


def _join_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")

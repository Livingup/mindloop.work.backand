# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import json
import logging
import urllib.parse
from dataclasses import replace
from typing import Any

from agent_runtime.llm_http_trace.config import LlmHttpTraceConfig
from agent_runtime.llm_http_trace.models import (
    LLM_HTTP_TRACE_STATUS_COMPLETED,
    LLM_HTTP_TRACE_STATUS_FAILED,
    LlmHttpTraceContext,
    LlmHttpTraceRecord,
    build_started_record,
    utc_now,
)
from agent_runtime.llm_http_trace.repository import LlmHttpTraceRepository

logger = logging.getLogger(__name__)

_SENSITIVE_HEADERS = {
    "authorization",
    "x-api-key",
    "x-goog-api-key",
    "api-key",
    "anthropic-api-key",
    "cookie",
    "set-cookie",
}
_SENSITIVE_QUERY_KEYS = {"key", "api_key", "apikey", "access_token", "token"}


class LlmHttpTraceService:
    def __init__(self, repository: LlmHttpTraceRepository, config: LlmHttpTraceConfig) -> None:
        self._repository = repository
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def max_body_bytes(self) -> int:
        return self._config.max_body_bytes

    def get(self, llm_request_id: str) -> LlmHttpTraceRecord | None:
        return self._repository.get(llm_request_id)

    def record_started(
        self,
        *,
        context: LlmHttpTraceContext,
        method: str,
        url: str,
        request_headers: dict[str, str],
        request_body_bytes: bytes,
        is_stream: bool,
    ) -> LlmHttpTraceRecord | None:
        if not self.enabled:
            return None
        try:
            sanitized_url = sanitize_url(url)
            sanitized_headers = sanitize_headers(request_headers)
            request_body_text, request_body_truncated = decode_and_truncate(request_body_bytes, self.max_body_bytes)
            raw_request_text = build_raw_request_text(
                method=method,
                url=sanitized_url,
                headers=sanitized_headers,
                body_text=request_body_text,
                body_truncated=request_body_truncated,
            )
            record = build_started_record(
                context=context,
                method=method,
                url=sanitized_url,
                request_headers=sanitized_headers,
                request_body_text=request_body_text,
                request_body_bytes=len(request_body_bytes),
                request_body_truncated=request_body_truncated,
                raw_request_text=raw_request_text,
                is_stream=is_stream,
            )
            self._repository.save_started(record)
            return record
        except Exception:
            logger.exception("保存 LLM HTTP 请求开始日志失败: llm_request_id=%s", context.llm_request_id)
            return None

    def record_completed(
        self,
        *,
        started_record: LlmHttpTraceRecord | None,
        context: LlmHttpTraceContext,
        status_code: int,
        response_headers: dict[str, str],
        response_body_bytes: bytes,
        response_content_type: str | None,
        duration_ms: int,
        sse_event_count: int | None = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            base = started_record or self._repository.get(context.llm_request_id)
            if base is None:
                return
            sanitized_headers = sanitize_headers(response_headers)
            response_body_text, response_body_truncated = decode_and_truncate(response_body_bytes, self.max_body_bytes)
            raw_response_text = build_raw_response_text(
                status_code=status_code,
                headers=sanitized_headers,
                body_text=response_body_text,
                body_truncated=response_body_truncated,
            )
            completed = replace(
                base,
                status=LLM_HTTP_TRACE_STATUS_COMPLETED,
                status_code=status_code,
                response_headers=sanitized_headers,
                response_body_text=response_body_text,
                response_body_bytes=len(response_body_bytes),
                response_body_truncated=response_body_truncated,
                response_content_type=response_content_type,
                sse_event_count=sse_event_count,
                completed_at=utc_now(),
                duration_ms=duration_ms,
                raw_response_text=raw_response_text,
            )
            self._repository.save_completed(completed)
        except Exception:
            logger.exception("保存 LLM HTTP 请求完成日志失败: llm_request_id=%s", context.llm_request_id)

    def record_failed(
        self,
        *,
        started_record: LlmHttpTraceRecord | None,
        context: LlmHttpTraceContext,
        error_type: str,
        error_message: str,
        duration_ms: int,
        status_code: int | None = None,
        response_headers: dict[str, str] | None = None,
        response_body_bytes: bytes | None = None,
        response_content_type: str | None = None,
        sse_event_count: int | None = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            base = started_record or self._repository.get(context.llm_request_id)
            if base is None:
                return
            sanitized_headers = sanitize_headers(response_headers or {})
            body = response_body_bytes or b""
            response_body_text, response_body_truncated = decode_and_truncate(body, self.max_body_bytes)
            raw_response_text = None
            if status_code is not None or sanitized_headers or response_body_text:
                raw_response_text = build_raw_response_text(
                    status_code=status_code,
                    headers=sanitized_headers,
                    body_text=response_body_text,
                    body_truncated=response_body_truncated,
                )
            failed = replace(
                base,
                status=LLM_HTTP_TRACE_STATUS_FAILED,
                status_code=status_code,
                response_headers=sanitized_headers,
                response_body_text=response_body_text if body else None,
                response_body_bytes=len(body),
                response_body_truncated=response_body_truncated,
                response_content_type=response_content_type,
                sse_event_count=sse_event_count,
                error_type=error_type,
                error_message=error_message,
                completed_at=utc_now(),
                duration_ms=duration_ms,
                raw_response_text=raw_response_text,
            )
            self._repository.save_failed(failed)
        except Exception:
            logger.exception("保存 LLM HTTP 请求失败日志失败: llm_request_id=%s", context.llm_request_id)


def sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        text_key = str(key)
        if text_key.lower() in _SENSITIVE_HEADERS:
            result[text_key] = mask_secret(str(value))
        else:
            result[text_key] = str(value)
    return result


def sanitize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    sanitized_pairs = [
        (key, "[REDACTED]" if key.lower() in _SENSITIVE_QUERY_KEYS else value)
        for key, value in query_pairs
    ]
    sanitized_query = urllib.parse.urlencode(sanitized_pairs, doseq=True)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, sanitized_query, parsed.fragment))


def mask_secret(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return "[REDACTED]"
    parts = stripped.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() in {"bearer", "basic"}:
        return f"{parts[0]} {mask_secret(parts[1])}"
    if len(stripped) <= 8:
        return "[REDACTED]"
    return f"{stripped[:3]}****{stripped[-4:]}"


def decode_and_truncate(data: bytes, max_bytes: int) -> tuple[str, bool]:
    truncated = len(data) > max_bytes
    sample = data[:max_bytes] if truncated else data
    text = sample.decode("utf-8", errors="replace")
    if truncated:
        text = f"{text}\n...[truncated {len(data) - max_bytes} bytes]"
    return text, truncated


def build_raw_request_text(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body_text: str,
    body_truncated: bool,
) -> str:
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    lines = [f"{method.upper()} {path} HTTP/1.1"]
    if parsed.netloc:
        lines.append(f"Host: {parsed.netloc}")
    lines.extend(f"{key}: {value}" for key, value in headers.items())
    lines.append("")
    lines.append(body_text)
    if body_truncated:
        lines.append("")
    return "\n".join(lines)


def build_raw_response_text(
    *,
    status_code: int | None,
    headers: dict[str, str],
    body_text: str,
    body_truncated: bool,
) -> str:
    status = status_code if status_code is not None else "unknown"
    lines = [f"HTTP/1.1 {status}"]
    lines.extend(f"{key}: {value}" for key, value in headers.items())
    lines.append("")
    lines.append(body_text)
    if body_truncated:
        lines.append("")
    return "\n".join(lines)


def record_to_dict(record: LlmHttpTraceRecord) -> dict[str, Any]:
    return {
        "llm_request_id": record.llm_request_id,
        "llm_call_id": record.llm_call_id,
        "conversation_id": record.conversation_id,
        "task_id": record.task_id,
        "instance_id": record.instance_id,
        "trace_id": record.trace_id,
        "provider_id": record.provider_id,
        "binding_id": record.binding_id,
        "model_id": record.model_id,
        "model_alias": record.model_alias,
        "api_format": record.api_format,
        "candidate_index": record.candidate_index,
        "api_key_index": record.api_key_index,
        "retry_index": record.retry_index,
        "attempt_count": record.attempt_count,
        "status": record.status,
        "method": record.method,
        "url": record.url,
        "request_headers": copy.deepcopy(record.request_headers),
        "request_body_text": record.request_body_text,
        "request_body_bytes": record.request_body_bytes,
        "request_body_truncated": record.request_body_truncated,
        "status_code": record.status_code,
        "response_headers": copy.deepcopy(record.response_headers),
        "response_body_text": record.response_body_text,
        "response_body_bytes": record.response_body_bytes,
        "response_body_truncated": record.response_body_truncated,
        "response_content_type": record.response_content_type,
        "is_stream": record.is_stream,
        "sse_event_count": record.sse_event_count,
        "error_type": record.error_type,
        "error_message": record.error_message,
        "started_at": record.started_at.isoformat(),
        "completed_at": record.completed_at.isoformat() if record.completed_at is not None else None,
        "duration_ms": record.duration_ms,
        "raw_request_text": record.raw_request_text,
        "raw_response_text": record.raw_response_text,
        "truncated": record.truncated,
        "request_body_json": parse_json_or_none(record.request_body_text),
        "response_body_json": parse_json_or_none(record.response_body_text),
    }


def parse_json_or_none(value: str | None) -> Any | None:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None

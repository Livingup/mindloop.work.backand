# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

LLM_HTTP_TRACE_STATUS_STARTED = "started"
LLM_HTTP_TRACE_STATUS_COMPLETED = "completed"
LLM_HTTP_TRACE_STATUS_FAILED = "failed"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class LlmHttpTraceContext:
    llm_request_id: str
    llm_call_id: str
    conversation_id: str | None = None
    task_id: str | None = None
    instance_id: str | None = None
    trace_id: str | None = None
    provider_id: str | None = None
    binding_id: str | None = None
    model_id: str | None = None
    model_alias: str | None = None
    api_format: str | None = None
    candidate_index: int | None = None
    api_key_index: int | None = None
    retry_index: int | None = None
    attempt_count: int | None = None


@dataclass(frozen=True, slots=True)
class LlmHttpTraceRecord:
    llm_request_id: str
    llm_call_id: str
    status: str
    method: str
    url: str
    request_headers: dict[str, str]
    request_body_text: str
    request_body_bytes: int
    request_body_truncated: bool
    started_at: datetime
    conversation_id: str | None = None
    task_id: str | None = None
    instance_id: str | None = None
    trace_id: str | None = None
    provider_id: str | None = None
    binding_id: str | None = None
    model_id: str | None = None
    model_alias: str | None = None
    api_format: str | None = None
    candidate_index: int | None = None
    api_key_index: int | None = None
    retry_index: int | None = None
    attempt_count: int | None = None
    status_code: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body_text: str | None = None
    response_body_bytes: int = 0
    response_body_truncated: bool = False
    response_content_type: str | None = None
    is_stream: bool = False
    sse_event_count: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    raw_request_text: str | None = None
    raw_response_text: str | None = None

    @property
    def truncated(self) -> bool:
        return self.request_body_truncated or self.response_body_truncated


def build_started_record(
    *,
    context: LlmHttpTraceContext,
    method: str,
    url: str,
    request_headers: dict[str, str],
    request_body_text: str,
    request_body_bytes: int,
    request_body_truncated: bool,
    raw_request_text: str,
    is_stream: bool,
) -> LlmHttpTraceRecord:
    return LlmHttpTraceRecord(
        llm_request_id=context.llm_request_id,
        llm_call_id=context.llm_call_id,
        conversation_id=context.conversation_id,
        task_id=context.task_id,
        instance_id=context.instance_id,
        trace_id=context.trace_id,
        provider_id=context.provider_id,
        binding_id=context.binding_id,
        model_id=context.model_id,
        model_alias=context.model_alias,
        api_format=context.api_format,
        candidate_index=context.candidate_index,
        api_key_index=context.api_key_index,
        retry_index=context.retry_index,
        attempt_count=context.attempt_count,
        status=LLM_HTTP_TRACE_STATUS_STARTED,
        method=method,
        url=url,
        request_headers=request_headers,
        request_body_text=request_body_text,
        request_body_bytes=request_body_bytes,
        request_body_truncated=request_body_truncated,
        started_at=utc_now(),
        raw_request_text=raw_request_text,
        is_stream=is_stream,
    )

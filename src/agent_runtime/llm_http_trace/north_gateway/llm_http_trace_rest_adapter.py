# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_runtime.llm_http_trace.service import LlmHttpTraceService, record_to_dict


class LlmHttpTraceResponse(BaseModel):
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
    status: str
    method: str
    url: str
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body_text: str
    request_body_bytes: int
    request_body_truncated: bool
    status_code: int | None = None
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body_text: str | None = None
    response_body_bytes: int
    response_body_truncated: bool
    response_content_type: str | None = None
    is_stream: bool
    sse_event_count: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    started_at: str
    completed_at: str | None = None
    duration_ms: int | None = None
    raw_request_text: str | None = None
    raw_response_text: str | None = None
    truncated: bool
    request_body_json: Any | None = None
    response_body_json: Any | None = None


def create_llm_http_trace_router(service: LlmHttpTraceService) -> APIRouter:
    router = APIRouter()

    @router.get("/{llm_request_id}", response_model=LlmHttpTraceResponse)
    def get_llm_http_trace(llm_request_id: str) -> dict[str, Any]:
        record = service.get(llm_request_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"LLM HTTP 请求日志不存在: {llm_request_id}")
        return record_to_dict(record)

    return router

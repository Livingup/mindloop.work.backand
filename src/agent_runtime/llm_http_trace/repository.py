# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Protocol

from agent_runtime.llm_http_trace.models import LlmHttpTraceRecord


class LlmHttpTraceRepository(Protocol):
    def save_started(self, record: LlmHttpTraceRecord) -> None:
        ...

    def save_completed(self, record: LlmHttpTraceRecord) -> None:
        ...

    def save_failed(self, record: LlmHttpTraceRecord) -> None:
        ...

    def get(self, llm_request_id: str) -> LlmHttpTraceRecord | None:
        ...

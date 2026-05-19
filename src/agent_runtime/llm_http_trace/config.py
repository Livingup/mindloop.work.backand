# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LlmHttpTraceConfig:
    enabled: bool = True
    data_dir: str = "~/.spread-ai/llm-http-traces"
    max_body_bytes: int = 20 * 1024 * 1024

    def __post_init__(self) -> None:
        if not isinstance(self.data_dir, str) or not self.data_dir.strip():
            raise ValueError("llm_http_trace.data_dir 不能为空。")
        if self.max_body_bytes <= 0:
            raise ValueError("llm_http_trace.max_body_bytes 必须大于 0。")

# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LlmHttpTraceConfig:
    enabled: bool = True
    data_dir: str = "~/.spread-ai/llm-http-traces"

    def __post_init__(self) -> None:
        if not isinstance(self.data_dir, str) or not self.data_dir.strip():
            raise ValueError("llm_http_trace.data_dir 不能为空。")

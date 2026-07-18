from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextCompressionConfig:
    enabled: bool = True
    default_threshold_ratio: float = 0.75
    max_worker_rounds: int = 128
    # 约定大于配置：上下文压缩始终使用原生多模态历史输入；字段仅作诊断兼容。
    input_mode: str = "native_multimodal_history"
    verifier_enabled: bool = False
    archive_data_dir: str = "~/.spread-ai/context-compression"

    def __post_init__(self) -> None:
        if not 0 < self.default_threshold_ratio <= 1:
            raise ValueError("default_threshold_ratio 必须在 0 到 1 之间。")
        if self.max_worker_rounds < 1:
            raise ValueError("max_worker_rounds 必须大于 0。")
        if self.input_mode != "native_multimodal_history":
            raise ValueError("input_mode 必须是 native_multimodal_history。")

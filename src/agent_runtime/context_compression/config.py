from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextCompressionConfig:
    enabled: bool = True
    default_threshold_ratio: float = 0.75
    max_worker_rounds: int = 128
    input_mode: str = "transcript_snapshot"
    verifier_enabled: bool = False
    archive_data_dir: str = "~/.spread-ai/context-compression"

    def __post_init__(self) -> None:
        if not 0 < self.default_threshold_ratio <= 1:
            raise ValueError("default_threshold_ratio 必须在 0 到 1 之间。")
        if self.max_worker_rounds < 1:
            raise ValueError("max_worker_rounds 必须大于 0。")
        if self.input_mode not in {"transcript_snapshot", "native_multimodal_history"}:
            raise ValueError("input_mode 必须是 transcript_snapshot 或 native_multimodal_history。")

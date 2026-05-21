from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextCompressionConfig:
    enabled: bool = True
    agent_definition_id: str = "context_compression"
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
        if not self.agent_definition_id.strip():
            raise ValueError("agent_definition_id 不能为空。")
        if self.input_mode not in {"transcript_snapshot", "historical_conversation"}:
            raise ValueError("input_mode 必须是 transcript_snapshot 或 historical_conversation。")

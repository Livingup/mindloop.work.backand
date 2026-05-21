from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agent_runtime.conversation_message.message.models import Conversation


SECTION_WORKERS: tuple[tuple[str, str], ...] = (
    ("RequirementConstraintWorker", "requirements_constraints"),
    ("ProgressPlanWorker", "progress_plan"),
    ("CodeArchitectureWorker", "code_architecture"),
    ("ObservationEvidenceWorker", "observations_evidence"),
    ("DecisionRationaleWorker", "decisions_rationale"),
    ("TodoRiskWorker", "todos_risks"),
)


@dataclass(frozen=True, slots=True)
class CompressionRange:
    compressed_start_index: int
    compressed_end_index: int
    retained_tail_start_index: int
    retained_tail_end_index: int
    retained_tail_anchor: str
    reason: str
    compression_index: int

    @property
    def compressed_message_count(self) -> int:
        return max(0, self.compressed_end_index - self.compressed_start_index + 1)

    @property
    def retained_tail_message_count(self) -> int:
        return max(0, self.retained_tail_end_index - self.retained_tail_start_index + 1)

    def compressed_range_payload(self) -> dict[str, int]:
        return {
            "start_index": self.compressed_start_index,
            "end_index": self.compressed_end_index,
        }

    def retained_tail_range_payload(self) -> dict[str, Any]:
        return {
            "start_index": self.retained_tail_start_index,
            "end_index": self.retained_tail_end_index,
            "anchor": self.retained_tail_anchor,
        }


@dataclass(frozen=True, slots=True)
class CompressionDecision:
    should_compress: bool
    reason: str
    threshold: int | None = None
    used_tokens: int | None = None


@dataclass(slots=True)
class SectionItem:
    section: str
    content: str
    priority: str = "P1"
    status: str = "active"
    source_message_ranges: list[list[int]] = field(default_factory=list)
    resource_references: list[dict[str, Any]] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    worker_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "content": self.content,
            "priority": self.priority,
            "status": self.status,
            "source_message_ranges": list(self.source_message_ranges),
            "resource_references": list(self.resource_references),
            "entities": list(self.entities),
            "worker_name": self.worker_name,
        }


@dataclass(slots=True)
class CompressionJob:
    job_id: str
    task_id: str
    thread_id: str
    agent_id: str
    agent_instance_id: str
    root_conversation_id: str
    source_conversation_id: str
    compression_index: int
    status: str = "created"
    input_mode: str = "transcript_snapshot"
    target_conversation_id: str | None = None
    summary_message_id: str | None = None
    range: CompressionRange | None = None
    section_items: list[SectionItem] = field(default_factory=list)
    completed_sections: set[str] = field(default_factory=set)
    issues: list[dict[str, Any]] = field(default_factory=list)
    final_markdown_chunks: list[str] = field(default_factory=list)
    verifier_result: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None

    @property
    def final_markdown(self) -> str:
        return "\n".join(chunk for chunk in self.final_markdown_chunks if chunk).strip()


@dataclass(frozen=True, slots=True)
class CompressionResult:
    created: bool
    target_conversation: Conversation | None = None
    job: CompressionJob | None = None
    reason: str | None = None

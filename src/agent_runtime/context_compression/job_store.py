from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from agent_runtime.context_compression.models import CompressionJob, SectionItem


TERMINAL_COMPRESSION_JOB_STATUSES = frozenset({"completed", "failed", "cancelled", "skipped"})


class CompressionJobClosed(RuntimeError):
    pass


class InMemoryCompressionJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, CompressionJob] = {}
        self._lock = RLock()

    def create_job(self, job: CompressionJob) -> CompressionJob:
        with self._lock:
            self._jobs[job.job_id] = job
            return job

    def get_job(self, job_id: str) -> CompressionJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"上下文压缩 job 不存在: {job_id}")
            return job

    def update_status(self, job_id: str, status: str) -> CompressionJob:
        with self._lock:
            job = self.get_job(job_id)
            if job.status in TERMINAL_COMPRESSION_JOB_STATUSES and status != job.status:
                if status in TERMINAL_COMPRESSION_JOB_STATUSES:
                    return job
                raise CompressionJobClosed(f"上下文压缩 job 已结束，拒绝状态回退: job_id={job.job_id}, status={job.status}, next_status={status}")
            job.status = status
            if status in TERMINAL_COMPRESSION_JOB_STATUSES and job.completed_at is None:
                job.completed_at = datetime.now(timezone.utc)
            return job

    def append_section_items(
        self,
        job_id: str,
        *,
        worker_name: str,
        section: str,
        expected_section: str | None,
        items: list[dict[str, Any]],
    ) -> int:
        with self._lock:
            job = self.get_job(job_id)
            _ensure_job_writable(job)
            if expected_section is not None and section != expected_section:
                raise ValueError(f"Worker 写入了非负责章节: worker={worker_name}, expected={expected_section}, actual={section}")
            for item in items:
                content = str(item.get("content") or "").strip()
                if not content:
                    continue
                job.section_items.append(
                    SectionItem(
                        section=section,
                        content=content,
                        priority=str(item.get("priority") or "P1"),
                        status=str(item.get("status") or "active"),
                        source_message_ranges=_list_of_ranges(item.get("source_message_ranges")),
                        resource_references=_list_of_dicts(item.get("resource_references")),
                        entities=[str(value) for value in item.get("entities") or []],
                        worker_name=worker_name,
                    )
                )
            return len(job.section_items)

    def mark_section_complete(self, job_id: str, section: str, *, expected_section: str | None) -> int:
        with self._lock:
            job = self.get_job(job_id)
            _ensure_job_writable(job)
            if expected_section is not None and section != expected_section:
                raise ValueError(f"Worker 完成了非负责章节: expected={expected_section}, actual={section}")
            job.completed_sections.add(section)
            return len(job.completed_sections)

    def append_final_markdown(self, job_id: str, markdown: str) -> int:
        with self._lock:
            job = self.get_job(job_id)
            _ensure_job_writable(job)
            if markdown.strip():
                job.final_markdown_chunks.append(markdown.strip())
            return len(job.final_markdown_chunks)

    def report_issue(self, job_id: str, issue: dict[str, Any]) -> int:
        with self._lock:
            job = self.get_job(job_id)
            _ensure_job_writable(job)
            job.issues.append(dict(issue))
            return len(job.issues)

    def set_target_conversation(
        self,
        job_id: str,
        *,
        target_conversation_id: str,
        summary_message_id: str,
    ) -> CompressionJob:
        with self._lock:
            job = self.get_job(job_id)
            job.target_conversation_id = target_conversation_id
            job.summary_message_id = summary_message_id
            return job


def _ensure_job_writable(job: CompressionJob) -> None:
    if job.status in TERMINAL_COMPRESSION_JOB_STATUSES:
        raise CompressionJobClosed(f"上下文压缩 job 已结束，拒绝继续写入: job_id={job.job_id}, status={job.status}")


def _list_of_ranges(value: object) -> list[list[int]]:
    result: list[list[int]] = []
    if not isinstance(value, list):
        return result
    for item in value:
        if isinstance(item, list | tuple) and len(item) == 2:
            try:
                result.append([int(item[0]), int(item[1])])
            except (TypeError, ValueError):
                pass
    return result


def _list_of_dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any

from agent_runtime.context_compression.models import CompressionJob
from agent_runtime.conversation_message.infrastructure.sqlite_conversation_repository import _message_to_json_line
from agent_runtime.conversation_message.message.models import Conversation


_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_compression_jobs (
    job_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL,
    root_conversation_id TEXT NOT NULL,
    source_conversation_id TEXT NOT NULL,
    target_conversation_id TEXT,
    compression_index INTEGER NOT NULL,
    status TEXT NOT NULL,
    compressed_start_index INTEGER,
    compressed_end_index INTEGER,
    retained_tail_start_index INTEGER,
    retained_tail_end_index INTEGER,
    retained_tail_anchor TEXT,
    input_mode TEXT NOT NULL,
    final_summary_path TEXT,
    before_messages_snapshot_path TEXT,
    after_messages_snapshot_path TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cc_jobs_task_instance_root_index ON context_compression_jobs(task_id, agent_instance_id, root_conversation_id, compression_index);
CREATE INDEX IF NOT EXISTS idx_cc_jobs_root_index ON context_compression_jobs(root_conversation_id, compression_index);
CREATE INDEX IF NOT EXISTS idx_cc_jobs_source ON context_compression_jobs(source_conversation_id);
CREATE INDEX IF NOT EXISTS idx_cc_jobs_target ON context_compression_jobs(target_conversation_id);
"""


class ContextCompressionArchiveRepository:
    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir).expanduser()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._snapshots_dir = self._data_dir / "snapshots"
        self._summaries_dir = self._data_dir / "summaries"
        self._snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._summaries_dir.mkdir(parents=True, exist_ok=True)
        # 压缩流程可由后台执行线程触发；同一长生命周期仓储连接允许跨线程复用，
        # 但 job 元数据与归档文件写入必须在仓储锁内串行化。
        self._lock = RLock()
        self._conn = sqlite3.connect(
            str(self._data_dir / "context_compression.db"),
            timeout=30,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def next_compression_index(self, *, task_id: str, agent_instance_id: str, root_conversation_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(compression_index) FROM context_compression_jobs "
                "WHERE task_id = ? AND agent_instance_id = ? AND root_conversation_id = ? AND status = 'completed'",
                (task_id, agent_instance_id, root_conversation_id),
            ).fetchone()
            value = row[0] if row else None
            return int(value or 0) + 1

    def root_conversation_id_for(self, *, agent_instance_id: str, source_conversation_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT root_conversation_id FROM context_compression_jobs "
                "WHERE agent_instance_id = ? AND target_conversation_id = ? AND status = 'completed' "
                "ORDER BY compression_index DESC LIMIT 1",
                (agent_instance_id, source_conversation_id),
            ).fetchone()
            return str(row[0]) if row else source_conversation_id

    def get_section_items(self, job_id: str) -> list[dict[str, Any]]:
        """读取上下文压缩 job 归档中的 Section Worker 提取条目。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT metadata_json FROM context_compression_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"上下文压缩 job 不存在: {job_id}")
        try:
            metadata = json.loads(row[0] or "{}")
        except (TypeError, ValueError):
            return []
        section_items = metadata.get("section_items") if isinstance(metadata, dict) else None
        if not isinstance(section_items, list):
            return []
        return [dict(item) for item in section_items if isinstance(item, dict)]

    def upsert_job(
        self,
        job: CompressionJob,
        *,
        source_conversation: Conversation | None = None,
        target_conversation: Conversation | None = None,
    ) -> None:
        with self._lock:
            final_summary_path = self._write_final_summary(job) if job.final_markdown else None
            before_path = self._write_snapshot(job.job_id, "before", source_conversation) if source_conversation is not None else None
            after_path = self._write_snapshot(job.job_id, "after", target_conversation) if target_conversation is not None else None
            range_value = job.range
            metadata = {
                "summary_message_id": job.summary_message_id,
                "section_items": [item.to_dict() for item in job.section_items],
                "completed_sections": sorted(job.completed_sections),
                "issues": list(job.issues),
                "verifier_result": job.verifier_result,
            }
            self._conn.execute(
                "INSERT OR REPLACE INTO context_compression_jobs ("
                "job_id, task_id, thread_id, agent_id, agent_instance_id, root_conversation_id, "
                "source_conversation_id, target_conversation_id, compression_index, status, "
                "compressed_start_index, compressed_end_index, retained_tail_start_index, retained_tail_end_index, "
                "retained_tail_anchor, input_mode, final_summary_path, before_messages_snapshot_path, "
                "after_messages_snapshot_path, metadata_json, created_at, completed_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.job_id,
                    job.task_id,
                    job.thread_id,
                    job.agent_id,
                    job.agent_instance_id,
                    job.root_conversation_id,
                    job.source_conversation_id,
                    job.target_conversation_id,
                    job.compression_index,
                    job.status,
                    range_value.compressed_start_index if range_value else None,
                    range_value.compressed_end_index if range_value else None,
                    range_value.retained_tail_start_index if range_value else None,
                    range_value.retained_tail_end_index if range_value else None,
                    range_value.retained_tail_anchor if range_value else None,
                    job.input_mode,
                    final_summary_path,
                    before_path,
                    after_path,
                    json.dumps(metadata, ensure_ascii=False),
                    job.created_at.isoformat(),
                    job.completed_at.isoformat() if job.completed_at else None,
                ),
            )
            self._conn.commit()

    def _write_final_summary(self, job: CompressionJob) -> str:
        path = self._summaries_dir / f"{job.job_id}.md"
        path.write_text(job.final_markdown, encoding="utf-8")
        return str(path)

    def _write_snapshot(self, job_id: str, suffix: str, conversation: Conversation | None) -> str | None:
        if conversation is None:
            return None
        path = self._snapshots_dir / f"{job_id}-{suffix}-{conversation.id}.jsonl"
        with open(path, "w", encoding="utf-8") as file:
            for message in conversation.messages:
                file.write(_message_to_json_line(message) + "\n")
        return str(path)

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Sequence

from agent_runtime.context_compression.history_markdown import (
    load_messages_from_jsonl_text,
    messages_to_jsonl_text,
    native_history_to_markdown,
    QUALITY_ANALYSIS_PROMPT_SUFFIX,
)
from agent_runtime.context_compression.models import CompressionJob
from agent_runtime.conversation_message.message.models import UniMessage
from agent_runtime.infrastructure.sqlite_maintenance import coordinated_sqlite_repository


_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_compression_jobs (
    job_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL,
    root_conversation_id TEXT NOT NULL,
    source_conversation_id TEXT NOT NULL,
    target_conversation_id TEXT,
    compression_index INTEGER NOT NULL,
    status TEXT NOT NULL,
    native_history_path TEXT,
    final_markdown_path TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cc_jobs_task_instance_root_index
    ON context_compression_jobs(task_id, agent_instance_id, root_conversation_id, compression_index);
CREATE INDEX IF NOT EXISTS idx_cc_jobs_root_index
    ON context_compression_jobs(root_conversation_id, compression_index);
CREATE INDEX IF NOT EXISTS idx_cc_jobs_source
    ON context_compression_jobs(source_conversation_id);
CREATE INDEX IF NOT EXISTS idx_cc_jobs_target
    ON context_compression_jobs(target_conversation_id);
"""


@dataclass(frozen=True, slots=True)
class ContextCompressionQualityArchive:
    """质量分析用归档三件套：job_id + native_history + final_markdown。"""

    job_id: str
    status: str
    native_history: tuple[UniMessage, ...]
    final_markdown: str
    native_history_markdown: str
    quality_analysis_text: str


@coordinated_sqlite_repository(
    lambda repository: repository._data_dir / "context_compression.db",
 )
class ContextCompressionArchiveRepository:
    """上下文压缩归档仓储。

    质量分析权威内容仅保留：
    1. job_id
    2. 该次压缩实际使用的 native_history（compressed range）
    3. 最终完整压缩 Markdown

    额外仅保留运行必需的索引字段（task/instance/root/source/target/compression_index/status/time），
    不归档 section 过程明细、issues、worker 过程、before/after 全会话快照。
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir).expanduser()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._histories_dir = self._data_dir / "histories"
        self._summaries_dir = self._data_dir / "summaries"
        self._histories_dir.mkdir(parents=True, exist_ok=True)
        self._summaries_dir.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._conn = sqlite3.connect(
            str(self._data_dir / "context_compression.db"),
            timeout=30,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._ensure_schema()
        self._columns = {
            str(row[1]) for row in self._conn.execute("PRAGMA table_info(context_compression_jobs)").fetchall()
        }
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

    def upsert_job(
        self,
        job: CompressionJob,
        *,
        native_history_messages: Sequence[UniMessage] | None = None,
    ) -> None:
        """写入/更新 job 归档。

        - native_history_messages：该次压缩实际使用的 compressed-range 原始消息；提供时覆盖写入。
        - final_markdown：job.final_markdown 非空时写入 summaries。
        不写入过程型 section/issues/worker/before-after 全会话快照。
        """
        with self._lock:
            native_history_path = self._existing_path(job.job_id, "native_history_path")
            if native_history_messages is not None:
                native_history_path = self._write_native_history(job.job_id, native_history_messages)
            final_markdown_path = self._existing_path(job.job_id, "final_markdown_path")
            if job.final_markdown:
                final_markdown_path = self._write_final_markdown(job.job_id, job.final_markdown)
            if "metadata_json" in self._columns or "input_mode" in self._columns:
                self._upsert_job_legacy_compatible(
                    job,
                    native_history_path=native_history_path,
                    final_markdown_path=final_markdown_path,
                )
            else:
                self._conn.execute(
                    "INSERT INTO context_compression_jobs ("
                    "job_id, task_id, agent_instance_id, root_conversation_id, source_conversation_id, "
                    "target_conversation_id, compression_index, status, native_history_path, "
                    "final_markdown_path, created_at, completed_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(job_id) DO UPDATE SET "
                    "task_id=excluded.task_id, "
                    "agent_instance_id=excluded.agent_instance_id, "
                    "root_conversation_id=excluded.root_conversation_id, "
                    "source_conversation_id=excluded.source_conversation_id, "
                    "target_conversation_id=excluded.target_conversation_id, "
                    "compression_index=excluded.compression_index, "
                    "status=excluded.status, "
                    "native_history_path=COALESCE(excluded.native_history_path, context_compression_jobs.native_history_path), "
                    "final_markdown_path=COALESCE(excluded.final_markdown_path, context_compression_jobs.final_markdown_path), "
                    "created_at=excluded.created_at, "
                    "completed_at=excluded.completed_at",
                    (
                        job.job_id,
                        job.task_id,
                        job.agent_instance_id,
                        job.root_conversation_id,
                        job.source_conversation_id,
                        job.target_conversation_id,
                        job.compression_index,
                        job.status,
                        native_history_path,
                        final_markdown_path,
                        job.created_at.isoformat(),
                        job.completed_at.isoformat() if job.completed_at else None,
                    ),
                )
            self._conn.commit()

    def get_quality_archive(self, job_id: str) -> ContextCompressionQualityArchive:
        """按 job_id 读取质量分析三件套；仅 completed 且字段齐全时成功。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id, status, native_history_path, final_markdown_path "
                "FROM context_compression_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"上下文压缩 job 不存在: {job_id}")
        status = str(row[1] or "")
        native_history_path = row[2]
        final_markdown_path = row[3]
        if status != "completed":
            raise ValueError(f"上下文压缩 job 尚未完成，无法导出质量分析文本: job_id={job_id}, status={status}")
        if not native_history_path:
            raise ValueError(f"上下文压缩 job 缺少 native_history 归档: job_id={job_id}")
        if not final_markdown_path:
            raise ValueError(f"上下文压缩 job 缺少最终 Markdown 归档: job_id={job_id}")
        history_path = Path(str(native_history_path))
        summary_path = Path(str(final_markdown_path))
        if not history_path.is_file():
            raise FileNotFoundError(f"native_history 文件不存在: {history_path}")
        if not summary_path.is_file():
            raise FileNotFoundError(f"最终 Markdown 文件不存在: {summary_path}")
        messages = load_messages_from_jsonl_text(history_path.read_text(encoding="utf-8"))
        final_markdown = summary_path.read_text(encoding="utf-8").strip()
        if not final_markdown:
            raise ValueError(f"最终 Markdown 为空: job_id={job_id}")
        history_markdown = native_history_to_markdown(messages)
        quality_text = build_quality_analysis_text(
            native_history_markdown=history_markdown,
            final_markdown=final_markdown,
        )
        return ContextCompressionQualityArchive(
            job_id=str(row[0]),
            status=status,
            native_history=tuple(messages),
            final_markdown=final_markdown,
            native_history_markdown=history_markdown,
            quality_analysis_text=quality_text,
        )

    def _upsert_job_legacy_compatible(
        self,
        job: CompressionJob,
        *,
        native_history_path: str | None,
        final_markdown_path: str | None,
    ) -> None:
        """旧库含 metadata_json/input_mode 等过程列时：只填运行必需字段，过程字段置空。"""
        range_value = job.range
        self._conn.execute(
            "INSERT INTO context_compression_jobs ("
            "job_id, task_id, thread_id, agent_id, agent_instance_id, root_conversation_id, "
            "source_conversation_id, target_conversation_id, compression_index, status, "
            "compressed_start_index, compressed_end_index, retained_tail_start_index, retained_tail_end_index, "
            "retained_tail_anchor, input_mode, final_summary_path, before_messages_snapshot_path, "
            "after_messages_snapshot_path, metadata_json, created_at, completed_at, "
            "native_history_path, final_markdown_path"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET "
            "task_id=excluded.task_id, "
            "thread_id=excluded.thread_id, "
            "agent_id=excluded.agent_id, "
            "agent_instance_id=excluded.agent_instance_id, "
            "root_conversation_id=excluded.root_conversation_id, "
            "source_conversation_id=excluded.source_conversation_id, "
            "target_conversation_id=excluded.target_conversation_id, "
            "compression_index=excluded.compression_index, "
            "status=excluded.status, "
            "compressed_start_index=excluded.compressed_start_index, "
            "compressed_end_index=excluded.compressed_end_index, "
            "retained_tail_start_index=excluded.retained_tail_start_index, "
            "retained_tail_end_index=excluded.retained_tail_end_index, "
            "retained_tail_anchor=excluded.retained_tail_anchor, "
            "input_mode=excluded.input_mode, "
            "final_summary_path=COALESCE(excluded.final_summary_path, context_compression_jobs.final_summary_path), "
            "before_messages_snapshot_path=NULL, "
            "after_messages_snapshot_path=NULL, "
            "metadata_json=excluded.metadata_json, "
            "created_at=excluded.created_at, "
            "completed_at=excluded.completed_at, "
            "native_history_path=COALESCE(excluded.native_history_path, context_compression_jobs.native_history_path), "
            "final_markdown_path=COALESCE(excluded.final_markdown_path, context_compression_jobs.final_markdown_path)",
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
                job.input_mode or "native_multimodal_history",
                final_markdown_path,  # also mirror into final_summary_path for old cleanup code
                None,
                None,
                "{}",  # 不再归档 section/issues 等过程 metadata
                job.created_at.isoformat(),
                job.completed_at.isoformat() if job.completed_at else None,
                native_history_path,
                final_markdown_path,
            ),
        )

    def _write_native_history(self, job_id: str, messages: Sequence[UniMessage]) -> str:
        path = self._histories_dir / f"{job_id}.jsonl"
        path.write_text(messages_to_jsonl_text(messages), encoding="utf-8")
        return str(path)

    def _write_final_markdown(self, job_id: str, markdown: str) -> str:
        path = self._summaries_dir / f"{job_id}.md"
        path.write_text(markdown.strip() + "\n", encoding="utf-8")
        return str(path)

    def _existing_path(self, job_id: str, column: str) -> str | None:
        row = self._conn.execute(
            f"SELECT {column} FROM context_compression_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        text = str(row[0]).strip()
        return text or None

    def _ensure_schema(self) -> None:
        columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(context_compression_jobs)").fetchall()}
        if not columns:
            return
        # 兼容旧表：补齐精简归档所需列；旧过程列可保留但不再写入权威内容。
        if "native_history_path" not in columns:
            self._conn.execute("ALTER TABLE context_compression_jobs ADD COLUMN native_history_path TEXT")
        if "final_markdown_path" not in columns:
            self._conn.execute("ALTER TABLE context_compression_jobs ADD COLUMN final_markdown_path TEXT")
        # 旧库可能没有精简 schema 的列组合；若缺核心运行列则无法使用旧行，仅保证新写入路径可用。
        for column, ddl in (
            ("task_id", "ALTER TABLE context_compression_jobs ADD COLUMN task_id TEXT"),
            ("agent_instance_id", "ALTER TABLE context_compression_jobs ADD COLUMN agent_instance_id TEXT"),
            ("root_conversation_id", "ALTER TABLE context_compression_jobs ADD COLUMN root_conversation_id TEXT"),
            ("source_conversation_id", "ALTER TABLE context_compression_jobs ADD COLUMN source_conversation_id TEXT"),
            ("target_conversation_id", "ALTER TABLE context_compression_jobs ADD COLUMN target_conversation_id TEXT"),
            ("compression_index", "ALTER TABLE context_compression_jobs ADD COLUMN compression_index INTEGER"),
            ("status", "ALTER TABLE context_compression_jobs ADD COLUMN status TEXT"),
            ("created_at", "ALTER TABLE context_compression_jobs ADD COLUMN created_at TEXT"),
            ("completed_at", "ALTER TABLE context_compression_jobs ADD COLUMN completed_at TEXT"),
        ):
            if column not in columns:
                self._conn.execute(ddl)


def build_quality_analysis_text(*, native_history_markdown: str, final_markdown: str) -> str:
    history = (native_history_markdown or "").strip() or "（native_history 为空）"
    final = (final_markdown or "").strip() or "（最终压缩 Markdown 为空）"
    return (
        "## 下面是目标业务agent的历史对话：\n"
        f"{history}\n\n"
        "## 下面是我的上下文压缩系统整理压缩后的完整上下文内容：\n"
        f"{final}\n\n"
        f"{QUALITY_ANALYSIS_PROMPT_SUFFIX}\n"
    )

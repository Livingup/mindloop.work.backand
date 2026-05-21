from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_compression_worker_prompts (
    input_mode TEXT NOT NULL,
    worker_name TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    default_prompt_text TEXT NOT NULL,
    is_custom INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (input_mode, worker_name)
);
"""


@dataclass(frozen=True, slots=True)
class ContextCompressionWorkerPrompt:
    input_mode: str
    worker_name: str
    prompt_text: str
    default_prompt_text: str
    is_custom: bool
    revision: int
    created_at: str
    updated_at: str


class ContextCompressionWorkerPromptRepository:
    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor

    def upsert_default(
        self,
        *,
        input_mode: str,
        worker_name: str,
        default_prompt_text: str,
    ) -> ContextCompressionWorkerPrompt:
        """同步代码内置默认值到 SQLite。

        - DB 中无记录：插入 prompt_text = default_text，is_custom=0，revision=1。
        - DB 中已有记录但 default_prompt_text 变化：刷新存储的默认文本；
          若当前未自定义，则同步更新 prompt_text 到新默认值，并自增 revision。
        """
        current = self.get_prompt(input_mode=input_mode, worker_name=worker_name)
        now = _utc_now_text()
        if current is None:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO context_compression_worker_prompts ("
                    "input_mode, worker_name, prompt_text, default_prompt_text,"
                    " is_custom, revision, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, 0, 1, ?, ?)",
                    (input_mode, worker_name, default_prompt_text, default_prompt_text, now, now),
                )
                conn.commit()
            finally:
                conn.close()
            return self.get_required_prompt(input_mode=input_mode, worker_name=worker_name)
        if current.default_prompt_text != default_prompt_text:
            prompt_text = current.prompt_text if current.is_custom else default_prompt_text
            new_revision = current.revision if current.is_custom else current.revision + 1
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE context_compression_worker_prompts "
                    "SET prompt_text = ?, default_prompt_text = ?, revision = ?, updated_at = ? "
                    "WHERE input_mode = ? AND worker_name = ?",
                    (prompt_text, default_prompt_text, new_revision, now, input_mode, worker_name),
                )
                conn.commit()
            finally:
                conn.close()
            return self.get_required_prompt(input_mode=input_mode, worker_name=worker_name)
        return current

    def list_prompts(self) -> list[ContextCompressionWorkerPrompt]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT input_mode, worker_name, prompt_text, default_prompt_text,"
                " is_custom, revision, created_at, updated_at "
                "FROM context_compression_worker_prompts ORDER BY input_mode, worker_name"
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_prompt(row) for row in rows]

    def get_prompt(
        self,
        *,
        input_mode: str,
        worker_name: str,
    ) -> ContextCompressionWorkerPrompt | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT input_mode, worker_name, prompt_text, default_prompt_text,"
                " is_custom, revision, created_at, updated_at "
                "FROM context_compression_worker_prompts WHERE input_mode = ? AND worker_name = ?",
                (input_mode, worker_name),
            ).fetchone()
        finally:
            conn.close()
        return _row_to_prompt(row) if row is not None else None

    def get_required_prompt(
        self,
        *,
        input_mode: str,
        worker_name: str,
    ) -> ContextCompressionWorkerPrompt:
        prompt = self.get_prompt(input_mode=input_mode, worker_name=worker_name)
        if prompt is None:
            raise KeyError(f"上下文压缩 Worker prompt 不存在: {input_mode}/{worker_name}")
        return prompt

    def update_prompt(
        self,
        *,
        input_mode: str,
        worker_name: str,
        prompt_text: str,
    ) -> ContextCompressionWorkerPrompt:
        """保存用户 override，is_custom=1，revision 自增。"""
        if not prompt_text.strip():
            raise ValueError("prompt_text 不能为空。")
        conn = self._connect()
        try:
            result = conn.execute(
                "UPDATE context_compression_worker_prompts "
                "SET prompt_text = ?, is_custom = 1, revision = revision + 1, updated_at = ? "
                "WHERE input_mode = ? AND worker_name = ?",
                (prompt_text, _utc_now_text(), input_mode, worker_name),
            )
            conn.commit()
        finally:
            conn.close()
        if result.rowcount <= 0:
            raise KeyError(f"上下文压缩 Worker prompt 不存在: {input_mode}/{worker_name}")
        return self.get_required_prompt(input_mode=input_mode, worker_name=worker_name)

    def reset_prompt(
        self,
        *,
        input_mode: str,
        worker_name: str,
    ) -> ContextCompressionWorkerPrompt:
        """恢复默认 prompt：prompt_text 还原为 default，is_custom=0，revision 自增。"""
        conn = self._connect()
        try:
            result = conn.execute(
                "UPDATE context_compression_worker_prompts "
                "SET prompt_text = default_prompt_text, is_custom = 0,"
                " revision = revision + 1, updated_at = ? "
                "WHERE input_mode = ? AND worker_name = ?",
                (_utc_now_text(), input_mode, worker_name),
            )
            conn.commit()
        finally:
            conn.close()
        if result.rowcount <= 0:
            raise KeyError(f"上下文压缩 Worker prompt 不存在: {input_mode}/{worker_name}")
        return self.get_required_prompt(input_mode=input_mode, worker_name=worker_name)

    def _connect(self) -> sqlite3.Connection:
        data_dir = Path(self._data_dir_accessor()).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(data_dir / "context_compression_worker_prompts.db"))
        conn.execute(_SCHEMA)
        _ensure_revision_column(conn)
        _migrate_legacy_input_modes(conn)
        conn.commit()
        return conn


def _ensure_revision_column(conn: sqlite3.Connection) -> None:
    """对早期未带 revision 字段的旧库做轻量迁移：补列并初始化为 1。"""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(context_compression_worker_prompts)").fetchall()}
    if "revision" not in columns:
        conn.execute(
            "ALTER TABLE context_compression_worker_prompts "
            "ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
        )


def _migrate_legacy_input_modes(conn: sqlite3.Connection) -> None:
    """把旧标识 historical_conversation 的行重命名为 native_multimodal_history。

    若新名 worker_name 行已存在则保留新行，删除旧行，避免主键冲突。
    """
    legacy_rows = conn.execute(
        "SELECT worker_name FROM context_compression_worker_prompts "
        "WHERE input_mode = 'historical_conversation'"
    ).fetchall()
    if not legacy_rows:
        return
    for (worker_name,) in legacy_rows:
        existing = conn.execute(
            "SELECT 1 FROM context_compression_worker_prompts "
            "WHERE input_mode = 'native_multimodal_history' AND worker_name = ?",
            (worker_name,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "UPDATE context_compression_worker_prompts "
                "SET input_mode = 'native_multimodal_history' "
                "WHERE input_mode = 'historical_conversation' AND worker_name = ?",
                (worker_name,),
            )
        else:
            conn.execute(
                "DELETE FROM context_compression_worker_prompts "
                "WHERE input_mode = 'historical_conversation' AND worker_name = ?",
                (worker_name,),
            )


def _row_to_prompt(row: tuple[str, str, str, str, int, int, str, str]) -> ContextCompressionWorkerPrompt:
    return ContextCompressionWorkerPrompt(
        input_mode=str(row[0]),
        worker_name=str(row[1]),
        prompt_text=str(row[2]),
        default_prompt_text=str(row[3]),
        is_custom=bool(row[4]),
        revision=int(row[5]),
        created_at=str(row[6]),
        updated_at=str(row[7]),
    )


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_compression_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    llm_policy_group_id TEXT,
    input_mode TEXT,
    updated_at TEXT NOT NULL
);
"""

_SUPPORTED_INPUT_MODES = frozenset({"transcript_snapshot", "native_multimodal_history"})


@dataclass(frozen=True, slots=True)
class ContextCompressionRuntimeSettings:
    """上下文压缩运行态可变设置（产品内置 Agent 视角，但与用户 AgentDefinition 解耦）。"""

    llm_policy_group_id: str | None
    input_mode: str | None
    updated_at: str | None


class ContextCompressionSettingsRepository:
    """独立持久化「上下文压缩」运行态可变设置的 SQLite 仓储。

    与设计文档 15.10 一致：
    - 上下文压缩流水线是产品内置后台能力，不依赖任何用户 AgentDefinition，
      但其唯一可变设置（绑定的 LLM 执行策略组 ID）必须独立持久化、用户可改。
    - 与 ContextCompressionWorkerPromptRepository 同目录、同 db 文件，独立表
      `context_compression_settings`，单行（id=1）。
    """

    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor

    def get_settings(self) -> ContextCompressionRuntimeSettings:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT llm_policy_group_id, input_mode, updated_at "
                "FROM context_compression_settings WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return ContextCompressionRuntimeSettings(
                llm_policy_group_id=None,
                input_mode=None,
                updated_at=None,
            )
        raw_policy_value = row[0]
        raw_input_mode = row[1]
        input_mode = str(raw_input_mode) if raw_input_mode is not None else None
        if input_mode not in _SUPPORTED_INPUT_MODES:
            input_mode = None
        return ContextCompressionRuntimeSettings(
            llm_policy_group_id=str(raw_policy_value) if raw_policy_value is not None else None,
            input_mode=input_mode,
            updated_at=str(row[2]),
        )

    def get_llm_policy_group_id(self) -> str | None:
        return self.get_settings().llm_policy_group_id

    def get_input_mode(self) -> str | None:
        return self.get_settings().input_mode

    def set_settings(
        self,
        *,
        llm_policy_group_id: str | None,
        input_mode: str | None,
    ) -> ContextCompressionRuntimeSettings:
        normalized_policy = _normalize_optional_text(llm_policy_group_id)
        normalized_input_mode = _normalize_input_mode(input_mode)
        now = _utc_now_text()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO context_compression_settings (id, llm_policy_group_id, input_mode, updated_at) "
                "VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                " llm_policy_group_id = excluded.llm_policy_group_id, "
                " input_mode = excluded.input_mode, "
                " updated_at = excluded.updated_at",
                (normalized_policy, normalized_input_mode, now),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_settings()

    def set_llm_policy_group_id(self, value: str | None) -> ContextCompressionRuntimeSettings:
        current = self.get_settings()
        return self.set_settings(
            llm_policy_group_id=value,
            input_mode=current.input_mode,
        )

    def _connect(self) -> sqlite3.Connection:
        data_dir = Path(self._data_dir_accessor()).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(data_dir / "context_compression_worker_prompts.db"))
        conn.execute(_SCHEMA)
        _ensure_settings_schema(conn)
        conn.commit()
        return conn


def _ensure_settings_schema(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(context_compression_settings)").fetchall()}
    if "input_mode" not in columns:
        conn.execute("ALTER TABLE context_compression_settings ADD COLUMN input_mode TEXT")


def _normalize_optional_text(value: str | None) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None


def _normalize_input_mode(value: str | None) -> str | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    if normalized not in _SUPPORTED_INPUT_MODES:
        raise ValueError("input_mode 必须是 transcript_snapshot 或 native_multimodal_history。")
    return normalized


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()

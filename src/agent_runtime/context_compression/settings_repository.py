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
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class ContextCompressionRuntimeSettings:
    """上下文压缩运行态可变设置（产品内置 Agent 视角，但与用户 AgentDefinition 解耦）。"""

    llm_policy_group_id: str | None
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
                "SELECT llm_policy_group_id, updated_at "
                "FROM context_compression_settings WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return ContextCompressionRuntimeSettings(
                llm_policy_group_id=None,
                updated_at=None,
            )
        raw_value = row[0]
        return ContextCompressionRuntimeSettings(
            llm_policy_group_id=str(raw_value) if raw_value is not None else None,
            updated_at=str(row[1]),
        )

    def get_llm_policy_group_id(self) -> str | None:
        return self.get_settings().llm_policy_group_id

    def set_llm_policy_group_id(self, value: str | None) -> ContextCompressionRuntimeSettings:
        normalized: str | None
        if isinstance(value, str):
            stripped = value.strip()
            normalized = stripped if stripped else None
        else:
            normalized = None
        now = _utc_now_text()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO context_compression_settings (id, llm_policy_group_id, updated_at) "
                "VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                " llm_policy_group_id = excluded.llm_policy_group_id, "
                " updated_at = excluded.updated_at",
                (normalized, now),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_settings()

    def _connect(self) -> sqlite3.Connection:
        data_dir = Path(self._data_dir_accessor()).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(data_dir / "context_compression_worker_prompts.db"))
        conn.execute(_SCHEMA)
        conn.commit()
        return conn


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()

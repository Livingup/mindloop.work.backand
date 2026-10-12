from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_compression_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    llm_policy_group_id TEXT,
    input_mode TEXT,
    updated_at TEXT NOT NULL
);
"""

_SUPPORTED_INPUT_MODES = frozenset({"native_multimodal_history"})


@dataclass(frozen=True, slots=True)
class ContextCompressionRuntimeSettings:
    """上下文压缩运行态可变设置（产品内置 Agent 视角，但与用户 AgentDefinition 解耦）。"""

    llm_policy_group_id: str | None
    input_mode: str | None
    active_profile_id: str | None
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
                "SELECT llm_policy_group_id, input_mode, active_profile_id, updated_at "
                "FROM context_compression_settings WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return ContextCompressionRuntimeSettings(
                llm_policy_group_id=None,
                input_mode=None,
                active_profile_id=None,
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
            active_profile_id=_normalize_active_profile_id(row[2] if len(row) > 2 else None),
            updated_at=str(row[3] if len(row) > 3 else row[2]),
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
        active_profile_id: str | None = None,
    ) -> ContextCompressionRuntimeSettings:
        normalized_policy = _normalize_optional_text(llm_policy_group_id)
        normalized_input_mode = _normalize_input_mode(input_mode)
        normalized_profile = _normalize_active_profile_id(active_profile_id)
        now = _utc_now_text()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO context_compression_settings (id, llm_policy_group_id, input_mode, active_profile_id, updated_at) "
                "VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                " llm_policy_group_id = excluded.llm_policy_group_id, "
                " input_mode = excluded.input_mode, "
                " active_profile_id = excluded.active_profile_id, "
                " updated_at = excluded.updated_at",
                (normalized_policy, normalized_input_mode, normalized_profile, now),
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
            active_profile_id=current.active_profile_id,
        )

    def update_settings(
        self,
        *,
        llm_policy_group_id: str | None = None,
    ) -> ContextCompressionRuntimeSettings:
        return self.set_llm_policy_group_id(llm_policy_group_id)

    def get_active_profile_id(self) -> str | None:
        return self.get_settings().active_profile_id

    def _connect(self) -> sqlite3.Connection:
        data_dir = Path(self._data_dir_accessor()).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(data_dir / "context_compression_worker_prompts.db"))
        conn.execute(_SCHEMA)
        _ensure_settings_schema(conn)
        conn.commit()
        return conn


class PostgresContextCompressionSettingsRepository:
    """上下文压缩用户共享运行态设置的云端 PostgreSQL 仓储。

    上下文压缩 LLM 策略组绑定与输入模式属于用户多设备共享级系统配置，
    运行时只写入 cloud_user_config_items，不提供本机 SQLite fallback。
    本机 SQLite 仓储仅作为显式迁移旧数据的 legacy source 使用。
    """

    _NAMESPACE = "context_compression.runtime_settings"
    _ITEM_ID = "default"

    def __init__(self, store_accessor: Callable[[], Any]) -> None:
        self._store_accessor = store_accessor

    def get_settings(self) -> ContextCompressionRuntimeSettings:
        payload = self._store_accessor().get_payload(self._NAMESPACE, self._ITEM_ID)
        if payload is None:
            return ContextCompressionRuntimeSettings(
                llm_policy_group_id=None,
                input_mode=None,
                active_profile_id=None,
                updated_at=None,
            )
        return _settings_from_payload(payload)

    def get_llm_policy_group_id(self) -> str | None:
        return self.get_settings().llm_policy_group_id

    def get_input_mode(self) -> str | None:
        return self.get_settings().input_mode

    def get_active_profile_id(self) -> str | None:
        return self.get_settings().active_profile_id

    def set_settings(
        self,
        *,
        llm_policy_group_id: str | None,
        input_mode: str | None,
        active_profile_id: str | None = None,
    ) -> ContextCompressionRuntimeSettings:
        settings = ContextCompressionRuntimeSettings(
            llm_policy_group_id=_normalize_optional_text(llm_policy_group_id),
            input_mode=_normalize_input_mode(input_mode),
            active_profile_id=_normalize_active_profile_id(active_profile_id),
            updated_at=_utc_now_text(),
        )
        self._store_accessor().save_payload(
            self._NAMESPACE,
            self._ITEM_ID,
            _settings_payload(settings),
        )
        return settings

    def set_llm_policy_group_id(self, value: str | None) -> ContextCompressionRuntimeSettings:
        current = self.get_settings()
        return self.set_settings(
            llm_policy_group_id=value,
            input_mode=current.input_mode,
            active_profile_id=current.active_profile_id,
        )

    def update_settings(
        self,
        *,
        llm_policy_group_id: str | None = None,
    ) -> ContextCompressionRuntimeSettings:
        return self.set_llm_policy_group_id(llm_policy_group_id)

    def save_settings_snapshot(
        self,
        settings: ContextCompressionRuntimeSettings,
    ) -> ContextCompressionRuntimeSettings:
        """保存迁移源中的完整设置快照，保留 legacy updated_at。"""
        normalized_settings = ContextCompressionRuntimeSettings(
            llm_policy_group_id=_normalize_optional_text(settings.llm_policy_group_id),
            input_mode=_normalize_input_mode(settings.input_mode),
            active_profile_id=_normalize_active_profile_id(settings.active_profile_id),
            updated_at=settings.updated_at or _utc_now_text(),
        )
        self._store_accessor().save_payload(
            self._NAMESPACE,
            self._ITEM_ID,
            _settings_payload(normalized_settings),
        )
        return normalized_settings

    def contains_settings(self) -> bool:
        return self._store_accessor().contains_payload(self._NAMESPACE, self._ITEM_ID)

    def db_path(self) -> str:
        return "postgresql:cloud_user_config_items/context_compression.runtime_settings/default"


def create_context_compression_settings_repository(
    data_dir_accessor: Callable[[], str],
):
    """创建上下文压缩共享运行态设置仓储。

    用户多设备共享级系统配置以云端 PostgreSQL 为唯一运行时权威；
    data_dir_accessor 仅保留为与 legacy SQLite 仓储和迁移代码兼容的形参，
    运行时不得因云端不可用而回退到本机 SQLite。
    """
    del data_dir_accessor
    from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore
    from agent_runtime.config import ConfigRegistry

    store = PostgresUserConfigStore(
        lambda: ConfigRegistry.instance().cloud_user_config_database,
    )
    return PostgresContextCompressionSettingsRepository(lambda: store)


def _settings_from_payload(payload: dict[str, Any]) -> ContextCompressionRuntimeSettings:
    return ContextCompressionRuntimeSettings(
        llm_policy_group_id=_normalize_optional_text(payload.get("llm_policy_group_id")),
        input_mode=_normalize_input_mode(payload.get("input_mode") if payload.get("input_mode") is not None else None),
        active_profile_id=_normalize_active_profile_id(payload.get("active_profile_id")),
        updated_at=str(payload["updated_at"]) if payload.get("updated_at") is not None else None,
    )


def _settings_payload(settings: ContextCompressionRuntimeSettings) -> dict[str, Any]:
    return {
        "llm_policy_group_id": settings.llm_policy_group_id,
        "input_mode": settings.input_mode,
        "active_profile_id": settings.active_profile_id,
        "updated_at": settings.updated_at,
    }


def _ensure_settings_schema(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(context_compression_settings)").fetchall()}
    if "input_mode" not in columns:
        conn.execute("ALTER TABLE context_compression_settings ADD COLUMN input_mode TEXT")
    if "active_profile_id" not in columns:
        conn.execute("ALTER TABLE context_compression_settings ADD COLUMN active_profile_id TEXT")


def _normalize_optional_text(value: str | None) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None


def _normalize_input_mode(value: str | None) -> str | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    # 历史数据兼容：旧 transcript_snapshot 统一收敛到 native。
    if normalized == "transcript_snapshot":
        return "native_multimodal_history"
    if normalized not in _SUPPORTED_INPUT_MODES:
        raise ValueError("input_mode 必须是 native_multimodal_history。")
    return normalized


def _normalize_active_profile_id(value: Any) -> str | None:
    if value is None:
        return None
    return _normalize_optional_text(str(value))


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()

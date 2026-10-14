# -*- coding: utf-8 -*-
"""机器本地云端共享配置引导状态与共享配置快照存储。

PostgreSQL 仍然是共享配置写入权威；本模块只保存本机启动判断所需的引导完成状态，
以及 PostgreSQL 已成功拉取过的只读快照，用于后续桌面启动和共享配置读取脱离同步数据库连接。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_user_config_bootstrap_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    object_storage_decision TEXT NOT NULL DEFAULT 'pending',
    completed_at TEXT,
    updated_at TEXT NOT NULL
);
"""

_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_user_config_shared_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    config_key_json TEXT NOT NULL,
    revision INTEGER NOT NULL,
    items_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

OBJECT_STORAGE_DECISION_PENDING = "pending"
OBJECT_STORAGE_DECISION_CONFIGURED = "configured"
OBJECT_STORAGE_DECISION_SKIPPED = "skipped"
_OBJECT_STORAGE_DECISIONS = {
    OBJECT_STORAGE_DECISION_PENDING,
    OBJECT_STORAGE_DECISION_CONFIGURED,
    OBJECT_STORAGE_DECISION_SKIPPED,
}


@dataclass(frozen=True, slots=True)
class CloudUserConfigBootstrapState:
    """本机云端共享配置引导状态。"""

    object_storage_decision: str
    completed_at: str | None
    updated_at: str | None

    @property
    def object_storage_configured_or_skipped(self) -> bool:
        return self.object_storage_decision in {
            OBJECT_STORAGE_DECISION_CONFIGURED,
            OBJECT_STORAGE_DECISION_SKIPPED,
        }


@dataclass(frozen=True, slots=True)
class CloudUserConfigLocalSnapshot:
    """本机共享配置快照元数据与 payload。"""

    config_key: tuple[object, ...]
    revision: int
    items: dict[tuple[str, str], dict[str, Any]]
    updated_at: str


@dataclass(frozen=True, slots=True)
class CloudUserConfigLocalReadiness:
    """本机启动模式判定所需的只读状态。"""

    credentials_saved: bool
    shared_config_snapshot_ready: bool
    object_storage_configured_or_skipped: bool
    completed: bool
    completed_at: str | None
    snapshot_updated_at: str | None
    object_storage_decision: str
    error_message: str | None = None


class CloudUserConfigLocalStateRepository:
    """机器本地 bootstrap 状态与共享配置快照 SQLite 仓储。"""

    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor

    def get_state(self) -> CloudUserConfigBootstrapState:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT object_storage_decision, completed_at, updated_at "
                "FROM cloud_user_config_bootstrap_state WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return CloudUserConfigBootstrapState(
                object_storage_decision=OBJECT_STORAGE_DECISION_PENDING,
                completed_at=None,
                updated_at=None,
            )
        decision = _normalize_object_storage_decision(row[0])
        return CloudUserConfigBootstrapState(
            object_storage_decision=decision,
            completed_at=str(row[1]) if row[1] is not None else None,
            updated_at=str(row[2]) if row[2] is not None else None,
        )

    def mark_object_storage_configured(self) -> CloudUserConfigBootstrapState:
        return self._save_state(object_storage_decision=OBJECT_STORAGE_DECISION_CONFIGURED)

    def mark_object_storage_skipped(self) -> CloudUserConfigBootstrapState:
        return self._save_state(object_storage_decision=OBJECT_STORAGE_DECISION_SKIPPED)

    def reset_object_storage_decision(self) -> CloudUserConfigBootstrapState:
        """重置 OSS 决策和完成标记，用于 PostgreSQL bootstrap 连接重新配置。"""
        return self._save_state(object_storage_decision=OBJECT_STORAGE_DECISION_PENDING)

    def save_snapshot(
        self,
        *,
        config_key: tuple[object, ...],
        revision: int,
        items: dict[tuple[str, str], dict[str, Any]],
    ) -> CloudUserConfigLocalSnapshot:
        now = _utc_now_text()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO cloud_user_config_shared_snapshot ("
                "id, config_key_json, revision, items_json, updated_at"
                ") VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "config_key_json = excluded.config_key_json, "
                "revision = excluded.revision, "
                "items_json = excluded.items_json, "
                "updated_at = excluded.updated_at",
                (
                    _config_key_to_json(config_key),
                    int(revision),
                    _items_to_json(items),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        snapshot = self.get_snapshot(expected_config_key=config_key)
        if snapshot is None:
            raise RuntimeError("本机共享配置快照保存后无法读取。")
        return snapshot

    def get_snapshot(
        self,
        *,
        expected_config_key: tuple[object, ...] | None = None,
    ) -> CloudUserConfigLocalSnapshot | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT config_key_json, revision, items_json, updated_at "
                "FROM cloud_user_config_shared_snapshot WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        config_key = _config_key_from_json(str(row[0] or "[]"))
        if expected_config_key is not None and config_key != expected_config_key:
            return None
        items = _items_from_json(str(row[2] or "[]"))
        return CloudUserConfigLocalSnapshot(
            config_key=config_key,
            revision=int(row[1] or 0),
            items=items,
            updated_at=str(row[3]) if row[3] is not None else "",
        )

    def get_readiness(
        self,
        *,
        credentials_saved: bool,
        expected_config_key: tuple[object, ...] | None,
    ) -> CloudUserConfigLocalReadiness:
        try:
            state = self.get_state()
            snapshot = (
                self.get_snapshot(expected_config_key=expected_config_key)
                if expected_config_key is not None
                else None
            )
        except Exception as exc:  # noqa: BLE001 - 状态接口和启动判断需要降级为未就绪
            return CloudUserConfigLocalReadiness(
                credentials_saved=credentials_saved,
                shared_config_snapshot_ready=False,
                object_storage_configured_or_skipped=False,
                completed=False,
                completed_at=None,
                snapshot_updated_at=None,
                object_storage_decision=OBJECT_STORAGE_DECISION_PENDING,
                error_message=str(exc),
            )
        snapshot_ready = snapshot is not None
        completed = bool(
            credentials_saved
            and snapshot_ready
            and state.object_storage_configured_or_skipped
            and state.completed_at
        )
        return CloudUserConfigLocalReadiness(
            credentials_saved=credentials_saved,
            shared_config_snapshot_ready=snapshot_ready,
            object_storage_configured_or_skipped=state.object_storage_configured_or_skipped,
            completed=completed,
            completed_at=state.completed_at,
            snapshot_updated_at=snapshot.updated_at if snapshot is not None else None,
            object_storage_decision=state.object_storage_decision,
            error_message=None,
        )

    def db_path(self) -> Path:
        return Path(self._data_dir_accessor()).expanduser() / "cloud_user_config_bootstrap.db"

    def _save_state(self, *, object_storage_decision: str) -> CloudUserConfigBootstrapState:
        decision = _normalize_object_storage_decision(object_storage_decision)
        now = _utc_now_text()
        completed_at = now if decision in {
            OBJECT_STORAGE_DECISION_CONFIGURED,
            OBJECT_STORAGE_DECISION_SKIPPED,
        } else None
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO cloud_user_config_bootstrap_state ("
                "id, object_storage_decision, completed_at, updated_at"
                ") VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "object_storage_decision = excluded.object_storage_decision, "
                "completed_at = excluded.completed_at, "
                "updated_at = excluded.updated_at",
                (decision, completed_at, now),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_state()

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute(_STATE_SCHEMA)
        conn.execute(_SNAPSHOT_SCHEMA)
        conn.commit()
        return conn


def _normalize_object_storage_decision(value: object) -> str:
    decision = str(value or "").strip().lower()
    if decision in _OBJECT_STORAGE_DECISIONS:
        return decision
    return OBJECT_STORAGE_DECISION_PENDING


def _config_key_to_json(config_key: tuple[object, ...]) -> str:
    return json.dumps(list(config_key), ensure_ascii=False, separators=(",", ":"))


def _config_key_from_json(value: str) -> tuple[object, ...]:
    data = json.loads(value)
    if not isinstance(data, list):
        raise ValueError("本机共享配置快照 config_key_json 格式错误。")
    return tuple(data)


def _items_to_json(items: dict[tuple[str, str], dict[str, Any]]) -> str:
    serializable = [
        {"namespace": namespace, "item_id": item_id, "payload": payload}
        for (namespace, item_id), payload in sorted(items.items())
    ]
    return json.dumps(serializable, ensure_ascii=False, separators=(",", ":"))


def _items_from_json(value: str) -> dict[tuple[str, str], dict[str, Any]]:
    data = json.loads(value)
    if not isinstance(data, list):
        raise ValueError("本机共享配置快照 items_json 格式错误。")
    items: dict[tuple[str, str], dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("本机共享配置快照条目格式错误。")
        namespace = str(item.get("namespace", ""))
        item_id = str(item.get("item_id", ""))
        payload = item.get("payload")
        if not namespace or not item_id or not isinstance(payload, dict):
            raise ValueError("本机共享配置快照条目缺少 namespace/item_id/payload。")
        items[(namespace, item_id)] = dict(payload)
    return items


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


_BOOTSTRAP_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_user_config_bootstrap_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL,
    backend TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    database_name TEXT NOT NULL,
    username TEXT NOT NULL,
    password TEXT,
    sslmode TEXT NOT NULL,
    pool_min_size INTEGER NOT NULL,
    pool_max_size INTEGER NOT NULL,
    connect_timeout_seconds INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def atomic_commit_database_reconfigure(
    data_dir: str | Path,
    *,
    enabled: bool,
    backend: str,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str | None,
    sslmode: str,
    pool_min_size: int,
    pool_max_size: int,
    connect_timeout_seconds: int,
    config_key: tuple[object, ...],
    revision: int,
    items: dict[tuple[str, str], dict[str, Any]],
    object_storage_decision: str | None = None,
) -> None:
    """在同一 SQLite 事务中原子提交 bootstrap 凭据、共享快照与可选 OSS 决策。

    Settings 重配置必须保证这三项同进同退：任一写入失败则整体 rollback，
    避免出现“凭据已切到新目标但 snapshot/decision 仍是旧 key”的 readiness 失配。
    使用 BEGIN IMMEDIATE 串行化并发写者。
    """

    expected_key = (
        host.strip(),
        int(port),
        database.strip(),
        username.strip(),
        (sslmode or "require").strip() or "require",
    )
    if tuple(config_key) != expected_key:
        raise ValueError("bootstrap credentials 与 shared snapshot config_key 不一致")

    path = Path(data_dir).expanduser() / "cloud_user_config_bootstrap.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = _utc_now_text()
    password_text = (password or "").strip() or None
    decision = (
        _normalize_object_storage_decision(object_storage_decision)
        if object_storage_decision is not None
        else None
    )
    completed_at = (
        now
        if decision in {OBJECT_STORAGE_DECISION_CONFIGURED, OBJECT_STORAGE_DECISION_SKIPPED}
        else None
    )

    conn = sqlite3.connect(str(path))
    try:
        conn.execute(_BOOTSTRAP_SETTINGS_SCHEMA)
        conn.execute(_STATE_SCHEMA)
        conn.execute(_SNAPSHOT_SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        _tx_write_bootstrap_settings(
            conn,
            enabled=enabled,
            backend=backend,
            host=host,
            port=port,
            database=database,
            username=username,
            password_text=password_text,
            sslmode=sslmode,
            pool_min_size=pool_min_size,
            pool_max_size=pool_max_size,
            connect_timeout_seconds=connect_timeout_seconds,
            updated_at=now,
        )
        _tx_write_shared_snapshot(
            conn,
            config_key=config_key,
            revision=revision,
            items=items,
            updated_at=now,
        )
        if decision is not None:
            _tx_write_bootstrap_state(
                conn,
                object_storage_decision=decision,
                completed_at=completed_at,
                updated_at=now,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _tx_write_bootstrap_settings(
    conn: sqlite3.Connection,
    *,
    enabled: bool,
    backend: str,
    host: str,
    port: int,
    database: str,
    username: str,
    password_text: str | None,
    sslmode: str,
    pool_min_size: int,
    pool_max_size: int,
    connect_timeout_seconds: int,
    updated_at: str,
) -> None:
    conn.execute(
        "INSERT INTO cloud_user_config_bootstrap_settings ("
        "id, enabled, backend, host, port, database_name, username, password, sslmode, "
        "pool_min_size, pool_max_size, connect_timeout_seconds, updated_at"
        ") VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "enabled = excluded.enabled, backend = excluded.backend, host = excluded.host, "
        "port = excluded.port, database_name = excluded.database_name, username = excluded.username, "
        "password = excluded.password, sslmode = excluded.sslmode, "
        "pool_min_size = excluded.pool_min_size, pool_max_size = excluded.pool_max_size, "
        "connect_timeout_seconds = excluded.connect_timeout_seconds, updated_at = excluded.updated_at",
        (
            1 if enabled else 0,
            (backend or "postgresql").strip() or "postgresql",
            host.strip(),
            int(port),
            database.strip(),
            username.strip(),
            password_text,
            (sslmode or "require").strip() or "require",
            max(int(pool_min_size), 1),
            max(int(pool_max_size), 1),
            max(int(connect_timeout_seconds), 1),
            updated_at,
        ),
    )


def _tx_write_shared_snapshot(
    conn: sqlite3.Connection,
    *,
    config_key: tuple[object, ...],
    revision: int,
    items: dict[tuple[str, str], dict[str, Any]],
    updated_at: str,
) -> None:
    conn.execute(
        "INSERT INTO cloud_user_config_shared_snapshot ("
        "id, config_key_json, revision, items_json, updated_at"
        ") VALUES (1, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "config_key_json = excluded.config_key_json, "
        "revision = excluded.revision, "
        "items_json = excluded.items_json, "
        "updated_at = excluded.updated_at",
        (
            _config_key_to_json(config_key),
            int(revision),
            _items_to_json(items),
            updated_at,
        ),
    )


def _tx_write_bootstrap_state(
    conn: sqlite3.Connection,
    *,
    object_storage_decision: str,
    completed_at: str | None,
    updated_at: str,
) -> None:
    conn.execute(
        "INSERT INTO cloud_user_config_bootstrap_state ("
        "id, object_storage_decision, completed_at, updated_at"
        ") VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "object_storage_decision = excluded.object_storage_decision, "
        "completed_at = excluded.completed_at, "
        "updated_at = excluded.updated_at",
        (object_storage_decision, completed_at, updated_at),
    )

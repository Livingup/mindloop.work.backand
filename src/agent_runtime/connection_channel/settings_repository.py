from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS connection_channel_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cloud_service_host TEXT,
    cloud_service_port INTEGER,
    remote_access_enabled INTEGER NOT NULL DEFAULT 0,
    tunnel_restart_required INTEGER NOT NULL DEFAULT 0,
    tunnel_controller TEXT,
    owner_id TEXT,
    managed_tunnel_id TEXT,
    managed_config_path TEXT,
    managed_task_name TEXT,
    managed_pid INTEGER,
    controller_owner_marker TEXT,
    applied_config_version TEXT,
    last_started_at TEXT,
    last_stopped_at TEXT,
    last_error_message TEXT,
    updated_at TEXT NOT NULL
);
"""

_COLUMN_MIGRATIONS = {
    "remote_access_enabled": "INTEGER NOT NULL DEFAULT 0",
    "tunnel_restart_required": "INTEGER NOT NULL DEFAULT 0",
    "tunnel_controller": "TEXT",
    "owner_id": "TEXT",
    "managed_tunnel_id": "TEXT",
    "managed_config_path": "TEXT",
    "managed_task_name": "TEXT",
    "managed_pid": "INTEGER",
    "controller_owner_marker": "TEXT",
    "applied_config_version": "TEXT",
    "last_started_at": "TEXT",
    "last_stopped_at": "TEXT",
    "last_error_message": "TEXT",
}


@dataclass(frozen=True, slots=True)
class ConnectionChannelSettings:
    cloud_service_host: str | None
    cloud_service_port: int | None
    remote_access_enabled: bool
    tunnel_restart_required: bool
    tunnel_controller: str | None
    owner_id: str | None
    managed_tunnel_id: str | None
    managed_config_path: str | None
    managed_task_name: str | None
    managed_pid: int | None
    controller_owner_marker: str | None
    applied_config_version: str | None
    last_started_at: str | None
    last_stopped_at: str | None
    last_error_message: str | None
    updated_at: str | None

    @property
    def configured(self) -> bool:
        return bool((self.cloud_service_host or "").strip()) and self.cloud_service_port is not None

    @property
    def effective_config_version(self) -> str | None:
        if not self.configured or self.cloud_service_host is None or self.cloud_service_port is None:
            return None
        return f"{self.cloud_service_host}:{self.cloud_service_port}"


class ConnectionChannelSettingsRepository:
    """Current-machine SQLite repository for local remote access settings and tunnel state."""

    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor

    def get_settings(self) -> ConnectionChannelSettings:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT cloud_service_host, cloud_service_port, remote_access_enabled, "
                "tunnel_restart_required, tunnel_controller, owner_id, managed_tunnel_id, "
                "managed_config_path, managed_task_name, managed_pid, controller_owner_marker, "
                "applied_config_version, last_started_at, last_stopped_at, last_error_message, updated_at "
                "FROM connection_channel_settings WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return _default_settings()
        return ConnectionChannelSettings(
            cloud_service_host=_nullable_text(row[0]),
            cloud_service_port=int(row[1]) if row[1] is not None else None,
            remote_access_enabled=_bool_from_db(row[2]),
            tunnel_restart_required=_bool_from_db(row[3]),
            tunnel_controller=_nullable_text(row[4]),
            owner_id=_nullable_text(row[5]),
            managed_tunnel_id=_nullable_text(row[6]),
            managed_config_path=_nullable_text(row[7]),
            managed_task_name=_nullable_text(row[8]),
            managed_pid=int(row[9]) if row[9] is not None else None,
            controller_owner_marker=_nullable_text(row[10]),
            applied_config_version=_nullable_text(row[11]),
            last_started_at=_nullable_text(row[12]),
            last_stopped_at=_nullable_text(row[13]),
            last_error_message=_nullable_text(row[14]),
            updated_at=str(row[15]) if row[15] is not None else None,
        )

    def save_endpoint(
        self,
        *,
        cloud_service_host: str | None,
        cloud_service_port: int | None,
    ) -> ConnectionChannelSettings:
        existing = self.get_settings()
        now = _utc_now_text()
        endpoint_changed = (
            _nullable_text(cloud_service_host) != existing.cloud_service_host
            or cloud_service_port != existing.cloud_service_port
        )
        restart_required = (
            existing.tunnel_restart_required
            or (existing.remote_access_enabled and existing.configured and endpoint_changed)
        )
        self._upsert(
            cloud_service_host=_nullable_text(cloud_service_host),
            cloud_service_port=cloud_service_port,
            remote_access_enabled=existing.remote_access_enabled,
            tunnel_restart_required=restart_required,
            tunnel_controller=existing.tunnel_controller,
            owner_id=existing.owner_id,
            managed_tunnel_id=existing.managed_tunnel_id,
            managed_config_path=existing.managed_config_path,
            managed_task_name=existing.managed_task_name,
            managed_pid=existing.managed_pid,
            controller_owner_marker=existing.controller_owner_marker,
            applied_config_version=existing.applied_config_version,
            last_started_at=existing.last_started_at,
            last_stopped_at=existing.last_stopped_at,
            last_error_message=existing.last_error_message,
            updated_at=now,
        )
        return self.get_settings()

    def set_remote_access_enabled(self, enabled: bool) -> ConnectionChannelSettings:
        existing = self.get_settings()
        now = _utc_now_text()
        self._upsert(
            cloud_service_host=existing.cloud_service_host,
            cloud_service_port=existing.cloud_service_port,
            remote_access_enabled=enabled,
            tunnel_restart_required=False,
            tunnel_controller=existing.tunnel_controller,
            owner_id=existing.owner_id,
            managed_tunnel_id=existing.managed_tunnel_id,
            managed_config_path=existing.managed_config_path,
            managed_task_name=existing.managed_task_name,
            managed_pid=existing.managed_pid,
            controller_owner_marker=existing.controller_owner_marker,
            applied_config_version=existing.effective_config_version if enabled else existing.applied_config_version,
            last_started_at=existing.last_started_at,
            last_stopped_at=now if not enabled else existing.last_stopped_at,
            last_error_message=existing.last_error_message,
            updated_at=now,
        )
        return self.get_settings()

    def record_controller_state(
        self,
        *,
        tunnel_controller: str | None,
        owner_id: str | None = None,
        managed_tunnel_id: str | None = None,
        managed_config_path: str | None = None,
        managed_task_name: str | None = None,
        managed_pid: int | None = None,
        controller_owner_marker: str | None = None,
        applied_config_version: str | None = None,
        last_error_message: str | None = None,
    ) -> ConnectionChannelSettings:
        existing = self.get_settings()
        now = _utc_now_text()
        self._upsert(
            cloud_service_host=existing.cloud_service_host,
            cloud_service_port=existing.cloud_service_port,
            remote_access_enabled=existing.remote_access_enabled,
            tunnel_restart_required=existing.tunnel_restart_required,
            tunnel_controller=tunnel_controller,
            owner_id=owner_id,
            managed_tunnel_id=managed_tunnel_id,
            managed_config_path=managed_config_path,
            managed_task_name=managed_task_name,
            managed_pid=managed_pid,
            controller_owner_marker=controller_owner_marker,
            applied_config_version=applied_config_version,
            last_started_at=existing.last_started_at,
            last_stopped_at=existing.last_stopped_at,
            last_error_message=last_error_message,
            updated_at=now,
        )
        return self.get_settings()

    def db_path(self) -> Path:
        return Path(self._data_dir_accessor()).expanduser() / "connection_channel_settings.db"

    def _upsert(
        self,
        *,
        cloud_service_host: str | None,
        cloud_service_port: int | None,
        remote_access_enabled: bool,
        tunnel_restart_required: bool,
        tunnel_controller: str | None,
        owner_id: str | None,
        managed_tunnel_id: str | None,
        managed_config_path: str | None,
        managed_task_name: str | None,
        managed_pid: int | None,
        controller_owner_marker: str | None,
        applied_config_version: str | None,
        last_started_at: str | None,
        last_stopped_at: str | None,
        last_error_message: str | None,
        updated_at: str,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO connection_channel_settings ("
                "id, cloud_service_host, cloud_service_port, remote_access_enabled, "
                "tunnel_restart_required, tunnel_controller, owner_id, managed_tunnel_id, "
                "managed_config_path, managed_task_name, managed_pid, controller_owner_marker, "
                "applied_config_version, last_started_at, last_stopped_at, last_error_message, updated_at"
                ") VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                " cloud_service_host = excluded.cloud_service_host, "
                " cloud_service_port = excluded.cloud_service_port, "
                " remote_access_enabled = excluded.remote_access_enabled, "
                " tunnel_restart_required = excluded.tunnel_restart_required, "
                " tunnel_controller = excluded.tunnel_controller, "
                " owner_id = excluded.owner_id, "
                " managed_tunnel_id = excluded.managed_tunnel_id, "
                " managed_config_path = excluded.managed_config_path, "
                " managed_task_name = excluded.managed_task_name, "
                " managed_pid = excluded.managed_pid, "
                " controller_owner_marker = excluded.controller_owner_marker, "
                " applied_config_version = excluded.applied_config_version, "
                " last_started_at = excluded.last_started_at, "
                " last_stopped_at = excluded.last_stopped_at, "
                " last_error_message = excluded.last_error_message, "
                " updated_at = excluded.updated_at",
                (
                    _nullable_text(cloud_service_host),
                    cloud_service_port,
                    _sqlite_bool(remote_access_enabled),
                    _sqlite_bool(tunnel_restart_required),
                    _nullable_text(tunnel_controller),
                    _nullable_text(owner_id),
                    _nullable_text(managed_tunnel_id),
                    _nullable_text(managed_config_path),
                    _nullable_text(managed_task_name),
                    managed_pid,
                    _nullable_text(controller_owner_marker),
                    _nullable_text(applied_config_version),
                    _nullable_text(last_started_at),
                    _nullable_text(last_stopped_at),
                    _nullable_text(last_error_message),
                    updated_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        _ensure_schema(conn)
        return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(connection_channel_settings)").fetchall()}
    for column_name, column_definition in _COLUMN_MIGRATIONS.items():
        if column_name not in existing_columns:
            conn.execute(f"ALTER TABLE connection_channel_settings ADD COLUMN {column_name} {column_definition}")
    conn.commit()


def _default_settings() -> ConnectionChannelSettings:
    return ConnectionChannelSettings(
        cloud_service_host=None,
        cloud_service_port=None,
        remote_access_enabled=False,
        tunnel_restart_required=False,
        tunnel_controller=None,
        owner_id=None,
        managed_tunnel_id=None,
        managed_config_path=None,
        managed_task_name=None,
        managed_pid=None,
        controller_owner_marker=None,
        applied_config_version=None,
        last_started_at=None,
        last_stopped_at=None,
        last_error_message=None,
        updated_at=None,
    )


def _nullable_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool_from_db(value: object) -> bool:
    return bool(int(value or 0))


def _sqlite_bool(value: bool) -> int:
    return 1 if value else 0


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()

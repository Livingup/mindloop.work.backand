from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS object_storage_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    gateway_provider TEXT NOT NULL,
    object_storage_endpoint TEXT NOT NULL,
    object_storage_region TEXT NOT NULL,
    object_storage_bucket_name TEXT NOT NULL,
    object_storage_public_base_url TEXT NOT NULL,
    object_storage_object_key_prefix TEXT NOT NULL,
    object_storage_access_key_id TEXT,
    object_storage_access_key_secret TEXT,
    connect_timeout_seconds INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""

DEFAULT_OBJECT_STORAGE_GATEWAY_PROVIDER = "aliyun_oss"
DEFAULT_OBJECT_STORAGE_ENDPOINT = "https://oss-cn-hongkong.aliyuncs.com"
DEFAULT_OBJECT_STORAGE_REGION = "cn-hongkong"
DEFAULT_OBJECT_STORAGE_BUCKET_NAME = "agent-runtime-resources"
DEFAULT_OBJECT_STORAGE_PUBLIC_BASE_URL = "https://agent-runtime-resources.oss-cn-hongkong.aliyuncs.com/agent-runtime/file-resource"
DEFAULT_OBJECT_STORAGE_OBJECT_KEY_PREFIX = "agent-runtime/file-resource"
DEFAULT_OBJECT_STORAGE_CONNECT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True, slots=True)
class ObjectStorageRuntimeSettings:
    gateway_provider: str
    object_storage_endpoint: str
    object_storage_region: str
    object_storage_bucket_name: str
    object_storage_public_base_url: str
    object_storage_object_key_prefix: str
    object_storage_access_key_id: str | None
    object_storage_access_key_secret: str | None
    connect_timeout_seconds: int
    updated_at: str | None

    @property
    def configured(self) -> bool:
        return all((
            self.gateway_provider.strip(),
            self.object_storage_endpoint.strip(),
            self.object_storage_region.strip(),
            self.object_storage_bucket_name.strip(),
            self.object_storage_public_base_url.strip(),
            (self.object_storage_access_key_id or "").strip(),
            (self.object_storage_access_key_secret or "").strip(),
        ))


class ObjectStorageSettingsRepository:
    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor

    def get_settings(self) -> ObjectStorageRuntimeSettings:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT gateway_provider, object_storage_endpoint, object_storage_region, "
                "object_storage_bucket_name, object_storage_public_base_url, object_storage_object_key_prefix, "
                "object_storage_access_key_id, object_storage_access_key_secret, connect_timeout_seconds, updated_at "
                "FROM object_storage_settings WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return ObjectStorageRuntimeSettings(
                gateway_provider=DEFAULT_OBJECT_STORAGE_GATEWAY_PROVIDER,
                object_storage_endpoint=DEFAULT_OBJECT_STORAGE_ENDPOINT,
                object_storage_region=DEFAULT_OBJECT_STORAGE_REGION,
                object_storage_bucket_name=DEFAULT_OBJECT_STORAGE_BUCKET_NAME,
                object_storage_public_base_url=DEFAULT_OBJECT_STORAGE_PUBLIC_BASE_URL,
                object_storage_object_key_prefix=DEFAULT_OBJECT_STORAGE_OBJECT_KEY_PREFIX,
                object_storage_access_key_id=None,
                object_storage_access_key_secret=None,
                connect_timeout_seconds=DEFAULT_OBJECT_STORAGE_CONNECT_TIMEOUT_SECONDS,
                updated_at=None,
            )
        return ObjectStorageRuntimeSettings(
            gateway_provider=str(row[0] or DEFAULT_OBJECT_STORAGE_GATEWAY_PROVIDER),
            object_storage_endpoint=str(row[1] or DEFAULT_OBJECT_STORAGE_ENDPOINT),
            object_storage_region=str(row[2] or DEFAULT_OBJECT_STORAGE_REGION),
            object_storage_bucket_name=str(row[3] or DEFAULT_OBJECT_STORAGE_BUCKET_NAME),
            object_storage_public_base_url=str(row[4] or DEFAULT_OBJECT_STORAGE_PUBLIC_BASE_URL),
            object_storage_object_key_prefix=str(row[5] or DEFAULT_OBJECT_STORAGE_OBJECT_KEY_PREFIX),
            object_storage_access_key_id=_nullable_text(row[6]),
            object_storage_access_key_secret=_nullable_text(row[7]),
            connect_timeout_seconds=int(row[8] or DEFAULT_OBJECT_STORAGE_CONNECT_TIMEOUT_SECONDS),
            updated_at=str(row[9]) if row[9] is not None else None,
        )

    def save_settings(
        self,
        *,
        gateway_provider: str,
        object_storage_endpoint: str,
        object_storage_region: str,
        object_storage_bucket_name: str,
        object_storage_public_base_url: str,
        object_storage_object_key_prefix: str,
        object_storage_access_key_id: str | None,
        object_storage_access_key_secret: str | None,
        connect_timeout_seconds: int,
    ) -> ObjectStorageRuntimeSettings:
        now = _utc_now_text()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO object_storage_settings ("
                "id, gateway_provider, object_storage_endpoint, object_storage_region, "
                "object_storage_bucket_name, object_storage_public_base_url, object_storage_object_key_prefix, "
                "object_storage_access_key_id, object_storage_access_key_secret, connect_timeout_seconds, updated_at"
                ") VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "gateway_provider = excluded.gateway_provider, "
                "object_storage_endpoint = excluded.object_storage_endpoint, "
                "object_storage_region = excluded.object_storage_region, "
                "object_storage_bucket_name = excluded.object_storage_bucket_name, "
                "object_storage_public_base_url = excluded.object_storage_public_base_url, "
                "object_storage_object_key_prefix = excluded.object_storage_object_key_prefix, "
                "object_storage_access_key_id = excluded.object_storage_access_key_id, "
                "object_storage_access_key_secret = excluded.object_storage_access_key_secret, "
                "connect_timeout_seconds = excluded.connect_timeout_seconds, "
                "updated_at = excluded.updated_at",
                (
                    gateway_provider.strip() or DEFAULT_OBJECT_STORAGE_GATEWAY_PROVIDER,
                    object_storage_endpoint.strip() or DEFAULT_OBJECT_STORAGE_ENDPOINT,
                    object_storage_region.strip() or DEFAULT_OBJECT_STORAGE_REGION,
                    object_storage_bucket_name.strip() or DEFAULT_OBJECT_STORAGE_BUCKET_NAME,
                    (object_storage_public_base_url.strip() or DEFAULT_OBJECT_STORAGE_PUBLIC_BASE_URL).rstrip("/"),
                    (object_storage_object_key_prefix.strip() or DEFAULT_OBJECT_STORAGE_OBJECT_KEY_PREFIX).strip("/"),
                    _normalize_nullable_text(object_storage_access_key_id),
                    _normalize_nullable_text(object_storage_access_key_secret),
                    int(connect_timeout_seconds) if int(connect_timeout_seconds) > 0 else DEFAULT_OBJECT_STORAGE_CONNECT_TIMEOUT_SECONDS,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_settings()

    def db_path(self) -> Path:
        return Path(self._data_dir_accessor()).expanduser() / "object_storage_settings.db"

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute(_SCHEMA)
        conn.commit()
        return conn


def _nullable_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_nullable_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()

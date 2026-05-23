from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS lacp_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL,
    registry_url TEXT NOT NULL,
    api_key TEXT,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class LacpRuntimeSettings:
    enabled: bool
    registry_url: str
    api_key: str | None
    updated_at: str | None


class LacpSettingsRepository:
    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor

    def get_settings(self) -> LacpRuntimeSettings:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT enabled, registry_url, api_key, updated_at FROM lacp_settings WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return LacpRuntimeSettings(
                enabled=False,
                registry_url="",
                api_key=None,
                updated_at=None,
            )
        api_key = str(row[2]) if row[2] is not None else None
        return LacpRuntimeSettings(
            enabled=bool(row[0]),
            registry_url=str(row[1] or ""),
            api_key=api_key if api_key else None,
            updated_at=str(row[3]),
        )

    def save_settings(
        self,
        *,
        enabled: bool,
        registry_url: str,
        api_key: str | None,
    ) -> LacpRuntimeSettings:
        normalized_url = registry_url.strip()
        normalized_api_key = api_key.strip() if isinstance(api_key, str) else None
        if normalized_api_key == "":
            normalized_api_key = None
        now = _utc_now_text()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO lacp_settings (id, enabled, registry_url, api_key, updated_at) "
                "VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                " enabled = excluded.enabled, "
                " registry_url = excluded.registry_url, "
                " api_key = excluded.api_key, "
                " updated_at = excluded.updated_at",
                (1 if enabled else 0, normalized_url, normalized_api_key, now),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_settings()

    def db_path(self) -> Path:
        return Path(self._data_dir_accessor()).expanduser() / "lacp_settings.db"

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute(_SCHEMA)
        conn.commit()
        return conn


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS image_generation_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    llm_policy_group_id TEXT,
    default_request_params_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class ImageGenerationRuntimeSettings:
    llm_policy_group_id: str | None
    default_request_params: dict[str, Any] = field(default_factory=dict)
    updated_at: str | None = None


class ImageGenerationSettingsRepository:
    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor

    def get_settings(self) -> ImageGenerationRuntimeSettings:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT llm_policy_group_id, default_request_params_json, updated_at "
                "FROM image_generation_settings WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return ImageGenerationRuntimeSettings(
                llm_policy_group_id=None,
                default_request_params={},
                updated_at=None,
            )
        return ImageGenerationRuntimeSettings(
            llm_policy_group_id=_nullable_text(row[0]),
            default_request_params=_json_object(row[1]),
            updated_at=str(row[2]) if row[2] is not None else None,
        )

    def save_settings(
        self,
        *,
        llm_policy_group_id: str | None,
        default_request_params: dict[str, Any] | None = None,
    ) -> ImageGenerationRuntimeSettings:
        normalized_policy_id = _nullable_text(llm_policy_group_id)
        params = dict(default_request_params or {})
        now = _utc_now_text()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO image_generation_settings ("
                "id, llm_policy_group_id, default_request_params_json, updated_at"
                ") VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "llm_policy_group_id = excluded.llm_policy_group_id, "
                "default_request_params_json = excluded.default_request_params_json, "
                "updated_at = excluded.updated_at",
                (
                    normalized_policy_id,
                    json.dumps(params, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_settings()

    def db_path(self) -> Path:
        return Path(self._data_dir_accessor()).expanduser() / "image_generation_settings.db"

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute(_SCHEMA)
        conn.commit()
        return conn


def _json_object(value: object) -> dict[str, Any]:
    if not value:
        return {}
    data = json.loads(str(value))
    if not isinstance(data, dict):
        raise ValueError("image_generation_settings.default_request_params_json must be a JSON object.")
    return data


def _nullable_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()

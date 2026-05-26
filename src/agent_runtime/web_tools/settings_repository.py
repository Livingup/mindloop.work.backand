from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_runtime.web_tools.models import WebToolPolicyCandidate, WebToolsRuntimeSettings


_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_tools_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    candidates_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class WebToolsSettingsRepository:
    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor

    def get_settings(self) -> WebToolsRuntimeSettings:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT candidates_json, updated_at FROM web_tools_settings WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return WebToolsRuntimeSettings()
        return WebToolsRuntimeSettings(
            candidates=_load_candidates(str(row[0] or "[]")),
            updated_at=str(row[1]) if row[1] is not None else None,
        )

    def save_settings(
        self,
        *,
        candidates: tuple[WebToolPolicyCandidate, ...],
    ) -> WebToolsRuntimeSettings:
        settings = WebToolsRuntimeSettings(candidates=candidates)
        now = _utc_now_text()
        payload = json.dumps([item.to_dict() for item in settings.candidates], ensure_ascii=False)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO web_tools_settings (id, candidates_json, updated_at) "
                "VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "candidates_json = excluded.candidates_json, "
                "updated_at = excluded.updated_at",
                (payload, now),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_settings()

    def db_path(self) -> Path:
        return Path(self._data_dir_accessor()).expanduser() / "web_tools_settings.db"

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute(_SCHEMA)
        conn.commit()
        return conn


def _load_candidates(raw: str) -> tuple[WebToolPolicyCandidate, ...]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"web tools candidates_json is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("web tools candidates_json must be a JSON array.")
    candidates: list[WebToolPolicyCandidate] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("web tools candidate item must be a JSON object.")
        candidates.append(WebToolPolicyCandidate.from_dict(item))
    return tuple(candidates)


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()

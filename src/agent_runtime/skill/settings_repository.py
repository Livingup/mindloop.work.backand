from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    skills_dir TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class SkillRuntimeSettings:
    skills_dir: str
    updated_at: str | None


class SkillSettingsRepository:
    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor

    def get_settings(self, *, default_skills_dir: str) -> SkillRuntimeSettings:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT skills_dir, updated_at FROM skill_settings WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return SkillRuntimeSettings(
                skills_dir=default_skills_dir,
                updated_at=None,
            )
        skills_dir = str(row[0] or "").strip() or default_skills_dir
        return SkillRuntimeSettings(
            skills_dir=skills_dir,
            updated_at=str(row[1]) if row[1] is not None else None,
        )

    def save_settings(self, *, skills_dir: str) -> SkillRuntimeSettings:
        normalized_skills_dir = skills_dir.strip()
        if not normalized_skills_dir:
            raise ValueError("Skill 根目录不能为空。")
        now = _utc_now_text()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO skill_settings (id, skills_dir, updated_at) "
                "VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "skills_dir = excluded.skills_dir, "
                "updated_at = excluded.updated_at",
                (normalized_skills_dir, now),
            )
            conn.commit()
        finally:
            conn.close()
        return SkillRuntimeSettings(skills_dir=normalized_skills_dir, updated_at=now)

    def db_path(self) -> Path:
        return Path(self._data_dir_accessor()).expanduser() / "skill_settings.db"

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute(_SCHEMA)
        conn.commit()
        return conn


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()

# -*- coding: utf-8 -*-
"""Scene 模块 SQLite 仓储实现。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from agent_runtime.scene.project import Project
from agent_runtime.scene.scene import Scene

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS projects (
    project_id     TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    workspace_path TEXT NOT NULL UNIQUE,
    created_at     TEXT NOT NULL,
    last_opened_at TEXT NOT NULL,
    metadata_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_last_opened_at ON projects(last_opened_at);

CREATE TABLE IF NOT EXISTS scenes (
    scene_id                       TEXT PRIMARY KEY,
    project_id                     TEXT NOT NULL,
    name                           TEXT NOT NULL,
    description                    TEXT NOT NULL,
    status                         TEXT NOT NULL,
    created_at                     TEXT NOT NULL,
    last_active_at                 TEXT NOT NULL,
    default_agent_definition_id    TEXT,
    default_llm_policy_group_id    TEXT,
    default_llm_provider_id        TEXT,
    default_llm_model_binding_id   TEXT,
    metadata_json                  TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_scenes_project_id ON scenes(project_id);
CREATE INDEX IF NOT EXISTS idx_scenes_last_active_at ON scenes(last_active_at);
CREATE INDEX IF NOT EXISTS idx_scenes_status ON scenes(status);
"""


class SqliteSceneRepository:
    """Scene 模块 SQLite 仓储。

    仓储直接读写 SQLite，不维护应用层缓存；SQLite 开启 WAL 以支持本机多进程读写，
    开启外键约束用于 project 删除时级联删除其下所有 scene。
    """

    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        # 配置目录访问器；每次使用 _connection() 时检查路径，支持配置热更新切换目录。
        self._data_dir_accessor = data_dir_accessor
        self._conn: sqlite3.Connection | None = None
        self._db_path: Path | None = None
        self._lock = RLock()

    def close(self) -> None:
        """关闭底层 SQLite 连接。"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._db_path = None

    # ── Project ────────────────────────────────────────────────────

    def create_project(self, project: Project) -> Project:
        """创建项目；project_id 或 workspace_path 重复时抛出 ValueError。"""
        with self._lock:
            conn = self._connection()
            try:
                conn.execute(
                    """
                    INSERT INTO projects (
                        project_id, name, workspace_path, created_at, last_opened_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    _project_to_row(project),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise ValueError(
                    f"创建项目失败：project_id 或 workspace_path 已存在: "
                    f"project_id={project.project_id}, workspace_path={project.workspace_path}"
                ) from exc
            return project

    def save_project(self, project: Project) -> Project:
        """保存项目当前状态（upsert）。"""
        with self._lock:
            conn = self._connection()
            try:
                conn.execute(
                    """
                    INSERT INTO projects (
                        project_id, name, workspace_path, created_at, last_opened_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        name = excluded.name,
                        workspace_path = excluded.workspace_path,
                        created_at = excluded.created_at,
                        last_opened_at = excluded.last_opened_at,
                        metadata_json = excluded.metadata_json
                    """,
                    _project_to_row(project),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise ValueError(
                    f"保存项目失败：workspace_path 已被其他项目占用: {project.workspace_path}"
                ) from exc
            return project

    def get_project(self, project_id: str) -> Project | None:
        """按 project_id 获取项目。"""
        with self._lock:
            row = self._connection().execute(
                "SELECT project_id, name, workspace_path, created_at, last_opened_at, metadata_json "
                "FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            return _row_to_project(row) if row else None

    def get_project_by_workspace_path(self, workspace_path: str) -> Project | None:
        """按 workspace_path 查找项目。"""
        with self._lock:
            row = self._connection().execute(
                "SELECT project_id, name, workspace_path, created_at, last_opened_at, metadata_json "
                "FROM projects WHERE workspace_path = ?",
                (workspace_path,),
            ).fetchone()
            return _row_to_project(row) if row else None

    def list_projects(self) -> list[Project]:
        """列出全部项目，按 last_opened_at 倒序。"""
        with self._lock:
            rows = self._connection().execute(
                "SELECT project_id, name, workspace_path, created_at, last_opened_at, metadata_json "
                "FROM projects ORDER BY last_opened_at DESC"
            ).fetchall()
            return [_row_to_project(row) for row in rows]

    def delete_project(self, project_id: str) -> bool:
        """删除项目；关联 scenes 通过外键级联删除。"""
        with self._lock:
            conn = self._connection()
            cursor = conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            conn.commit()
            return cursor.rowcount > 0

    # ── Scene ──────────────────────────────────────────────────────

    def create_scene(self, scene: Scene) -> Scene:
        """创建场景；project_id 不存在时抛 KeyError；scene_id 重复时抛 ValueError。"""
        with self._lock:
            conn = self._connection()
            # 显式校验 project 存在性，提供清晰的错误语义；外键约束会兜底。
            project_row = conn.execute(
                "SELECT 1 FROM projects WHERE project_id = ?", (scene.project_id,)
            ).fetchone()
            if project_row is None:
                raise KeyError(f"场景关联的项目不存在: project_id={scene.project_id}")
            try:
                conn.execute(
                    """
                    INSERT INTO scenes (
                        scene_id, project_id, name, description, status,
                        created_at, last_active_at,
                        default_agent_definition_id, default_llm_policy_group_id,
                        default_llm_provider_id, default_llm_model_binding_id,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _scene_to_row(scene),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise ValueError(f"创建场景失败：scene_id 已存在: {scene.scene_id}") from exc
            return scene

    def save_scene(self, scene: Scene) -> Scene:
        """保存场景当前状态（upsert）。"""
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT INTO scenes (
                    scene_id, project_id, name, description, status,
                    created_at, last_active_at,
                    default_agent_definition_id, default_llm_policy_group_id,
                    default_llm_provider_id, default_llm_model_binding_id,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scene_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    name = excluded.name,
                    description = excluded.description,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    last_active_at = excluded.last_active_at,
                    default_agent_definition_id = excluded.default_agent_definition_id,
                    default_llm_policy_group_id = excluded.default_llm_policy_group_id,
                    default_llm_provider_id = excluded.default_llm_provider_id,
                    default_llm_model_binding_id = excluded.default_llm_model_binding_id,
                    metadata_json = excluded.metadata_json
                """,
                _scene_to_row(scene),
            )
            conn.commit()
            return scene

    def get_scene(self, scene_id: str) -> Scene | None:
        """按 scene_id 获取场景。"""
        with self._lock:
            row = self._connection().execute(
                """
                SELECT scene_id, project_id, name, description, status,
                       created_at, last_active_at,
                       default_agent_definition_id, default_llm_policy_group_id,
                       default_llm_provider_id, default_llm_model_binding_id,
                       metadata_json
                FROM scenes WHERE scene_id = ?
                """,
                (scene_id,),
            ).fetchone()
            return _row_to_scene(row) if row else None

    def list_scenes_by_project(self, project_id: str) -> list[Scene]:
        """列出指定项目下的全部场景，按 last_active_at 倒序。"""
        with self._lock:
            rows = self._connection().execute(
                """
                SELECT scene_id, project_id, name, description, status,
                       created_at, last_active_at,
                       default_agent_definition_id, default_llm_policy_group_id,
                       default_llm_provider_id, default_llm_model_binding_id,
                       metadata_json
                FROM scenes
                WHERE project_id = ?
                ORDER BY last_active_at DESC
                """,
                (project_id,),
            ).fetchall()
            return [_row_to_scene(row) for row in rows]

    def delete_scene(self, scene_id: str) -> bool:
        """删除场景。"""
        with self._lock:
            conn = self._connection()
            cursor = conn.execute("DELETE FROM scenes WHERE scene_id = ?", (scene_id,))
            conn.commit()
            return cursor.rowcount > 0

    # ── 内部连接管理 ─────────────────────────────────────────────────

    def _connection(self) -> sqlite3.Connection:
        """获取当前配置目录对应的 SQLite 连接，配置路径变化时自动重建连接。"""
        data_dir = Path(self._data_dir_accessor()).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "scene.db"
        if self._conn is not None and self._db_path == db_path:
            return self._conn
        if self._conn is not None:
            self._conn.close()
        self._conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._db_path = db_path
        return self._conn


# ── 行转换辅助 ─────────────────────────────────────────────────────


def _project_to_row(project: Project) -> tuple[Any, ...]:
    """把 Project 实体转换为 SQLite 行参数。"""
    return (
        project.project_id,
        project.name,
        project.workspace_path,
        _datetime_to_text(project.created_at),
        _datetime_to_text(project.last_opened_at),
        json.dumps(project.metadata, ensure_ascii=False),
    )


def _row_to_project(row: tuple[Any, ...]) -> Project:
    """把 SQLite 行还原为 Project 实体。"""
    return Project(
        project_id=row[0],
        name=row[1],
        workspace_path=row[2],
        created_at=_text_to_datetime(row[3]),
        last_opened_at=_text_to_datetime(row[4]),
        metadata=json.loads(row[5]) if row[5] else {},
    )


def _scene_to_row(scene: Scene) -> tuple[Any, ...]:
    """把 Scene 实体转换为 SQLite 行参数。"""
    return (
        scene.scene_id,
        scene.project_id,
        scene.name,
        scene.description,
        scene.status,
        _datetime_to_text(scene.created_at),
        _datetime_to_text(scene.last_active_at),
        scene.default_agent_definition_id,
        scene.default_llm_policy_group_id,
        scene.default_llm_provider_id,
        scene.default_llm_model_binding_id,
        json.dumps(scene.metadata, ensure_ascii=False),
    )


def _row_to_scene(row: tuple[Any, ...]) -> Scene:
    """把 SQLite 行还原为 Scene 实体。"""
    return Scene(
        scene_id=row[0],
        project_id=row[1],
        name=row[2],
        description=row[3],
        status=row[4],
        created_at=_text_to_datetime(row[5]),
        last_active_at=_text_to_datetime(row[6]),
        default_agent_definition_id=row[7],
        default_llm_policy_group_id=row[8],
        default_llm_provider_id=row[9],
        default_llm_model_binding_id=row[10],
        metadata=json.loads(row[11]) if row[11] else {},
    )


def _datetime_to_text(value: datetime) -> str:
    """将 datetime 转为 ISO 文本。"""
    return value.isoformat()


def _text_to_datetime(value: str) -> datetime:
    """将 ISO 文本还原为 datetime。"""
    return datetime.fromisoformat(value)

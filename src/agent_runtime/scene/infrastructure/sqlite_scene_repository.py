# -*- coding: utf-8 -*-
"""Scene 模块 SQLite 仓储实现 — 全局统一数据库模式。

所有 workspace 共享一份位于 ``SceneConfig.scenes_db_path`` 的全局 SQLite 数据库
（默认 ``~/.spread-ai/scene/scene.db``）；仓储进程内只维护一条长连接。
数据库内不存 projects 表 — 项目身份由
:class:`agent_runtime.scene.infrastructure.file_project_manifest_repository.FileProjectManifestRepository`
管理；scenes 表通过 ``project_id`` 列与 manifest 中的项目身份关联。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from agent_runtime.scene.scene import Scene

logger = logging.getLogger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS scenes (
    scene_id                       TEXT PRIMARY KEY,
    project_id                     TEXT NOT NULL,
    name                           TEXT NOT NULL,
    description                    TEXT NOT NULL,
    status                         TEXT NOT NULL,
    created_at                     TEXT NOT NULL,
    last_active_at                 TEXT NOT NULL,
    bound_agent_definition_id      TEXT NOT NULL,
    bound_root_conversation_id     TEXT,
    metadata_json                  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scenes_project_id ON scenes(project_id);
CREATE INDEX IF NOT EXISTS idx_scenes_last_active_at ON scenes(last_active_at);
CREATE INDEX IF NOT EXISTS idx_scenes_status ON scenes(status);
"""

# 历史遗留：旧 schema 的 default_agent_definition_id 列在迁移时若为 NULL 回填的哨兵值。
_LEGACY_AGENT_DEFINITION_ID = "__legacy_unknown__"


def _migrate_scenes_table(conn: sqlite3.Connection) -> None:
    """将旧版 scenes 表（default_* 列）迁移到新结构（bound_* 列）。

    迁移触发条件：scenes 表存在且含 default_agent_definition_id 列。
    迁移内容：
    - default_agent_definition_id → bound_agent_definition_id（NULL 回填为哨兵）
    - 删除 default_llm_policy_group_id / default_llm_provider_id / default_llm_model_binding_id
    - 新增 bound_root_conversation_id（旧数据填 NULL，由 attach 用例首次绑定时回填）

    迁移幂等：已是新结构（含 bound_agent_definition_id 列）时直接返回。
    """
    rows = conn.execute("PRAGMA table_info(scenes)").fetchall()
    if not rows:
        return
    columns = {row[1] for row in rows}
    if "bound_agent_definition_id" in columns:
        return
    if "default_agent_definition_id" not in columns:
        return
    conn.execute("ALTER TABLE scenes RENAME TO scenes_legacy_v1")
    conn.executescript(_SCHEMA)
    conn.execute(
        f"""
        INSERT INTO scenes (
            scene_id, project_id, name, description, status,
            created_at, last_active_at,
            bound_agent_definition_id, bound_root_conversation_id,
            metadata_json
        )
        SELECT
            scene_id, project_id, name, description, status,
            created_at, last_active_at,
            COALESCE(NULLIF(default_agent_definition_id, ''), '{_LEGACY_AGENT_DEFINITION_ID}'),
            NULL,
            metadata_json
        FROM scenes_legacy_v1
        """
    )
    conn.execute("DROP TABLE scenes_legacy_v1")
    conn.commit()


class SqliteSceneRepository:
    """Scene 模块 SQLite 仓储 — 全局统一数据库实现。

    所有 workspace 共享同一物理数据库，进程内仅维护一条长连接；CRUD 通过 ``project_id``
    或 ``scene_id`` 直接定位行，不再按 workspace 路由。线程安全：使用 ``RLock`` 保护连接
    创建与每条 SQL 写入；SQLite 启 WAL 以支持本机多进程并发读写。

    连接策略：
    - DB 路径来自 ``db_path_accessor`` 回调（典型实现读取 ``ConfigRegistry.scene.scenes_db_path``），
      支持配置热更新场景下 lazy 解析；
    - 进程生命周期内仅打开一次，并复用；首次访问触发建目录 + schema + 迁移；
    - ``close()`` 关闭连接（进程退出时由组合根调用），下次访问自动重建。
    """

    def __init__(self, db_path_accessor: Callable[[], str]) -> None:
        self._db_path_accessor = db_path_accessor
        self._lock = RLock()
        self._conn: sqlite3.Connection | None = None

    def close(self) -> None:
        """关闭 SQLite 连接。"""
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                logger.warning("关闭 scene.db 连接失败 err=%s", exc)
            finally:
                self._conn = None

    def ensure_initialized(self) -> None:
        """确保全局 scene 数据库 + schema 已就绪。"""
        self._connection()

    def create_scene(self, scene: Scene) -> Scene:
        """创建场景；scene_id 重复时抛 ValueError。"""
        with self._lock:
            conn = self._connection()
            try:
                conn.execute(
                    """
                    INSERT INTO scenes (
                        scene_id, project_id, name, description, status,
                        created_at, last_active_at,
                        bound_agent_definition_id, bound_root_conversation_id,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _scene_to_row(scene),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise ValueError(f"创建场景失败：scene_id 已存在: {scene.scene_id}") from exc
            return scene

    def save_scene(self, scene: Scene) -> Scene:
        """upsert 保存场景当前状态。"""
        with self._lock:
            conn = self._connection()
            conn.execute(
                """
                INSERT INTO scenes (
                    scene_id, project_id, name, description, status,
                    created_at, last_active_at,
                    bound_agent_definition_id, bound_root_conversation_id,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scene_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    name = excluded.name,
                    description = excluded.description,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    last_active_at = excluded.last_active_at,
                    bound_agent_definition_id = excluded.bound_agent_definition_id,
                    bound_root_conversation_id = excluded.bound_root_conversation_id,
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
                       bound_agent_definition_id, bound_root_conversation_id,
                       metadata_json
                FROM scenes WHERE scene_id = ?
                """,
                (scene_id,),
            ).fetchone()
            return _row_to_scene(row) if row else None

    def list_scenes_by_project(self, project_id: str) -> list[Scene]:
        """列出属于该项目的全部场景，按 last_active_at 倒序。"""
        with self._lock:
            rows = self._connection().execute(
                """
                SELECT scene_id, project_id, name, description, status,
                       created_at, last_active_at,
                       bound_agent_definition_id, bound_root_conversation_id,
                       metadata_json
                FROM scenes
                WHERE project_id = ?
                ORDER BY last_active_at DESC
                """,
                (project_id,),
            ).fetchall()
            return [_row_to_scene(row) for row in rows]

    def delete_scene(self, scene_id: str) -> bool:
        """删除场景；幂等。"""
        with self._lock:
            conn = self._connection()
            cursor = conn.execute("DELETE FROM scenes WHERE scene_id = ?", (scene_id,))
            conn.commit()
            return cursor.rowcount > 0

    # ── 内部连接管理 ─────────────────────────────────────────────────

    def _connection(self) -> sqlite3.Connection:
        """获取或创建全局 scene 数据库连接。

        首次访问从 ``db_path_accessor`` 解析路径、创建父目录、建立连接、跑 schema
        初始化与历史迁移；后续访问直接复用连接。
        """
        with self._lock:
            if self._conn is not None:
                return self._conn
            db_path = Path(self._db_path_accessor()).expanduser()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            _migrate_scenes_table(conn)
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
            logger.info("已打开全局 scene 数据库 path=%s", db_path)
            return conn


# ── 行转换辅助 ─────────────────────────────────────────────────────


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
        scene.bound_agent_definition_id,
        scene.bound_root_conversation_id,
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
        bound_agent_definition_id=row[7],
        bound_root_conversation_id=row[8],
        metadata=json.loads(row[9]) if row[9] else {},
    )


def _datetime_to_text(value: datetime) -> str:
    """将 datetime 转为 ISO 文本。"""
    return value.isoformat()


def _text_to_datetime(value: str) -> datetime:
    """将 ISO 文本还原为 datetime。"""
    return datetime.fromisoformat(value)

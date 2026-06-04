from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from agent_runtime.config import ConfigRegistry


LIFECYCLE_ENDED_TASKS = "ended_tasks"
LIFECYCLE_ARCHIVED_SCENES = "archived_scenes"
LIFECYCLE_CLOSED_SCENES = "closed_scenes"

CATEGORY_COMMUNICATION_MESSAGES = "communication_messages"
CATEGORY_CHAT_BUBBLES = "chat_bubbles"
CATEGORY_LLM_HTTP_TRACE = "llm_http_trace"
CATEGORY_CONTEXT_COMPRESSION_ARCHIVE = "context_compression_archive"
CATEGORY_TERMINAL_HISTORY = "terminal_history"

SUPPORTED_LIFECYCLE_FILTERS: frozenset[str] = frozenset({
    LIFECYCLE_ENDED_TASKS,
    LIFECYCLE_ARCHIVED_SCENES,
    LIFECYCLE_CLOSED_SCENES,
})

CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_COMMUNICATION_MESSAGES: "Communication messages",
    CATEGORY_CHAT_BUBBLES: "Chat Bubbles",
    CATEGORY_LLM_HTTP_TRACE: "LLM HTTP Trace",
    CATEGORY_CONTEXT_COMPRESSION_ARCHIVE: "Context Compression archive",
    CATEGORY_TERMINAL_HISTORY: "Terminal history",
}


@dataclass(frozen=True, slots=True)
class LogsStorePaths:
    agent_runtime_db: Path
    scene_db: Path
    communication_db: Path
    chat_bubbles_db: Path
    llm_http_trace_db: Path
    context_compression_dir: Path
    context_compression_db: Path
    terminal_db: Path


@dataclass(frozen=True, slots=True)
class CleanupScope:
    lifecycle_filters: tuple[str, ...]
    task_ids: frozenset[str]
    scene_ids: frozenset[str]

    @property
    def is_empty(self) -> bool:
        return not self.task_ids and not self.scene_ids


class LogsManagementService:
    """Logs 工作区应用服务。

    该服务只面向日志工作区提供两类能力：实时存储大小总览，以及用户显式触发的
    生命周期过滤 + 类别交集清理。它直接检查各 SQLite / 文件系统存储的当前状态，
    不依赖 system-settings 的 persistence_inventory 元数据来计算大小或删除范围。
    """

    def __init__(self, paths_accessor: Callable[[], LogsStorePaths] | None = None) -> None:
        self._paths_accessor = paths_accessor or _paths_from_config

    def get_overview(self) -> dict[str, Any]:
        paths = self._paths_accessor()
        items = [
            self._sqlite_store_overview(
                store_id=CATEGORY_COMMUNICATION_MESSAGES,
                title=CATEGORY_LABELS[CATEGORY_COMMUNICATION_MESSAGES],
                db_path=paths.communication_db,
                table_name="communication_messages",
            ),
            self._sqlite_store_overview(
                store_id=CATEGORY_CHAT_BUBBLES,
                title=CATEGORY_LABELS[CATEGORY_CHAT_BUBBLES],
                db_path=paths.chat_bubbles_db,
                table_name="chat_bubble_messages",
            ),
            self._sqlite_store_overview(
                store_id=CATEGORY_LLM_HTTP_TRACE,
                title=CATEGORY_LABELS[CATEGORY_LLM_HTTP_TRACE],
                db_path=paths.llm_http_trace_db,
                table_name="llm_http_traces",
            ),
            self._context_compression_overview(paths.context_compression_dir, paths.context_compression_db),
            self._sqlite_store_overview(
                store_id=CATEGORY_TERMINAL_HISTORY,
                title=CATEGORY_LABELS[CATEGORY_TERMINAL_HISTORY],
                db_path=paths.terminal_db,
                table_name="terminals",
            ),
        ]
        return {
            "items": items,
            "total_size_bytes": sum(int(item["size_bytes"]) for item in items),
        }

    def preview_cleanup(
        self,
        *,
        lifecycle_filters: Iterable[str],
        categories: Iterable[str],
    ) -> dict[str, Any]:
        return self._build_cleanup_result(
            lifecycle_filters=tuple(lifecycle_filters),
            categories=tuple(categories),
            execute=False,
        )

    def execute_cleanup(
        self,
        *,
        lifecycle_filters: Iterable[str],
        categories: Iterable[str],
    ) -> dict[str, Any]:
        return self._build_cleanup_result(
            lifecycle_filters=tuple(lifecycle_filters),
            categories=tuple(categories),
            execute=True,
        )

    def _build_cleanup_result(
        self,
        *,
        lifecycle_filters: tuple[str, ...],
        categories: tuple[str, ...],
        execute: bool,
    ) -> dict[str, Any]:
        selected_categories = _normalize_categories(categories)
        scope = self._resolve_scope(lifecycle_filters)
        paths = self._paths_accessor()
        category_results = [
            self._category_result(paths, scope, category, execute=execute)
            for category in selected_categories
        ]
        return {
            "mode": "execute" if execute else "preview",
            "lifecycle_filters": list(scope.lifecycle_filters),
            "categories": list(selected_categories),
            "scope": {
                "task_count": len(scope.task_ids),
                "scene_count": len(scope.scene_ids),
            },
            "category_results": category_results,
            "total_matched_count": sum(int(item["matched_count"]) for item in category_results),
            "total_deleted_count": sum(int(item["deleted_count"]) for item in category_results),
        }

    def _resolve_scope(self, lifecycle_filters: tuple[str, ...]) -> CleanupScope:
        filters = _normalize_lifecycle_filters(lifecycle_filters)
        paths = self._paths_accessor()
        task_id_sets: list[set[str]] = []
        scene_ids: set[str] = set()
        has_ended_task_filter = LIFECYCLE_ENDED_TASKS in filters
        has_scene_filter = LIFECYCLE_ARCHIVED_SCENES in filters or LIFECYCLE_CLOSED_SCENES in filters

        if has_ended_task_filter:
            task_id_sets.append(_ended_task_ids(paths.agent_runtime_db))

        if has_scene_filter:
            archived_scene_ids = _archived_scene_ids(paths.scene_db)
            scene_task_ids = _task_ids_for_scenes(paths.agent_runtime_db, archived_scene_ids)
            task_id_sets.append(scene_task_ids)
            # 同时选择任务生命周期和 Scene 生命周期时采用交集语义：
            # 只有能明确关联到交集任务的日志才会删除，避免仅按 scene_id 扩大到未结束任务或场景级共享记录。
            if not has_ended_task_filter:
                scene_ids.update(archived_scene_ids)

        task_ids = set.intersection(*task_id_sets) if task_id_sets else set()

        return CleanupScope(
            lifecycle_filters=filters,
            task_ids=frozenset(task_ids),
            scene_ids=frozenset(scene_ids),
        )

    def _category_result(
        self,
        paths: LogsStorePaths,
        scope: CleanupScope,
        category: str,
        *,
        execute: bool,
    ) -> dict[str, Any]:
        if category == CATEGORY_COMMUNICATION_MESSAGES:
            matched = self._count_communication_messages(paths.communication_db, scope)
            deleted = self._delete_communication_messages(paths.communication_db, scope) if execute else 0
        elif category == CATEGORY_CHAT_BUBBLES:
            matched = self._count_chat_bubbles(paths.chat_bubbles_db, scope)
            deleted = self._delete_chat_bubbles(paths.chat_bubbles_db, scope) if execute else 0
        elif category == CATEGORY_LLM_HTTP_TRACE:
            matched = self._count_task_table(paths.llm_http_trace_db, "llm_http_traces", "task_id", scope)
            deleted = self._delete_task_table(paths.llm_http_trace_db, "llm_http_traces", "task_id", scope) if execute else 0
        elif category == CATEGORY_CONTEXT_COMPRESSION_ARCHIVE:
            matched = self._count_task_table(
                paths.context_compression_db,
                "context_compression_jobs",
                "task_id",
                scope,
            )
            deleted = self._delete_context_compression_archive(paths.context_compression_dir, paths.context_compression_db, scope) if execute else 0
        elif category == CATEGORY_TERMINAL_HISTORY:
            matched = self._count_terminal_history(paths.terminal_db, scope)
            deleted = self._delete_terminal_history(paths.terminal_db, scope) if execute else 0
        else:  # pragma: no cover - guarded by _normalize_categories
            raise ValueError(f"不支持的日志清理类别: {category}")
        return {
            "category": category,
            "label": CATEGORY_LABELS[category],
            "matched_count": matched,
            "deleted_count": deleted,
        }

    def _sqlite_store_overview(
        self,
        *,
        store_id: str,
        title: str,
        db_path: Path,
        table_name: str,
    ) -> dict[str, Any]:
        record_count = _count_rows(db_path, table_name)
        size_bytes = _sqlite_store_size(db_path)
        return {
            "store_id": store_id,
            "title": title,
            "size_bytes": size_bytes,
            "record_count": record_count,
            "path": str(db_path),
            "available": db_path.exists(),
        }

    def _context_compression_overview(self, archive_dir: Path, db_path: Path) -> dict[str, Any]:
        return {
            "store_id": CATEGORY_CONTEXT_COMPRESSION_ARCHIVE,
            "title": CATEGORY_LABELS[CATEGORY_CONTEXT_COMPRESSION_ARCHIVE],
            "size_bytes": _sqlite_store_size(db_path) + _directory_size(archive_dir / "snapshots") + _directory_size(archive_dir / "summaries"),
            "record_count": _count_rows(db_path, "context_compression_jobs"),
            "path": str(archive_dir),
            "available": archive_dir.exists() or db_path.exists(),
        }

    def _count_chat_bubbles(self, db_path: Path, scope: CleanupScope) -> int:
        where_sql, params = _task_scene_where(scope, task_column="task_id", scene_column="scene_id")
        return _count_where(db_path, "chat_bubble_messages", where_sql, params)

    def _delete_chat_bubbles(self, db_path: Path, scope: CleanupScope) -> int:
        where_sql, params = _task_scene_where(scope, task_column="task_id", scene_column="scene_id")
        return _delete_where(db_path, "chat_bubble_messages", where_sql, params)

    def _count_task_table(self, db_path: Path, table_name: str, task_column: str, scope: CleanupScope) -> int:
        where_sql, params = _task_only_where(scope, task_column)
        return _count_where(db_path, table_name, where_sql, params)

    def _delete_task_table(self, db_path: Path, table_name: str, task_column: str, scope: CleanupScope) -> int:
        where_sql, params = _task_only_where(scope, task_column)
        return _delete_where(db_path, table_name, where_sql, params)

    def _communication_messages_where(self, scope: CleanupScope) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope.task_ids:
            task_values = sorted(scope.task_ids)
            placeholders = _placeholders(task_values)
            clauses.append(f"json_extract(metadata, '$.task_id') IN ({placeholders})")
            params.extend(task_values)
        if scope.scene_ids:
            scene_values = sorted(scope.scene_ids)
            scene_placeholders = _placeholders(scene_values)
            stream_ids = [f"stream-scene-{scene_id}" for scene_id in scene_values]
            stream_placeholders = _placeholders(stream_ids)
            clauses.append(f"stream_id IN ({stream_placeholders})")
            params.extend(stream_ids)
            clauses.append(f"json_extract(metadata, '$.scene_id') IN ({scene_placeholders})")
            params.extend(scene_values)
        if not clauses:
            return "0 = 1", []
        return "(" + " OR ".join(clauses) + ")", params

    def _count_communication_messages(self, db_path: Path, scope: CleanupScope) -> int:
        where_sql, params = self._communication_messages_where(scope)
        return _count_where(db_path, "communication_messages", where_sql, params)

    def _delete_communication_messages(self, db_path: Path, scope: CleanupScope) -> int:
        where_sql, params = self._communication_messages_where(scope)
        return _delete_where(db_path, "communication_messages", where_sql, params)

    def _terminal_history_where(self, scope: CleanupScope) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope.task_ids:
            task_values = sorted(scope.task_ids)
            task_placeholders = _placeholders(task_values)
            clauses.append(f"(owner_type = 'task' AND owner_id IN ({task_placeholders}))")
            params.extend(task_values)
            clauses.append(f"json_extract(metadata_json, '$.task_id') IN ({task_placeholders})")
            params.extend(task_values)
        if scope.scene_ids:
            scene_values = sorted(scope.scene_ids)
            scene_placeholders = _placeholders(scene_values)
            clauses.append(f"(owner_type = 'scene' AND owner_id IN ({scene_placeholders}))")
            params.extend(scene_values)
            clauses.append(f"json_extract(metadata_json, '$.scene_id') IN ({scene_placeholders})")
            params.extend(scene_values)
        if not clauses:
            return "0 = 1", []
        return "(" + " OR ".join(clauses) + ")", params

    def _count_terminal_history(self, db_path: Path, scope: CleanupScope) -> int:
        where_sql, params = self._terminal_history_where(scope)
        return _count_where(db_path, "terminals", where_sql, params)

    def _delete_terminal_history(self, db_path: Path, scope: CleanupScope) -> int:
        where_sql, params = self._terminal_history_where(scope)
        return _delete_where(db_path, "terminals", where_sql, params, foreign_keys=True)

    def _delete_context_compression_archive(self, archive_dir: Path, db_path: Path, scope: CleanupScope) -> int:
        if not db_path.exists() or not scope.task_ids:
            return 0
        task_values = sorted(scope.task_ids)
        placeholders = _placeholders(task_values)
        conn = _connect_existing(db_path)
        if conn is None:
            return 0
        try:
            rows = conn.execute(
                f"""
                SELECT job_id, final_summary_path, before_messages_snapshot_path, after_messages_snapshot_path
                FROM context_compression_jobs
                WHERE task_id IN ({placeholders})
                """,
                tuple(task_values),
            ).fetchall()
            for row in rows:
                for column_name in ("final_summary_path", "before_messages_snapshot_path", "after_messages_snapshot_path"):
                    _unlink_archive_file(archive_dir, row[column_name])
            result = conn.execute(
                f"DELETE FROM context_compression_jobs WHERE task_id IN ({placeholders})",
                tuple(task_values),
            )
            conn.commit()
            return int(result.rowcount or 0)
        finally:
            conn.close()


def _paths_from_config() -> LogsStorePaths:
    config = ConfigRegistry.instance()
    agent_runtime_dir = Path(config.agent_runtime.data_dir).expanduser()
    communication_dir = Path(config.communication.data_dir).expanduser()
    llm_http_trace_dir = Path(config.llm_http_trace.data_dir).expanduser()
    context_compression_dir = Path(config.context_compression.archive_data_dir).expanduser()
    terminal_dir = Path(config.terminal.data_dir).expanduser()
    return LogsStorePaths(
        agent_runtime_db=agent_runtime_dir / "agent_runtime.db",
        scene_db=Path(config.scene.scenes_db_path).expanduser(),
        communication_db=communication_dir / "communication.db",
        chat_bubbles_db=agent_runtime_dir / "chat_bubbles.db",
        llm_http_trace_db=llm_http_trace_dir / "llm_http_traces.db",
        context_compression_dir=context_compression_dir,
        context_compression_db=context_compression_dir / "context_compression.db",
        terminal_db=terminal_dir / "terminal.db",
    )


def _normalize_lifecycle_filters(lifecycle_filters: Iterable[str]) -> tuple[str, ...]:
    filters = tuple(dict.fromkeys(str(item) for item in lifecycle_filters if str(item).strip()))
    if not filters:
        raise ValueError("请选择至少一个生命周期过滤条件。")
    unsupported = sorted(set(filters) - SUPPORTED_LIFECYCLE_FILTERS)
    if unsupported:
        raise ValueError("不支持的生命周期过滤条件: " + ", ".join(unsupported))
    return filters


def _normalize_categories(categories: Iterable[str]) -> tuple[str, ...]:
    selected = tuple(dict.fromkeys(str(item) for item in categories if str(item).strip()))
    if not selected:
        raise ValueError("请选择至少一个日志清理类别。")
    unsupported = sorted(set(selected) - set(CATEGORY_LABELS))
    if unsupported:
        raise ValueError("不支持的日志清理类别: " + ", ".join(unsupported))
    return selected


def _connect_existing(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _count_rows(db_path: Path, table_name: str) -> int:
    conn = _connect_existing(db_path)
    if conn is None:
        return 0
    try:
        if not _table_exists(conn, table_name):
            return 0
        row = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
        return int(row[0] if row else 0)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _count_where(db_path: Path, table_name: str, where_sql: str, params: list[Any]) -> int:
    conn = _connect_existing(db_path)
    if conn is None:
        return 0
    try:
        if not _table_exists(conn, table_name):
            return 0
        row = conn.execute(f"SELECT COUNT(*) FROM {table_name} WHERE {where_sql}", tuple(params)).fetchone()
        return int(row[0] if row else 0)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _delete_where(
    db_path: Path,
    table_name: str,
    where_sql: str,
    params: list[Any],
    *,
    foreign_keys: bool = False,
 ) -> int:
    conn = _connect_existing(db_path)
    if conn is None:
        return 0
    try:
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON")
        if not _table_exists(conn, table_name):
            return 0
        result = conn.execute(f"DELETE FROM {table_name} WHERE {where_sql}", tuple(params))
        conn.commit()
        return int(result.rowcount or 0)
    finally:
        conn.close()


def _ended_task_ids(agent_runtime_db: Path) -> set[str]:
    conn = _connect_existing(agent_runtime_db)
    if conn is None:
        return set()
    try:
        if not _table_exists(conn, "agent_tasks"):
            return set()
        rows = conn.execute(
            "SELECT task_id FROM agent_tasks WHERE status IN ('completed', 'failed', 'cancelled')"
        ).fetchall()
        return {str(row[0]) for row in rows if row[0]}
    finally:
        conn.close()


def _task_ids_for_scenes(agent_runtime_db: Path, scene_ids: Iterable[str]) -> set[str]:
    scene_values = sorted({scene_id for scene_id in scene_ids if scene_id})
    if not scene_values:
        return set()
    conn = _connect_existing(agent_runtime_db)
    if conn is None:
        return set()
    try:
        columns = _table_columns(conn, "agent_tasks")
        if "scene_id" not in columns:
            return set()
        placeholders = _placeholders(scene_values)
        rows = conn.execute(
            f"SELECT task_id FROM agent_tasks WHERE scene_id IN ({placeholders})",
            tuple(scene_values),
        ).fetchall()
        return {str(row[0]) for row in rows if row[0]}
    finally:
        conn.close()


def _archived_scene_ids(scene_db: Path) -> set[str]:
    conn = _connect_existing(scene_db)
    if conn is None:
        return set()
    try:
        if not _table_exists(conn, "scenes"):
            return set()
        rows = conn.execute("SELECT scene_id FROM scenes WHERE status = 'archived'").fetchall()
        return {str(row[0]) for row in rows if row[0]}
    finally:
        conn.close()


def _task_only_where(scope: CleanupScope, task_column: str) -> tuple[str, list[Any]]:
    if not scope.task_ids:
        return "0 = 1", []
    values = sorted(scope.task_ids)
    return f"{task_column} IN ({_placeholders(values)})", list(values)


def _task_scene_where(
    scope: CleanupScope,
    *,
    task_column: str,
    scene_column: str,
 ) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if scope.task_ids:
        task_values = sorted(scope.task_ids)
        clauses.append(f"{task_column} IN ({_placeholders(task_values)})")
        params.extend(task_values)
    if scope.scene_ids:
        scene_values = sorted(scope.scene_ids)
        clauses.append(f"{scene_column} IN ({_placeholders(scene_values)})")
        params.extend(scene_values)
    if not clauses:
        return "0 = 1", []
    return "(" + " OR ".join(clauses) + ")", params


def _placeholders(values: Iterable[Any]) -> str:
    return ", ".join("?" for _ in values)


def _sqlite_store_size(db_path: Path) -> int:
    return sum(_file_size(path) for path in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")))


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.exists() and path.is_file() else 0
    except OSError:
        return 0


def _directory_size(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for filename in files:
            total += _file_size(Path(root) / filename)
    return total


def _unlink_archive_file(archive_dir: Path, raw_path: Any) -> None:
    if not raw_path:
        return
    try:
        path = Path(str(raw_path)).expanduser()
        archive_root = archive_dir.expanduser().resolve()
        resolved = path.resolve()
        resolved.relative_to(archive_root)
    except (OSError, ValueError):
        return
    try:
        if resolved.exists() and resolved.is_file():
            resolved.unlink()
    except OSError:
        return

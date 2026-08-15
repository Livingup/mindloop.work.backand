from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from agent_runtime.config import ConfigRegistry
from agent_runtime.infrastructure.sqlite_maintenance import (
    SafeSqliteStoreMaintenanceGateway,
    SqliteStoreMaintenanceGateway,
    measure_sqlite_store,
    sqlite_operation_lease,
 )


logger = logging.getLogger(__name__)


LIFECYCLE_ENDED_TASKS = "ended_tasks"
LIFECYCLE_ARCHIVED_SCENES = "archived_scenes"

CLEANUP_MODE_PREVIEW = "preview"
CLEANUP_MODE_EXECUTE = "execute"

SPACE_RELEASE_MODE_LOGICAL_ONLY = "logical_only"
SPACE_RELEASE_MODE_SAFE_PHYSICAL_RELEASE = "safe_physical_release"
SUPPORTED_SPACE_RELEASE_MODES: frozenset[str] = frozenset({
    SPACE_RELEASE_MODE_LOGICAL_ONLY,
    SPACE_RELEASE_MODE_SAFE_PHYSICAL_RELEASE,
})

CLEANUP_PHASE_RESOLVING_SCOPE = "resolving_scope"
CLEANUP_PHASE_DELETING_DATA = "deleting_data"
CLEANUP_PHASE_RELEASING_SPACE = "releasing_space"
CLEANUP_PHASE_COMPLETED = "completed"
CLEANUP_PHASE_FAILED = "failed"

CLEANUP_JOB_STATUS_QUEUED = "queued"
CLEANUP_JOB_STATUS_RUNNING = "running"
CLEANUP_JOB_STATUS_SUCCEEDED = "succeeded"
CLEANUP_JOB_STATUS_FAILED = "failed"

SQLITE_LOCK_RETRY_ATTEMPTS = 3
SQLITE_LOCK_RETRY_DELAY_SECONDS = 0.2

CATEGORY_COMMUNICATION_MESSAGES = "communication_messages"
CATEGORY_CHAT_BUBBLES = "chat_bubbles"
CATEGORY_LLM_HTTP_TRACE = "llm_http_trace"
CATEGORY_CONTEXT_COMPRESSION_ARCHIVE = "context_compression_archive"
CATEGORY_TERMINAL_HISTORY = "terminal_history"

SUPPORTED_LIFECYCLE_FILTERS: frozenset[str] = frozenset({
    LIFECYCLE_ENDED_TASKS,
    LIFECYCLE_ARCHIVED_SCENES,
})

CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_COMMUNICATION_MESSAGES: "Communication messages",
    CATEGORY_CHAT_BUBBLES: "Chat Bubbles",
    CATEGORY_LLM_HTTP_TRACE: "LLM HTTP Trace",
    CATEGORY_CONTEXT_COMPRESSION_ARCHIVE: "Context Compression archive",
    CATEGORY_TERMINAL_HISTORY: "Terminal history",
}

MAINTENANCE_CATEGORY_ORDER: tuple[str, ...] = (
    CATEGORY_COMMUNICATION_MESSAGES,
    CATEGORY_CHAT_BUBBLES,
    CATEGORY_LLM_HTTP_TRACE,
    CATEGORY_CONTEXT_COMPRESSION_ARCHIVE,
    CATEGORY_TERMINAL_HISTORY,
 )

_UNSET = object()


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


@dataclass(frozen=True, slots=True)
class CleanupJobState:
    job_id: str
    mode: str
    status: str
    space_release_mode: str
    phase: str
    active_store_id: str | None
    lifecycle_filters: tuple[str, ...]
    categories: tuple[str, ...]
    release_only: bool
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "mode": self.mode,
            "status": self.status,
            "space_release_mode": self.space_release_mode,
            "phase": self.phase,
            "active_store_id": self.active_store_id,
            "release_only": self.release_only,
            "lifecycle_filters": list(self.lifecycle_filters),
            "categories": list(self.categories),
            "created_at": _datetime_to_iso(self.created_at),
            "updated_at": _datetime_to_iso(self.updated_at),
            "started_at": _datetime_to_iso(self.started_at),
            "completed_at": _datetime_to_iso(self.completed_at),
            "result": self.result,
            "error_message": self.error_message,
        }


class LogsManagementService:
    """Logs 工作区应用服务。

    该服务只面向日志工作区提供两类能力：实时存储大小总览，以及用户显式触发的
    异步 cleanup job。cleanup job 按生命周期过滤条件的 OR/union 语义解析范围，
    并通过进程内串行锁执行预览/删除，避免长时间占用 HTTP 请求和并发清理冲突。
    它直接检查各 SQLite / 文件系统存储的当前状态，不依赖 system-settings 的
    persistence_inventory 元数据来计算大小或删除范围。
    """

    def __init__(
        self,
        paths_accessor: Callable[[], LogsStorePaths] | None = None,
        maintenance_gateway: SqliteStoreMaintenanceGateway | None = None,
    ) -> None:
        self._paths_accessor = paths_accessor or _paths_from_config
        self._maintenance_gateway = maintenance_gateway or SafeSqliteStoreMaintenanceGateway()
        self._jobs_lock = threading.RLock()
        self._cleanup_lock = threading.Lock()
        self._cleanup_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="logs-cleanup-maintenance")
        self._jobs: dict[str, CleanupJobState] = {}

    def create_cleanup_job(
        self,
        *,
        mode: str,
        lifecycle_filters: Iterable[str],
        categories: Iterable[str],
        space_release_mode: str = SPACE_RELEASE_MODE_LOGICAL_ONLY,
        release_only: bool = False,
    ) -> dict[str, Any]:
        normalized_mode = _normalize_cleanup_mode(mode)
        normalized_lifecycle = _normalize_lifecycle_filters(lifecycle_filters)
        normalized_categories = _normalize_categories(categories)
        normalized_space_release_mode = _normalize_space_release_mode(space_release_mode)
        if release_only and (
            normalized_mode != CLEANUP_MODE_EXECUTE
            or normalized_space_release_mode != SPACE_RELEASE_MODE_SAFE_PHYSICAL_RELEASE
        ):
            raise ValueError("仅空间释放重试必须使用 execute + safe_physical_release。")
        now = _utc_now()
        job = CleanupJobState(
            job_id=uuid.uuid4().hex,
            mode=normalized_mode,
            status=CLEANUP_JOB_STATUS_QUEUED,
            space_release_mode=normalized_space_release_mode,
            phase=CLEANUP_PHASE_RESOLVING_SCOPE,
            active_store_id=None,
            lifecycle_filters=normalized_lifecycle,
            categories=normalized_categories,
            created_at=now,
            updated_at=now,
            release_only=release_only,
        )
        with self._jobs_lock:
            self._jobs[job.job_id] = job
        self._cleanup_executor.submit(self._run_cleanup_job, job.job_id)
        return job.to_dict()

    def close(self) -> None:
        self._cleanup_executor.shutdown(wait=False, cancel_futures=False)

    def get_cleanup_job(self, job_id: str) -> dict[str, Any] | None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job is not None else None

    def list_cleanup_jobs(
        self,
        *,
        limit: int = 10,
        active_only: bool = False,
    ) -> dict[str, Any]:
        normalized_limit = max(1, min(int(limit), 50))
        active_statuses = {CLEANUP_JOB_STATUS_QUEUED, CLEANUP_JOB_STATUS_RUNNING}
        with self._jobs_lock:
            jobs = [
                job
                for job in self._jobs.values()
                if job.mode == CLEANUP_MODE_EXECUTE and job.status in active_statuses
            ]
        jobs.sort(key=lambda job: job.created_at, reverse=True)
        return {
            "items": [job.to_dict() for job in jobs[:normalized_limit]],
            "active_count": len(jobs),
            "volatile": True,
            "retention_note": "只返回当前后端进程内未完成的 execute 删除任务；预览任务和已完成删除任务不会出现在恢复列表中，后端重启后未完成任务状态不会保留。",
        }

    def _run_cleanup_job(self, job_id: str) -> None:
        with self._cleanup_lock:
            self._update_job(
                job_id,
                status=CLEANUP_JOB_STATUS_RUNNING,
                phase=CLEANUP_PHASE_RESOLVING_SCOPE,
                active_store_id=None,
                started_at=_utc_now(),
            )
            try:
                job = self._get_job_state(job_id)
                result = self._build_cleanup_result(
                    lifecycle_filters=job.lifecycle_filters,
                    categories=job.categories,
                    execute=job.mode == CLEANUP_MODE_EXECUTE,
                    space_release_mode=job.space_release_mode,
                    release_only=job.release_only,
                    progress_callback=lambda phase, store_id=None: self._update_job(
                        job_id,
                        phase=phase,
                        active_store_id=store_id,
                    ),
                )
                self._update_job(
                    job_id,
                    status=CLEANUP_JOB_STATUS_SUCCEEDED,
                    phase=CLEANUP_PHASE_COMPLETED,
                    active_store_id=None,
                    completed_at=_utc_now(),
                    result=result,
                )
            except Exception:
                logger.exception("日志清理任务执行失败: job_id=%s", job_id)
                self._update_job(
                    job_id,
                    status=CLEANUP_JOB_STATUS_FAILED,
                    phase=CLEANUP_PHASE_FAILED,
                    active_store_id=None,
                    completed_at=_utc_now(),
                    error_message="日志清理任务未完成；请稍后重试或查看各日志库的安全维护结果。",
                )

    def _get_job_state(self, job_id: str) -> CleanupJobState:
        with self._jobs_lock:
            job = self._jobs[job_id]
            return job

    def _update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        phase: str | None = None,
        active_store_id: str | None | object = _UNSET,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._jobs_lock:
            current = self._jobs[job_id]
            self._jobs[job_id] = CleanupJobState(
                job_id=current.job_id,
                mode=current.mode,
                status=status or current.status,
                space_release_mode=current.space_release_mode,
                phase=phase if phase is not None else current.phase,
                active_store_id=(
                    current.active_store_id
                    if active_store_id is _UNSET
                    else active_store_id
                ),
                lifecycle_filters=current.lifecycle_filters,
                categories=current.categories,
                release_only=current.release_only,
                created_at=current.created_at,
                updated_at=_utc_now(),
                started_at=started_at if started_at is not None else current.started_at,
                completed_at=completed_at if completed_at is not None else current.completed_at,
                result=result if result is not None else current.result,
                error_message=error_message if error_message is not None else current.error_message,
            )

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "supported_space_release_modes": sorted(SUPPORTED_SPACE_RELEASE_MODES),
            "release_only_retry_supported": True,
        }

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
        space_release_mode: str = SPACE_RELEASE_MODE_LOGICAL_ONLY,
    ) -> dict[str, Any]:
        with self._cleanup_lock:
            return self._build_cleanup_result(
                lifecycle_filters=tuple(lifecycle_filters),
                categories=tuple(categories),
                execute=False,
                space_release_mode=space_release_mode,
            )

    def execute_cleanup(
        self,
        *,
        lifecycle_filters: Iterable[str],
        categories: Iterable[str],
        space_release_mode: str = SPACE_RELEASE_MODE_LOGICAL_ONLY,
        release_only: bool = False,
    ) -> dict[str, Any]:
        with self._cleanup_lock:
            return self._build_cleanup_result(
                lifecycle_filters=tuple(lifecycle_filters),
                categories=tuple(categories),
                execute=True,
                space_release_mode=space_release_mode,
                release_only=release_only,
            )

    def _build_cleanup_result(
        self,
        *,
        lifecycle_filters: tuple[str, ...],
        categories: tuple[str, ...],
        execute: bool,
        space_release_mode: str = SPACE_RELEASE_MODE_LOGICAL_ONLY,
        release_only: bool = False,
        progress_callback: Callable[[str, str | None], None] | None = None,
    ) -> dict[str, Any]:
        selected_categories = _normalize_categories(categories)
        normalized_space_release_mode = _normalize_space_release_mode(space_release_mode)
        if release_only and (
            not execute or normalized_space_release_mode != SPACE_RELEASE_MODE_SAFE_PHYSICAL_RELEASE
        ):
            raise ValueError("仅空间释放重试必须使用 execute + safe_physical_release。")
        if progress_callback is not None:
            progress_callback(CLEANUP_PHASE_RESOLVING_SCOPE, None)
        scope = self._resolve_scope(lifecycle_filters)
        paths = self._paths_accessor()
        if execute and not release_only and progress_callback is not None:
            progress_callback(CLEANUP_PHASE_DELETING_DATA, None)
        category_results = [
            self._category_result(
                paths,
                scope,
                category,
                execute=execute and not release_only,
            )
            for category in selected_categories
        ]
        if normalized_space_release_mode == SPACE_RELEASE_MODE_LOGICAL_ONLY:
            space_release = _logical_only_space_release_result()
        elif not execute:
            space_release = self._preview_space_release(paths, selected_categories)
        else:
            space_release = self._execute_space_release(
                paths,
                selected_categories,
                progress_callback=progress_callback,
            )
        return {
            "mode": "execute" if execute else "preview",
            "space_release_mode": normalized_space_release_mode,
            "release_only": release_only,
            "lifecycle_filters": list(scope.lifecycle_filters),
            "categories": list(selected_categories),
            "scope": {
                "task_count": len(scope.task_ids),
                "scene_count": len(scope.scene_ids),
            },
            "category_results": category_results,
            "total_matched_count": sum(int(item["matched_count"]) for item in category_results),
            "total_deleted_count": sum(int(item["deleted_count"]) for item in category_results),
            "space_release": space_release,
        }

    def _preview_space_release(
        self,
        paths: LogsStorePaths,
        selected_categories: tuple[str, ...],
    ) -> dict[str, Any]:
        stores: list[dict[str, Any]] = []
        for category in MAINTENANCE_CATEGORY_ORDER:
            if category not in selected_categories:
                continue
            db_path = _category_db_path(paths, category)
            measurement = measure_sqlite_store(db_path)
            stores.append({
                "store_id": category,
                "eligible": db_path.exists(),
                "status": "preview_only" if db_path.exists() else "not_available",
                "vacuum_started": False,
                "required_working_bytes": None,
                "available_bytes": None,
                "page_size_bytes": None,
                "page_count": None,
                "freelist_count": None,
                "before_size_bytes": measurement.total_size_bytes,
                "after_size_bytes": None,
                "reclaimed_size_bytes": 0,
                "net_size_change_bytes": 0,
                "before_db_size_bytes": measurement.db_size_bytes,
                "after_db_size_bytes": None,
                "before_wal_size_bytes": measurement.wal_size_bytes,
                "after_wal_size_bytes": None,
                "before_shm_size_bytes": measurement.shm_size_bytes,
                "after_shm_size_bytes": None,
                "error_code": None if db_path.exists() else "database_not_available",
                "message": (
                    "执行阶段将尝试安全 checkpoint + VACUUM；实际释放量以完成后测量为准。"
                    if db_path.exists()
                    else "数据库文件当前不存在。"
                ),
            })
        eligible = [item for item in stores if item["eligible"]]
        return {
            "requested_mode": SPACE_RELEASE_MODE_SAFE_PHYSICAL_RELEASE,
            "status": "preview_only",
            "eligible_store_count": len(eligible),
            "before_total_size_bytes": sum(int(item["before_size_bytes"]) for item in eligible),
            "after_total_size_bytes": None,
            "reclaimed_size_bytes": 0,
            "net_size_change_bytes": 0,
            "stores": stores,
            "warnings": ["预览不执行 DELETE、checkpoint 或 VACUUM，也不估算可释放字节。"],
        }

    def _execute_space_release(
        self,
        paths: LogsStorePaths,
        selected_categories: tuple[str, ...],
        *,
        progress_callback: Callable[[str, str | None], None] | None,
    ) -> dict[str, Any]:
        stores: list[dict[str, Any]] = []
        for category in MAINTENANCE_CATEGORY_ORDER:
            if category not in selected_categories:
                continue
            if progress_callback is not None:
                progress_callback(CLEANUP_PHASE_RELEASING_SPACE, category)
            stores.append(
                self._maintenance_gateway.maintain_store(
                    store_id=category,
                    db_path=_category_db_path(paths, category),
                )
            )
        if progress_callback is not None:
            progress_callback(CLEANUP_PHASE_RELEASING_SPACE, None)
        return _aggregate_space_release_results(stores)

    def _resolve_scope(self, lifecycle_filters: tuple[str, ...]) -> CleanupScope:
        filters = _normalize_lifecycle_filters(lifecycle_filters)
        paths = self._paths_accessor()
        task_ids: set[str] = set()
        scene_ids: set[str] = set()

        if LIFECYCLE_ENDED_TASKS in filters:
            task_ids.update(_ended_task_ids(paths.agent_runtime_db))

        if LIFECYCLE_ARCHIVED_SCENES in filters:
            archived_scene_ids = _archived_scene_ids(paths.scene_db)
            scene_ids.update(archived_scene_ids)
            task_ids.update(_task_ids_for_scenes(paths.agent_runtime_db, archived_scene_ids))

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
        archive_files_deleted_count = 0
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
            if execute:
                deleted, archive_files_deleted_count = self._delete_context_compression_archive(
                    paths.context_compression_dir,
                    paths.context_compression_db,
                    scope,
                )
            else:
                deleted = 0
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
            "archive_files_deleted_count": archive_files_deleted_count,
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
            "size_bytes": (
                _sqlite_store_size(db_path)
                + _directory_size(archive_dir / "histories")
                + _directory_size(archive_dir / "snapshots")
                + _directory_size(archive_dir / "summaries")
            ),
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

    def _delete_context_compression_archive(self, archive_dir: Path, db_path: Path, scope: CleanupScope) -> tuple[int, int]:
        if not db_path.exists() or not scope.task_ids:
            return 0, 0
        task_values = sorted(scope.task_ids)
        placeholders = _placeholders(task_values)

        path_columns = (
            "final_summary_path",
            "before_messages_snapshot_path",
            "after_messages_snapshot_path",
            "native_history_path",
            "final_markdown_path",
        )

        def read_rows() -> list[tuple[Any, ...]]:
            with sqlite_operation_lease(db_path):
                conn = _connect_existing(db_path)
                if conn is None:
                    return []
                try:
                    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(context_compression_jobs)").fetchall()}
                    if "job_id" not in existing:
                        return []
                    selected = ["job_id"] + [column for column in path_columns if column in existing]
                    select_sql = ", ".join(selected)
                    raw_rows = list(
                        conn.execute(
                            f"""
                            SELECT {select_sql}
                            FROM context_compression_jobs
                            WHERE task_id IN ({placeholders})
                            """,
                            tuple(task_values),
                        ).fetchall()
                    )
                    # 统一成 (job_id, path1, path2, ...) 供后续 unlink
                    return [tuple(row) for row in raw_rows]
                finally:
                    conn.close()

        with sqlite_operation_lease(db_path):
            rows = _with_sqlite_retry(read_rows)

            def delete_rows() -> int:
                with sqlite_operation_lease(db_path):
                    conn = _connect_existing(db_path)
                    if conn is None:
                        return 0
                    try:
                        result = conn.execute(
                            f"DELETE FROM context_compression_jobs WHERE task_id IN ({placeholders})",
                            tuple(task_values),
                        )
                        conn.commit()
                        return int(result.rowcount or 0)
                    finally:
                        conn.close()

            deleted_count = _with_sqlite_retry(delete_rows)
            archive_files_deleted_count = 0
            if deleted_count > 0:
                for row in rows:
                    for value in row[1:]:
                        archive_files_deleted_count += int(_unlink_archive_file(archive_dir, value))
            return deleted_count, archive_files_deleted_count


def _category_db_path(paths: LogsStorePaths, category: str) -> Path:
    mapping = {
        CATEGORY_COMMUNICATION_MESSAGES: paths.communication_db,
        CATEGORY_CHAT_BUBBLES: paths.chat_bubbles_db,
        CATEGORY_LLM_HTTP_TRACE: paths.llm_http_trace_db,
        CATEGORY_CONTEXT_COMPRESSION_ARCHIVE: paths.context_compression_db,
        CATEGORY_TERMINAL_HISTORY: paths.terminal_db,
    }
    try:
        return mapping[category]
    except KeyError as exc:
        raise ValueError(f"不支持的日志清理类别: {category}") from exc


def _logical_only_space_release_result() -> dict[str, Any]:
    return {
        "requested_mode": SPACE_RELEASE_MODE_LOGICAL_ONLY,
        "status": "not_requested",
        "eligible_store_count": 0,
        "before_total_size_bytes": 0,
        "after_total_size_bytes": 0,
        "reclaimed_size_bytes": 0,
        "net_size_change_bytes": 0,
        "stores": [],
        "warnings": ["SQLite 文件未执行 checkpoint/VACUUM；已删除行形成的空闲页可供后续写入复用。"],
    }


def _aggregate_space_release_results(stores: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [item for item in stores if item.get("eligible")]
    successful_statuses = {"released", "skipped_no_changes"}
    deferred_statuses = {"busy", "timeout"}
    failed_statuses = {"insufficient_disk_space", "permission_denied", "io_error", "failed"}
    statuses = {str(item.get("status")) for item in eligible}
    if not eligible:
        status = "skipped_no_changes"
    elif statuses <= {"skipped_no_changes"}:
        status = "skipped_no_changes"
    elif statuses <= successful_statuses:
        status = "completed"
    elif statuses and statuses <= deferred_statuses:
        status = "deferred_busy"
    elif statuses & successful_statuses:
        status = "partial"
    elif statuses & failed_statuses:
        status = "failed"
    else:
        status = "partial"
    warnings = [
        f"{CATEGORY_LABELS.get(str(item.get('store_id')), str(item.get('store_id')))}：{item.get('message')}"
        for item in stores
        if item.get("status") not in successful_statuses and item.get("message")
    ]
    return {
        "requested_mode": SPACE_RELEASE_MODE_SAFE_PHYSICAL_RELEASE,
        "status": status,
        "eligible_store_count": len(eligible),
        "before_total_size_bytes": sum(int(item.get("before_size_bytes") or 0) for item in eligible),
        "after_total_size_bytes": sum(int(item.get("after_size_bytes") or 0) for item in eligible),
        "reclaimed_size_bytes": sum(int(item.get("reclaimed_size_bytes") or 0) for item in eligible),
        "net_size_change_bytes": sum(int(item.get("net_size_change_bytes") or 0) for item in eligible),
        "stores": stores,
        "warnings": warnings,
    }


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


def _normalize_cleanup_mode(mode: str) -> str:
    normalized = str(mode).strip()
    if normalized not in {CLEANUP_MODE_PREVIEW, CLEANUP_MODE_EXECUTE}:
        raise ValueError(f"不支持的日志清理任务模式: {mode}")
    return normalized


def _normalize_space_release_mode(mode: str | None) -> str:
    normalized = str(mode or SPACE_RELEASE_MODE_LOGICAL_ONLY).strip()
    if normalized not in SUPPORTED_SPACE_RELEASE_MODES:
        raise ValueError(f"不支持的数据删除空间释放模式: {mode}")
    return normalized


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _datetime_to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _is_sqlite_locked(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _with_sqlite_retry(operation: Callable[[], Any]) -> Any:
    for attempt in range(SQLITE_LOCK_RETRY_ATTEMPTS):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_locked(exc) or attempt >= SQLITE_LOCK_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(SQLITE_LOCK_RETRY_DELAY_SECONDS * (attempt + 1))
    raise RuntimeError("SQLite 操作重试失败。")


def _connect_existing(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
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
    def operation() -> int:
        with sqlite_operation_lease(db_path):
            conn = _connect_existing(db_path)
            if conn is None:
                return 0
            try:
                if not _table_exists(conn, table_name):
                    return 0
                row = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
                return int(row[0] if row else 0)
            except sqlite3.OperationalError as exc:
                if _is_sqlite_locked(exc):
                    raise
                return 0
            except sqlite3.Error:
                return 0
            finally:
                conn.close()

    return _with_sqlite_retry(operation)


def _count_where(db_path: Path, table_name: str, where_sql: str, params: list[Any]) -> int:
    def operation() -> int:
        with sqlite_operation_lease(db_path):
            conn = _connect_existing(db_path)
            if conn is None:
                return 0
            try:
                if not _table_exists(conn, table_name):
                    return 0
                row = conn.execute(f"SELECT COUNT(*) FROM {table_name} WHERE {where_sql}", tuple(params)).fetchone()
                return int(row[0] if row else 0)
            except sqlite3.OperationalError as exc:
                if _is_sqlite_locked(exc):
                    raise
                return 0
            except sqlite3.Error:
                return 0
            finally:
                conn.close()

    return _with_sqlite_retry(operation)


def _delete_where(
    db_path: Path,
    table_name: str,
    where_sql: str,
    params: list[Any],
    *,
    foreign_keys: bool = False,
 ) -> int:
    def operation() -> int:
        with sqlite_operation_lease(db_path):
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

    return _with_sqlite_retry(operation)


def _ended_task_ids(agent_runtime_db: Path) -> set[str]:
    def operation() -> set[str]:
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

    return _with_sqlite_retry(operation)


def _task_ids_for_scenes(agent_runtime_db: Path, scene_ids: Iterable[str]) -> set[str]:
    scene_values = sorted({scene_id for scene_id in scene_ids if scene_id})
    if not scene_values:
        return set()

    def operation() -> set[str]:
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

    return _with_sqlite_retry(operation)


def _archived_scene_ids(scene_db: Path) -> set[str]:
    def operation() -> set[str]:
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

    return _with_sqlite_retry(operation)


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


def _unlink_archive_file(archive_dir: Path, raw_path: Any) -> bool:
    if not raw_path:
        return False
    try:
        path = Path(str(raw_path)).expanduser()
        archive_root = archive_dir.expanduser().resolve()
        resolved = path.resolve()
        resolved.relative_to(archive_root)
    except (OSError, ValueError):
        return False
    try:
        if resolved.exists() and resolved.is_file():
            resolved.unlink()
            return True
    except OSError:
        return False
    return False

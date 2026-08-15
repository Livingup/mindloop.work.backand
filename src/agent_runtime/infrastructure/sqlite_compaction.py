from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from agent_runtime.infrastructure.sqlite_maintenance import (
    SQLITE_MAINTENANCE_BUSY_TIMEOUT_MS,
    SQLITE_MAINTENANCE_LEASE_TIMEOUT_SECONDS,
    SqliteMaintenanceCoordinator,
    SqliteStoreMeasurement,
    get_sqlite_maintenance_coordinator,
    measure_sqlite_store,
    try_acquire_runtime_presence_exclusive,
 )

logger = logging.getLogger(__name__)

COMPACTION_MANIFEST_VERSION = 1
COMPACTION_STAGING_DIRECTORY = ".spread-ai-compaction"
COMPACTION_MANIFEST_PREFIX = ".spread-ai-compaction-"
COMPACTION_MANIFEST_SUFFIX = ".json"
COMPACTION_STATUS_REQUIRES_RUNTIME_RESTART = "requires_runtime_restart"
COMPACTION_STATUS_REQUIRES_MORE_SPACE = "requires_more_space"
COMPACTION_STATUS_COMPLETED = "completed"
COMPACTION_ERROR_RUNTIME_PRESENCE_ACTIVE = "runtime_presence_active"
COMPACTION_ERROR_COMPACT_TARGET_SPACE_INSUFFICIENT = "compact_target_space_insufficient"
COMPACTION_ERROR_SOURCE_WAL_NOT_DRAINED = "source_wal_not_drained"
COMPACTION_ERROR_COMPACT_COPY_FAILED = "compact_copy_failed"
COMPACTION_ERROR_COMPACT_VALIDATION_FAILED = "compact_validation_failed"
COMPACTION_ERROR_COMPACT_SWAP_FAILED = "compact_swap_failed"
COMPACTION_ERROR_SOURCE_NOT_AVAILABLE = "database_not_available"
COMPACTION_ERROR_MANIFEST_IDENTITY_MISMATCH = "compaction_manifest_identity_mismatch"
COMPACTION_ERROR_MANIFEST_EPOCH_INVALID = "compaction_manifest_epoch_invalid"


@dataclass(frozen=True, slots=True)
class SqliteCompactionDiagnostic:
    """Non-secret diagnostics persisted with a pending offline compaction."""

    measurement: SqliteStoreMeasurement
    page_size_bytes: int | None
    page_count: int | None
    freelist_count: int | None
    auto_vacuum_mode: str | None
    freelist_potential_bytes: int | None
    compact_target_size_bytes: int | None
    available_bytes: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "before_size_bytes": self.measurement.total_size_bytes,
            "before_db_size_bytes": self.measurement.db_size_bytes,
            "before_wal_size_bytes": self.measurement.wal_size_bytes,
            "before_shm_size_bytes": self.measurement.shm_size_bytes,
            "page_size_bytes": self.page_size_bytes,
            "page_count": self.page_count,
            "freelist_count": self.freelist_count,
            "auto_vacuum_mode": self.auto_vacuum_mode,
            "freelist_potential_bytes": self.freelist_potential_bytes,
            "compact_target_size_bytes": self.compact_target_size_bytes,
            "available_bytes": self.available_bytes,
        }


def compaction_manifest_path(db_path: Path) -> Path:
    """Return the managed manifest path; callers never provide this path."""
    return db_path.expanduser().resolve().parent / (
        f"{COMPACTION_MANIFEST_PREFIX}{db_path.name}{COMPACTION_MANIFEST_SUFFIX}"
    )


def compaction_staging_root(db_path: Path) -> Path:
    return db_path.expanduser().resolve().parent / COMPACTION_STAGING_DIRECTORY


def database_identity(db_path: Path) -> str:
    canonical = str(db_path.expanduser().resolve()).casefold().encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _managed_epoch(value: Any, *, default: str | None = "") -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value, flags=re.IGNORECASE):
        return value.lower()
    if default is None:
        return None
    return uuid.uuid4().hex


def _manifest_matches_db(payload: dict[str, Any], db_path: Path) -> bool:
    raw_path = payload.get("db_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return False
    try:
        manifest_path = Path(raw_path).expanduser().resolve()
        controlled_path = db_path.expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return (
        str(manifest_path).casefold() == str(controlled_path).casefold()
        and payload.get("db_identity") == database_identity(controlled_path)
    )


def read_sqlite_compaction_diagnostic(db_path: Path) -> SqliteCompactionDiagnostic:
    measurement = measure_sqlite_store(db_path)
    page_size: int | None = None
    page_count: int | None = None
    freelist_count: int | None = None
    auto_vacuum_mode: str | None = None
    if db_path.exists():
        try:
            uri = db_path.expanduser().resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=SQLITE_MAINTENANCE_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            try:
                page_size = _pragma_int(conn, "page_size")
                page_count = _pragma_int(conn, "page_count")
                freelist_count = _pragma_int(conn, "freelist_count")
                auto_vacuum_value = _pragma_int(conn, "auto_vacuum")
                auto_vacuum_mode = _auto_vacuum_mode(auto_vacuum_value)
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            logger.debug("读取 SQLite 压缩诊断失败: db=%s", db_path, exc_info=True)
    freelist_potential = (
        freelist_count * page_size
        if freelist_count is not None and page_size is not None
        else None
    )
    compact_target = _estimate_compact_target_bytes(
        page_size_bytes=page_size,
        page_count=page_count,
        freelist_count=freelist_count,
        measurement=measurement,
    )
    available = _available_bytes(compaction_staging_root(db_path))
    return SqliteCompactionDiagnostic(
        measurement=measurement,
        page_size_bytes=page_size,
        page_count=page_count,
        freelist_count=freelist_count,
        auto_vacuum_mode=auto_vacuum_mode,
        freelist_potential_bytes=freelist_potential,
        compact_target_size_bytes=compact_target,
        available_bytes=available,
    )


def queue_pending_sqlite_compaction(
    *,
    store_id: str,
    db_path: Path,
    categories: Iterable[str],
    diagnostic: SqliteCompactionDiagnostic | None = None,
    error_code: str = COMPACTION_ERROR_COMPACT_TARGET_SPACE_INSUFFICIENT,
    message: str = "已提交逻辑删除；物理压缩等待 Runtime 重启后在离线维护窗口执行。",
 ) -> dict[str, Any]:
    """Persist a managed, additive retry record without accepting user paths."""
    resolved_db_path = db_path.expanduser().resolve()
    diagnostic = diagnostic or read_sqlite_compaction_diagnostic(resolved_db_path)
    manifest_path = compaction_manifest_path(resolved_db_path)
    previous = _read_manifest(manifest_path) or {}
    epoch = _managed_epoch(previous.get("epoch"))
    payload: dict[str, Any] = {
        "version": COMPACTION_MANIFEST_VERSION,
        "epoch": epoch,
        "store_id": str(store_id),
        "db_path": str(resolved_db_path),
        "db_identity": database_identity(resolved_db_path),
        "categories": sorted({str(category) for category in categories}),
        "created_at": str(previous.get("created_at") or _utc_iso()),
        "updated_at": _utc_iso(),
        "attempts": int(previous.get("attempts") or 0),
        "status": COMPACTION_STATUS_REQUIRES_RUNTIME_RESTART,
        "error_code": error_code,
        "message": _safe_manifest_message(message, error_code),
        "diagnostic": diagnostic.to_dict(),
    }
    _write_manifest(manifest_path, payload)
    return _pending_result(
        payload,
        db_path=resolved_db_path,
        diagnostic=diagnostic,
        status=COMPACTION_STATUS_REQUIRES_RUNTIME_RESTART,
        error_code=error_code,
        message=message,
    )


def pending_sqlite_compaction_summaries(db_paths: Iterable[Path]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for db_path in sorted({Path(path).expanduser().resolve() for path in db_paths}, key=lambda path: str(path).casefold()):
        payload = _read_manifest(compaction_manifest_path(db_path))
        if payload is None:
            continue
        summaries.append(_public_manifest_summary(payload))
    return summaries


def run_pending_sqlite_compactions(
    db_paths: Iterable[Path],
    *,
    coordinator: SqliteMaintenanceCoordinator | None = None,
    timeout_seconds: float = SQLITE_MAINTENANCE_LEASE_TIMEOUT_SECONDS,
 ) -> list[dict[str, Any]]:
    """Run pending compactions before this worker opens controlled repositories.

    The runtime-presence exclusive lock is the hard offline boundary: every worker
    acquires the matching shared lease for its lifetime, so an idle long-lived DB
    connection still blocks this runner until all cooperating workers have exited.
    """
    results: list[dict[str, Any]] = []
    coordinator = coordinator or get_sqlite_maintenance_coordinator()
    for db_path in sorted({Path(path).expanduser().resolve() for path in db_paths}, key=lambda path: str(path).casefold()):
        manifest_path = compaction_manifest_path(db_path)
        payload = _read_manifest(manifest_path)
        if payload is None:
            continue
        results.append(
            _run_one_pending_compaction(
                db_path,
                payload,
                coordinator=coordinator,
                timeout_seconds=timeout_seconds,
            )
        )
    return results


def _run_one_pending_compaction(
    db_path: Path,
    payload: dict[str, Any],
    *,
    coordinator: SqliteMaintenanceCoordinator,
    timeout_seconds: float,
 ) -> dict[str, Any]:
    if not _manifest_matches_db(payload, db_path):
        return _update_pending_failure(
            db_path,
            payload,
            error_code=COMPACTION_ERROR_MANIFEST_IDENTITY_MISMATCH,
            message="压缩 manifest 与受控数据库身份不一致，已安全保留待处理状态。",
        )
    epoch = _managed_epoch(payload.get("epoch"), default=None)
    if epoch is None:
        return _update_pending_failure(
            db_path,
            payload,
            error_code=COMPACTION_ERROR_MANIFEST_EPOCH_INVALID,
            message="压缩 manifest epoch 无效，已安全保留待处理状态且未构造临时路径。",
        )
    if not db_path.exists():
        return _update_pending_failure(
            db_path,
            payload,
            error_code=COMPACTION_ERROR_SOURCE_NOT_AVAILABLE,
            message="受控日志数据库不存在，未执行离线压缩。",
        )
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    presence_lease = try_acquire_runtime_presence_exclusive(db_path, deadline=deadline)
    if presence_lease is None:
        return _update_pending_failure(
            db_path,
            payload,
            error_code=COMPACTION_ERROR_RUNTIME_PRESENCE_ACTIVE,
            message="仍有 Runtime worker 持有受控数据库连接；请停止全部 Runtime worker 后重启，当前未执行替换。",
        )
    try:
        with coordinator.maintenance_lease(db_path, timeout_seconds=max(deadline - time.monotonic(), 0.0)) as acquired:
            if not acquired:
                return _update_pending_failure(
                    db_path,
                    payload,
                    error_code="maintenance_lease_timeout",
                    message="离线压缩未取得 SQLite 独占维护窗口，当前未执行替换。",
                )
            return _compact_copy_and_swap(db_path, payload)
    finally:
        presence_lease.release()


def _compact_copy_and_swap(db_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    diagnostic = read_sqlite_compaction_diagnostic(db_path)
    staging_root = compaction_staging_root(db_path)
    try:
        staging_root.mkdir(parents=True, exist_ok=True)
        available = _available_bytes(staging_root)
        required_target = diagnostic.compact_target_size_bytes
        if required_target is not None and available is not None and available < required_target:
            diagnostic = _replace_diagnostic(diagnostic, available_bytes=available)
            return _update_pending_failure(
                db_path,
                payload,
                error_code=COMPACTION_ERROR_COMPACT_TARGET_SPACE_INSUFFICIENT,
                message=(
                    "紧凑副本所需源卷空间不足，未启动 VACUUM INTO；"
                    f"预计至少需要 {required_target} 字节，当前可用约 {available} 字节。"
                ),
                diagnostic=diagnostic,
                status=COMPACTION_STATUS_REQUIRES_MORE_SPACE,
            )
    except OSError as exc:
        return _update_pending_failure(
            db_path,
            payload,
            error_code=COMPACTION_ERROR_COMPACT_TARGET_SPACE_INSUFFICIENT,
            message="无法创建 Runtime 管理的压缩 staging 目录，未启动 VACUUM INTO。",
            diagnostic=diagnostic,
            status=COMPACTION_STATUS_REQUIRES_MORE_SPACE,
            exception=exc,
        )

    epoch = _managed_epoch(payload.get("epoch"))
    target = staging_root / f"{db_path.name}.{epoch}.tmp"
    backup = db_path.parent / f".{db_path.name}.old-{epoch}"
    compact_started = False
    try:
        if target.exists() or backup.exists():
            raise FileExistsError("managed compaction target or backup already exists")
        compact_started = True
        _vacuum_into(db_path, target)
        output_measurement = measure_sqlite_store(target)
        if output_measurement.db_size_bytes <= 0:
            raise sqlite3.DatabaseError("VACUUM INTO produced an empty database")
        _validate_compact_copy(db_path, target)
        source_after_copy = measure_sqlite_store(db_path)
        if source_after_copy.wal_size_bytes or source_after_copy.shm_size_bytes:
            raise RuntimeError(
                "source WAL/SHM was not drained before offline swap"
            )
        os.replace(str(db_path), str(backup))
        source_moved = True
        try:
            os.replace(str(target), str(db_path))
        except BaseException:
            if source_moved and backup.exists() and not db_path.exists():
                _restore_backup(db_path, backup)
            raise
        try:
            _reopen_checkpoint_and_validate(db_path)
        except BaseException:
            _restore_backup(db_path, backup)
            raise
        after = measure_sqlite_store(db_path)
        before = diagnostic.measurement
        result = _pending_result(
            payload,
            db_path=db_path,
            diagnostic=diagnostic,
            status=COMPACTION_STATUS_COMPLETED,
            error_code=None,
            message=(
                f"离线紧凑副本已校验并安全替换，物理文件实测净变化 {after.total_size_bytes - before.total_size_bytes} 字节。"
            ),
            compact_copy_started=True,
            compact_output_size_bytes=output_measurement.db_size_bytes,
            before_measurement=before,
            after_measurement=after,
            vacuum_started=True,
            compaction_pending=False,
        )
        try:
            backup.unlink()
        except OSError:
            logger.warning("清理已替换的 SQLite 旧库备份失败: %s", backup, exc_info=True)
        _clear_manifest(compaction_manifest_path(db_path), payload.get("epoch"))
        return result
    except OSError as exc:
        error_code = (
            COMPACTION_ERROR_COMPACT_TARGET_SPACE_INSUFFICIENT
            if getattr(exc, "errno", None) in {28, 112, 39}
            else COMPACTION_ERROR_COMPACT_SWAP_FAILED if backup.exists() else COMPACTION_ERROR_COMPACT_COPY_FAILED
        )
        return _update_pending_failure(
            db_path,
            payload,
            error_code=error_code,
            message=(
                "压缩副本或受控替换未完成；源库未被宣称已释放，待 Runtime 重启后可再次尝试。"
            ),
            diagnostic=diagnostic,
            status=(
                COMPACTION_STATUS_REQUIRES_MORE_SPACE
                if error_code == COMPACTION_ERROR_COMPACT_TARGET_SPACE_INSUFFICIENT
                else COMPACTION_STATUS_REQUIRES_RUNTIME_RESTART
            ),
            exception=exc,
            compact_copy_started=compact_started,
        )
    except (sqlite3.Error, RuntimeError, ValueError) as exc:
        no_space = _is_no_space_error(exc)
        error_code = (
            COMPACTION_ERROR_COMPACT_TARGET_SPACE_INSUFFICIENT
            if no_space
            else COMPACTION_ERROR_COMPACT_VALIDATION_FAILED
            if isinstance(exc, (sqlite3.Error, ValueError))
            else COMPACTION_ERROR_SOURCE_WAL_NOT_DRAINED
        )
        return _update_pending_failure(
            db_path,
            payload,
            error_code=error_code,
            message=(
                "紧凑副本执行遇到目标空间不足，源库保持不变；请释放/扩展源卷后重试。"
                if no_space
                else "紧凑副本校验或源库离线边界未满足，未替换源库；待 Runtime 重启后可再次尝试。"
            ),
            diagnostic=diagnostic,
            status=(
                COMPACTION_STATUS_REQUIRES_MORE_SPACE
                if no_space
                else COMPACTION_STATUS_REQUIRES_RUNTIME_RESTART
            ),
            exception=exc,
            compact_copy_started=compact_started,
        )
    finally:
        if target.exists():
            try:
                target.unlink()
            except OSError:
                logger.warning("清理 SQLite 紧凑临时副本失败: %s", target, exc_info=True)


def _restore_backup(db_path: Path, backup: Path) -> None:
    """Restore the same-volume old database after a failed controlled swap."""
    if db_path.exists():
        db_path.unlink()
    if not backup.exists():
        raise FileNotFoundError(f"SQLite old backup is missing: {backup}")
    os.replace(str(backup), str(db_path))


def _vacuum_into(db_path: Path, target: Path) -> None:
    conn = sqlite3.connect(
        str(db_path),
        timeout=SQLITE_MAINTENANCE_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    try:
        conn.execute(f"PRAGMA busy_timeout={SQLITE_MAINTENANCE_BUSY_TIMEOUT_MS}")
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0] or 0) != 0:
            raise sqlite3.OperationalError("database is busy during offline checkpoint")
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()


def _validate_compact_copy(source: Path, target: Path) -> None:
    _quick_check(target)
    source_signature = _schema_signature(source)
    target_signature = _schema_signature(target)
    if source_signature != target_signature:
        raise ValueError("紧凑副本 schema/table/index 元数据与源库不一致")


def _reopen_checkpoint_and_validate(db_path: Path) -> None:
    conn = sqlite3.connect(
        str(db_path),
        timeout=SQLITE_MAINTENANCE_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    try:
        conn.execute(f"PRAGMA busy_timeout={SQLITE_MAINTENANCE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0] or 0) != 0:
            raise sqlite3.OperationalError("database is busy after offline swap")
        check = conn.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            raise sqlite3.DatabaseError("SQLite quick_check failed after offline swap")
    finally:
        conn.close()


def _quick_check(db_path: Path) -> None:
    conn = sqlite3.connect(
        str(db_path),
        timeout=SQLITE_MAINTENANCE_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    try:
        conn.execute(f"PRAGMA busy_timeout={SQLITE_MAINTENANCE_BUSY_TIMEOUT_MS}")
        check = conn.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            raise sqlite3.DatabaseError("SQLite quick_check failed")
    finally:
        conn.close()


def _schema_signature(db_path: Path) -> tuple[tuple[str, str, str | None, str | None], ...]:
    conn = sqlite3.connect(
        str(db_path),
        timeout=SQLITE_MAINTENANCE_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    try:
        rows = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        return tuple((str(row[0]), str(row[1]), row[2], row[3]) for row in rows)
    finally:
        conn.close()


def _update_pending_failure(
    db_path: Path,
    payload: dict[str, Any],
    *,
    error_code: str,
    message: str,
    diagnostic: SqliteCompactionDiagnostic | None = None,
    status: str = COMPACTION_STATUS_REQUIRES_RUNTIME_RESTART,
    exception: BaseException | None = None,
    compact_copy_started: bool = False,
 ) -> dict[str, Any]:
    if exception is not None:
        logger.warning(
            "SQLite 离线压缩未完成: db=%s error_type=%s",
            db_path,
            exception.__class__.__name__,
            exc_info=True,
        )
    diagnostic = diagnostic or read_sqlite_compaction_diagnostic(db_path)
    updated = dict(payload)
    updated["updated_at"] = _utc_iso()
    updated["attempts"] = int(updated.get("attempts") or 0) + 1
    updated["status"] = status
    updated["error_code"] = error_code
    updated["message"] = _safe_manifest_message(message, error_code)
    updated["diagnostic"] = diagnostic.to_dict()
    _write_manifest(compaction_manifest_path(db_path), updated)
    return _pending_result(
        updated,
        db_path=db_path,
        diagnostic=diagnostic,
        status=status,
        error_code=error_code,
        message=message,
        compact_copy_started=compact_copy_started,
        compaction_pending=True,
    )


def _pending_result(
    payload: dict[str, Any],
    *,
    db_path: Path,
    diagnostic: SqliteCompactionDiagnostic,
    status: str,
    error_code: str | None,
    message: str | None,
    compact_copy_started: bool = False,
    compact_output_size_bytes: int | None = None,
    before_measurement: SqliteStoreMeasurement | None = None,
    after_measurement: SqliteStoreMeasurement | None = None,
    vacuum_started: bool = False,
    compaction_pending: bool = True,
 ) -> dict[str, Any]:
    before = before_measurement or diagnostic.measurement
    observed_after = after_measurement or measure_sqlite_store(db_path)
    net_change = observed_after.total_size_bytes - before.total_size_bytes
    reclaimed = max(before.total_size_bytes - observed_after.total_size_bytes, 0)
    fields = {
        "store_id": str(payload.get("store_id") or ""),
        "eligible": True,
        "status": status,
        "vacuum_started": vacuum_started,
        "compaction_pending": compaction_pending,
        "compact_copy_started": compact_copy_started,
        "compact_output_size_bytes": compact_output_size_bytes,
        "compact_target_size_bytes": diagnostic.compact_target_size_bytes,
        "required_compact_target_size_bytes": diagnostic.compact_target_size_bytes,
        "required_working_bytes": None,
        "available_bytes": diagnostic.available_bytes,
        "page_size_bytes": diagnostic.page_size_bytes,
        "page_count": diagnostic.page_count,
        "freelist_count": diagnostic.freelist_count,
        "auto_vacuum_mode": diagnostic.auto_vacuum_mode,
        "freelist_potential_bytes": diagnostic.freelist_potential_bytes,
        "before_size_bytes": before.total_size_bytes,
        "after_size_bytes": observed_after.total_size_bytes,
        "reclaimed_size_bytes": reclaimed,
        "net_size_change_bytes": net_change,
        "before_db_size_bytes": before.db_size_bytes,
        "after_db_size_bytes": observed_after.db_size_bytes,
        "before_wal_size_bytes": before.wal_size_bytes,
        "after_wal_size_bytes": observed_after.wal_size_bytes,
        "before_shm_size_bytes": before.shm_size_bytes,
        "after_shm_size_bytes": observed_after.shm_size_bytes,
        "error_code": error_code,
        "message": message,
        "pending_manifest_epoch": payload.get("epoch"),
    }
    return fields


def _public_manifest_summary(payload: dict[str, Any]) -> dict[str, Any]:
    diagnostic = payload.get("diagnostic") if isinstance(payload.get("diagnostic"), dict) else {}
    return {
        "store_id": str(payload.get("store_id") or ""),
        "status": str(payload.get("status") or COMPACTION_STATUS_REQUIRES_RUNTIME_RESTART),
        "error_code": payload.get("error_code"),
        "message": _public_manifest_message(payload.get("error_code")),
        "epoch": payload.get("epoch"),
        "compact_target_size_bytes": _nullable_int(diagnostic.get("compact_target_size_bytes")),
        "required_compact_target_size_bytes": _nullable_int(diagnostic.get("compact_target_size_bytes")),
        "available_bytes": _nullable_int(diagnostic.get("available_bytes")),
        "freelist_potential_bytes": _nullable_int(diagnostic.get("freelist_potential_bytes")),
        "auto_vacuum_mode": diagnostic.get("auto_vacuum_mode") if isinstance(diagnostic.get("auto_vacuum_mode"), str) else None,
        "page_size_bytes": _nullable_int(diagnostic.get("page_size_bytes")),
        "page_count": _nullable_int(diagnostic.get("page_count")),
        "freelist_count": _nullable_int(diagnostic.get("freelist_count")),
    }


def _safe_manifest_message(message: Any, error_code: Any) -> str:
    text = str(message or "").strip()
    if not text or len(text) > 512 or "http://" in text.lower() or "https://" in text.lower() or "\\" in text or "/" in text:
        return _public_manifest_message(error_code)
    return text


def _public_manifest_message(error_code: Any) -> str:
    return {
        COMPACTION_ERROR_COMPACT_TARGET_SPACE_INSUFFICIENT: "紧凑副本目标空间不足，待释放空间后重试。",
        COMPACTION_ERROR_RUNTIME_PRESENCE_ACTIVE: "需要停止全部 Runtime worker 后重启以取得离线维护窗口。",
        COMPACTION_ERROR_SOURCE_WAL_NOT_DRAINED: "源库 WAL/SHM 尚未排空，待 Runtime 重启后重试。",
        "source_volume_workspace_insufficient": "源卷临时工作区不足，待 Runtime 重启后执行离线紧凑副本。",
        "source_volume_workspace_unavailable": "无法确认源卷工作区，待 Runtime 重启后重试。",
    }.get(str(error_code), "存在待处理的受控离线空间维护。")


def _replace_diagnostic(
    diagnostic: SqliteCompactionDiagnostic,
    *,
    available_bytes: int | None,
 ) -> SqliteCompactionDiagnostic:
    return SqliteCompactionDiagnostic(
        measurement=diagnostic.measurement,
        page_size_bytes=diagnostic.page_size_bytes,
        page_count=diagnostic.page_count,
        freelist_count=diagnostic.freelist_count,
        auto_vacuum_mode=diagnostic.auto_vacuum_mode,
        freelist_potential_bytes=diagnostic.freelist_potential_bytes,
        compact_target_size_bytes=diagnostic.compact_target_size_bytes,
        available_bytes=available_bytes,
    )


def _is_no_space_error(exc: BaseException) -> bool:
    errno_value = getattr(exc, "errno", None)
    if errno_value in {28, 39, 112}:
        return True
    text = str(exc).lower()
    return "disk is full" in text or "database or disk is full" in text or "no space" in text


def _estimate_compact_target_bytes(
    *,
    page_size_bytes: int | None,
    page_count: int | None,
    freelist_count: int | None,
    measurement: SqliteStoreMeasurement,
 ) -> int | None:
    if page_size_bytes is not None and page_count is not None and freelist_count is not None:
        live_pages = max(page_count - freelist_count, 1)
        return max(live_pages * page_size_bytes + page_size_bytes, page_size_bytes)
    if measurement.db_size_bytes > 0:
        return measurement.db_size_bytes
    return None


def _available_bytes(path: Path) -> int | None:
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return None


def _read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or int(payload.get("version", 0)) != COMPACTION_MANIFEST_VERSION:
            return None
        return payload
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        logger.warning("读取 SQLite 压缩 manifest 失败: %s", path, exc_info=True)
        return None


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                logger.debug("清理 SQLite 压缩 manifest 临时文件失败: %s", temporary, exc_info=True)


def _clear_manifest(path: Path, epoch: Any) -> None:
    payload = _read_manifest(path)
    if payload is None or epoch is None or payload.get("epoch") == epoch:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            logger.warning("清理 SQLite 压缩 manifest 失败: %s", path, exc_info=True)


def _pragma_int(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute(f"PRAGMA {name}").fetchone()
    if row is None:
        return None
    try:
        value = int(row[0])
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _auto_vacuum_mode(value: int | None) -> str | None:
    return {0: "none", 1: "full", 2: "incremental"}.get(value)


def _nullable_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

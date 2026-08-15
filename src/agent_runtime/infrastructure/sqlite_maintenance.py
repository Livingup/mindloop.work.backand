from __future__ import annotations

import errno
import logging
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, TypeVar


logger = logging.getLogger(__name__)

SQLITE_MAINTENANCE_BUSY_TIMEOUT_MS = 2_000
SQLITE_MAINTENANCE_LEASE_TIMEOUT_SECONDS = 2.0
SQLITE_MAINTENANCE_DISK_RESERVE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SqliteStoreMeasurement:
    db_size_bytes: int
    wal_size_bytes: int
    shm_size_bytes: int

    @property
    def total_size_bytes(self) -> int:
        return self.db_size_bytes + self.wal_size_bytes + self.shm_size_bytes


@dataclass(frozen=True, slots=True)
class SqliteStoreMaintenancePreflight:
    """Diagnostics collected before a safe SQLite VACUUM may begin.

    ``required_working_bytes`` is this Runtime's conservative source-volume
    preflight policy. It is not an assertion of SQLite's exact disk requirement,
    which may vary by database and VACUUM execution conditions.
    """

    required_working_bytes: int
    available_bytes: int | None
    page_size_bytes: int | None
    page_count: int | None
    freelist_count: int | None
    error_code: str | None = None
    message: str | None = None

    def to_result_fields(self) -> dict[str, int | None]:
        return {
            "required_working_bytes": self.required_working_bytes,
            "available_bytes": self.available_bytes,
            "page_size_bytes": self.page_size_bytes,
            "page_count": self.page_count,
            "freelist_count": self.freelist_count,
        }


class SqliteStoreMaintenanceGateway(Protocol):
    def maintain_store(self, *, store_id: str, db_path: Path) -> dict[str, Any]: ...


class SqliteMaintenanceCoordinator:
    """Coordinates SQLite read/write leases with exclusive maintenance leases.

    Runtime SQLite repositories acquire a shared operation lease around each public
    transaction/query. Safe physical maintenance acquires an exclusive lease for the
    selected store and waits only for a bounded interval. The per-store advisory lock
    file additionally prevents two Runtime processes that understand this protocol
    from starting maintenance for the same database concurrently. Legacy processes
    remain protected by SQLite busy timeouts and cause a safe busy/deferred result.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._active_operations: dict[str, int] = {}
        self._thread_operation_depths: dict[tuple[str, int], int] = {}
        self._process_operation_leases: dict[str, tuple[_ProcessFileLease, int]] = {}
        self._operation_process_lease_pending: set[str] = set()
        self._maintenance_waiters: dict[str, int] = {}
        self._maintenance_claimed: set[str] = set()
        self._maintenance_active: set[str] = set()

    @contextmanager
    def operation_lease(self, db_path: Path) -> Iterator[None]:
        key = _database_key(db_path)
        thread_key = (key, threading.get_ident())
        needs_process_lease = False
        with self._condition:
            nested_depth = self._thread_operation_depths.get(thread_key, 0)
            if nested_depth == 0:
                while (
                    self._maintenance_waiters.get(key, 0) > 0
                    or key in self._maintenance_claimed
                    or key in self._maintenance_active
                ):
                    self._condition.wait()
                while key in self._operation_process_lease_pending:
                    self._condition.wait()
                if key in self._process_operation_leases:
                    lease, count = self._process_operation_leases[key]
                    self._process_operation_leases[key] = (lease, count + 1)
                else:
                    self._operation_process_lease_pending.add(key)
                    needs_process_lease = True
            self._thread_operation_depths[thread_key] = nested_depth + 1
            self._active_operations[key] = self._active_operations.get(key, 0) + 1
        if needs_process_lease:
            try:
                process_lease = _acquire_operation_file_lease(db_path)
            except BaseException:
                with self._condition:
                    self._operation_process_lease_pending.discard(key)
                    self._thread_operation_depths.pop(thread_key, None)
                    remaining = self._active_operations.get(key, 1) - 1
                    if remaining <= 0:
                        self._active_operations.pop(key, None)
                    else:
                        self._active_operations[key] = remaining
                    self._condition.notify_all()
                raise
            with self._condition:
                self._process_operation_leases[key] = (process_lease, 1)
                self._operation_process_lease_pending.discard(key)
                self._condition.notify_all()
        try:
            yield
        finally:
            process_lease_to_release: _ProcessFileLease | None = None
            with self._condition:
                thread_remaining = self._thread_operation_depths.get(thread_key, 1) - 1
                if thread_remaining <= 0:
                    self._thread_operation_depths.pop(thread_key, None)
                    lease_entry = self._process_operation_leases.get(key)
                    if lease_entry is not None:
                        process_lease, count = lease_entry
                        if count <= 1:
                            self._process_operation_leases.pop(key, None)
                            process_lease_to_release = process_lease
                        else:
                            self._process_operation_leases[key] = (process_lease, count - 1)
                else:
                    self._thread_operation_depths[thread_key] = thread_remaining
                remaining = self._active_operations.get(key, 1) - 1
                if remaining <= 0:
                    self._active_operations.pop(key, None)
                else:
                    self._active_operations[key] = remaining
                self._condition.notify_all()
            if process_lease_to_release is not None:
                process_lease_to_release.release()

    @contextmanager
    def maintenance_lease(
        self,
        db_path: Path,
        *,
        timeout_seconds: float = SQLITE_MAINTENANCE_LEASE_TIMEOUT_SECONDS,
    ) -> Iterator[bool]:
        key = _database_key(db_path)
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        acquired = False
        claimed = False
        process_lease: _ProcessFileLease | None = None
        with self._condition:
            self._maintenance_waiters[key] = self._maintenance_waiters.get(key, 0) + 1
            while (
                self._active_operations.get(key, 0) > 0
                or key in self._maintenance_claimed
                or key in self._maintenance_active
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            if (
                time.monotonic() <= deadline
                and self._active_operations.get(key, 0) == 0
                and key not in self._maintenance_claimed
                and key not in self._maintenance_active
            ):
                self._maintenance_claimed.add(key)
                claimed = True
        if claimed:
            try:
                process_lease = _try_acquire_maintenance_file_lease(db_path, deadline=deadline)
            except OSError:
                logger.warning("无法取得 SQLite 跨进程维护锁，已安全跳过维护。", exc_info=True)
        with self._condition:
            if claimed:
                self._maintenance_claimed.discard(key)
            if (
                process_lease is not None
                and self._active_operations.get(key, 0) == 0
                and key not in self._maintenance_active
            ):
                self._maintenance_active.add(key)
                acquired = True
            remaining_waiters = self._maintenance_waiters.get(key, 1) - 1
            if remaining_waiters <= 0:
                self._maintenance_waiters.pop(key, None)
            else:
                self._maintenance_waiters[key] = remaining_waiters
            self._condition.notify_all()
        if not acquired and process_lease is not None:
            process_lease.release()
            process_lease = None
        try:
            yield acquired
        finally:
            with self._condition:
                if acquired:
                    self._maintenance_active.discard(key)
                self._condition.notify_all()
            if process_lease is not None:
                process_lease.release()


_GLOBAL_SQLITE_MAINTENANCE_COORDINATOR = SqliteMaintenanceCoordinator()


def sqlite_operation_lease(db_path: Path) -> Any:
    return _GLOBAL_SQLITE_MAINTENANCE_COORDINATOR.operation_lease(db_path)


_RepositoryType = TypeVar("_RepositoryType", bound=type)


def coordinated_sqlite_repository(
    db_path_resolver: Callable[[Any], Path],
    *,
    exclude: frozenset[str] = frozenset({"close"}),
 ) -> Callable[[_RepositoryType], _RepositoryType]:
    """Wrap public repository methods in a complete operation lease.

    The wrapper is deliberately applied at repository boundaries so one lease covers
    the full query/transaction rather than individual execute/commit calls.
    """

    def decorate(repository_type: _RepositoryType) -> _RepositoryType:
        for name, attribute in tuple(repository_type.__dict__.items()):
            if name.startswith("_") or name in exclude or not callable(attribute):
                continue

            @wraps(attribute)
            def leased(self: Any, *args: Any, __method: Callable[..., Any] = attribute, **kwargs: Any) -> Any:
                with sqlite_operation_lease(db_path_resolver(self)):
                    return __method(self, *args, **kwargs)

            setattr(repository_type, name, leased)
        return repository_type

    return decorate


class SafeSqliteStoreMaintenanceGateway:
    """Performs bounded checkpoint/VACUUM maintenance without touching WAL files directly."""

    def __init__(self, coordinator: SqliteMaintenanceCoordinator | None = None) -> None:
        self._coordinator = coordinator or _GLOBAL_SQLITE_MAINTENANCE_COORDINATOR

    def maintain_store(self, *, store_id: str, db_path: Path) -> dict[str, Any]:
        before = measure_sqlite_store(db_path)
        base = _store_result(
            store_id=store_id,
            eligible=db_path.exists(),
            status="failed",
            before=before,
        )
        if not db_path.exists():
            return {
                **base,
                "status": "not_available",
                "after_size_bytes": before.total_size_bytes,
                "after_db_size_bytes": before.db_size_bytes,
                "after_wal_size_bytes": before.wal_size_bytes,
                "after_shm_size_bytes": before.shm_size_bytes,
                "reclaimed_size_bytes": 0,
                "net_size_change_bytes": 0,
                "error_code": "database_not_available",
                "message": "数据库文件不存在，已安全跳过空间释放。",
            }

        with self._coordinator.maintenance_lease(db_path) as lease_acquired:
            if not lease_acquired:
                return _deferred_result(
                    base,
                    before,
                    db_path=db_path,
                    status="timeout",
                    error_code="maintenance_lease_timeout",
                    message="相关日志数据库仍在处理读写，未在安全等待时间内取得维护窗口。",
                )
            preflight = _collect_maintenance_preflight(db_path, before)
            base = {**base, **preflight.to_result_fields()}
            if preflight.error_code is not None:
                return _deferred_result(
                    base,
                    before,
                    db_path=db_path,
                    status=(
                        "insufficient_disk_space"
                        if preflight.error_code == "source_volume_workspace_insufficient"
                        else "io_error"
                    ),
                    error_code=preflight.error_code,
                    message=preflight.message or "无法安全开始 SQLite 空间维护。",
                    reclaimed_size_bytes=0,
                )

            vacuum_started = False

            def mark_vacuum_started() -> None:
                nonlocal vacuum_started
                vacuum_started = True

            try:
                _checkpoint_and_vacuum(db_path, on_vacuum_start=mark_vacuum_started)
                after = measure_sqlite_store(db_path)
                net_change = after.total_size_bytes - before.total_size_bytes
                reclaimed = max(-net_change, 0)
                return {
                    **base,
                    "status": "released" if reclaimed > 0 else "skipped_no_changes",
                    "vacuum_started": True,
                    "after_size_bytes": after.total_size_bytes,
                    "after_db_size_bytes": after.db_size_bytes,
                    "after_wal_size_bytes": after.wal_size_bytes,
                    "after_shm_size_bytes": after.shm_size_bytes,
                    "reclaimed_size_bytes": reclaimed,
                    "net_size_change_bytes": net_change,
                    "error_code": None,
                    "message": (
                        f"安全维护已完成，实际释放 {reclaimed} 字节。"
                        if reclaimed > 0
                        else "安全维护已完成，但数据库文件当前没有可测量的空间缩减。"
                    ),
                }
            except sqlite3.OperationalError as exc:
                if _is_busy_error(exc):
                    return _deferred_result(
                        base,
                        before,
                        db_path=db_path,
                        status="busy",
                        error_code="sqlite_busy",
                        message="SQLite 仍有活跃连接或事务，已安全跳过物理释放。",
                        vacuum_started=vacuum_started,
                    )
                return _exception_result(base, before, exc, db_path=db_path, vacuum_started=vacuum_started)
            except PermissionError as exc:
                return _exception_result(
                    base,
                    before,
                    exc,
                    db_path=db_path,
                    status="permission_denied",
                    vacuum_started=vacuum_started,
                )
            except OSError as exc:
                status = "permission_denied" if exc.errno in {errno.EACCES, errno.EPERM} else "io_error"
                return _exception_result(base, before, exc, db_path=db_path, status=status, vacuum_started=vacuum_started)
            except sqlite3.Error as exc:
                return _exception_result(base, before, exc, db_path=db_path, vacuum_started=vacuum_started)
            except Exception as exc:
                return _exception_result(base, before, exc, db_path=db_path, vacuum_started=vacuum_started)


def measure_sqlite_store(db_path: Path) -> SqliteStoreMeasurement:
    return SqliteStoreMeasurement(
        db_size_bytes=_file_size(db_path),
        wal_size_bytes=_file_size(Path(str(db_path) + "-wal")),
        shm_size_bytes=_file_size(Path(str(db_path) + "-shm")),
    )


def _checkpoint_and_vacuum(
    db_path: Path,
    *,
    on_vacuum_start: Callable[[], None] | None = None,
 ) -> None:
    conn = sqlite3.connect(
        str(db_path),
        timeout=SQLITE_MAINTENANCE_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    try:
        conn.execute(f"PRAGMA busy_timeout={SQLITE_MAINTENANCE_BUSY_TIMEOUT_MS}")
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0] or 0) != 0:
            raise sqlite3.OperationalError("database is busy during WAL checkpoint")
        if on_vacuum_start is not None:
            on_vacuum_start()
        conn.execute("VACUUM")
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0] or 0) != 0:
            raise sqlite3.OperationalError("database is busy during final WAL checkpoint")
    finally:
        conn.close()
    check_conn = sqlite3.connect(
        str(db_path),
        timeout=SQLITE_MAINTENANCE_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    try:
        check_conn.execute(f"PRAGMA busy_timeout={SQLITE_MAINTENANCE_BUSY_TIMEOUT_MS}")
        check = check_conn.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            raise sqlite3.DatabaseError("SQLite quick_check failed after maintenance")
    finally:
        check_conn.close()


def _collect_maintenance_preflight(
    db_path: Path,
    before: SqliteStoreMeasurement,
 ) -> SqliteStoreMaintenancePreflight:
    required_working_bytes = max(before.db_size_bytes * 2, SQLITE_MAINTENANCE_DISK_RESERVE_BYTES)
    page_size_bytes, page_count, freelist_count = _read_sqlite_page_metrics(db_path)
    try:
        available_bytes = int(shutil.disk_usage(db_path.parent).free)
    except OSError:
        logger.warning("无法确认 SQLite VACUUM 所需的源卷临时工作区。", exc_info=True)
        return SqliteStoreMaintenancePreflight(
            required_working_bytes=required_working_bytes,
            available_bytes=None,
            page_size_bytes=page_size_bytes,
            page_count=page_count,
            freelist_count=freelist_count,
            error_code="source_volume_workspace_unavailable",
            message="无法确认源卷临时工作区可用空间，安全维护未启动且 VACUUM 未执行。",
        )
    if available_bytes < required_working_bytes:
        return SqliteStoreMaintenancePreflight(
            required_working_bytes=required_working_bytes,
            available_bytes=available_bytes,
            page_size_bytes=page_size_bytes,
            page_count=page_count,
            freelist_count=freelist_count,
            error_code="source_volume_workspace_insufficient",
            message=(
                "当前 Runtime 的保守源卷临时工作区预检未通过，安全维护未启动且 VACUUM 未执行；"
                f"本实现预检需要至少 {required_working_bytes} 字节，当前可用约 {available_bytes} 字节。"
                "该阈值不是 SQLite 的固定必需值；VACUUM 在不同条件下可能需要约两倍数据库大小的临时空间。"
            ),
        )
    return SqliteStoreMaintenancePreflight(
        required_working_bytes=required_working_bytes,
        available_bytes=available_bytes,
        page_size_bytes=page_size_bytes,
        page_count=page_count,
        freelist_count=freelist_count,
    )


def _read_sqlite_page_metrics(db_path: Path) -> tuple[int | None, int | None, int | None]:
    """Reads explanatory page counters without opening a write-capable connection."""
    try:
        read_only_uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(
            read_only_uri,
            uri=True,
            timeout=SQLITE_MAINTENANCE_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
    except (OSError, sqlite3.Error):
        logger.debug("无法读取 SQLite 页面诊断指标。", exc_info=True)
        return None, None, None
    try:
        return (
            _read_pragma_int(conn, "page_size"),
            _read_pragma_int(conn, "page_count"),
            _read_pragma_int(conn, "freelist_count"),
        )
    except sqlite3.Error:
        logger.debug("无法读取 SQLite 页面诊断指标。", exc_info=True)
        return None, None, None
    finally:
        conn.close()


def _read_pragma_int(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute(f"PRAGMA {name}").fetchone()
    if row is None:
        return None
    try:
        value = int(row[0])
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _database_key(db_path: Path) -> str:
    try:
        return str(db_path.expanduser().resolve()).casefold()
    except OSError:
        return str(db_path.expanduser().absolute()).casefold()


_WINDOWS_LOCK_REGION_BYTES = 256


@dataclass(slots=True)
class _ProcessFileLease:
    handle: Any
    offset: int
    length: int
    shared: bool

    def release(self) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(self.offset)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, self.length)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            logger.warning("释放 SQLite 维护锁失败: lock=%s", getattr(self.handle, "name", None), exc_info=True)
        finally:
            self.handle.close()


def _open_lock_handle(db_path: Path) -> Any:
    lock_path = Path(str(db_path) + ".spread-ai-maintenance.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    if os.name == "nt":
        handle.seek(0, os.SEEK_END)
        if handle.tell() < _WINDOWS_LOCK_REGION_BYTES:
            handle.write(b"\0" * (_WINDOWS_LOCK_REGION_BYTES - handle.tell()))
            handle.flush()
    return handle


def _acquire_operation_file_lease(db_path: Path) -> _ProcessFileLease:
    handle = _open_lock_handle(db_path)
    if os.name != "nt":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        return _ProcessFileLease(handle=handle, offset=0, length=1, shared=True)
    import msvcrt

    offset = 1 + (os.getpid() % (_WINDOWS_LOCK_REGION_BYTES - 1))
    while True:
        try:
            handle.seek(offset)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return _ProcessFileLease(handle=handle, offset=offset, length=1, shared=True)
        except OSError:
            time.sleep(0.05)


def _try_acquire_maintenance_file_lease(db_path: Path, *, deadline: float) -> _ProcessFileLease | None:
    handle = _open_lock_handle(db_path)
    if os.name != "nt":
        import fcntl

        while time.monotonic() < deadline:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return _ProcessFileLease(handle=handle, offset=0, length=1, shared=False)
            except OSError:
                time.sleep(0.05)
        handle.close()
        return None
    import msvcrt

    while time.monotonic() < deadline:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, _WINDOWS_LOCK_REGION_BYTES)
            return _ProcessFileLease(
                handle=handle,
                offset=0,
                length=_WINDOWS_LOCK_REGION_BYTES,
                shared=False,
            )
        except OSError:
            time.sleep(0.05)
    handle.close()
    return None


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.exists() and path.is_file() else 0
    except OSError:
        return 0


def _store_result(
    *,
    store_id: str,
    eligible: bool,
    status: str,
    before: SqliteStoreMeasurement,
 ) -> dict[str, Any]:
    return {
        "store_id": store_id,
        "eligible": eligible,
        "status": status,
        "vacuum_started": False,
        "required_working_bytes": None,
        "available_bytes": None,
        "page_size_bytes": None,
        "page_count": None,
        "freelist_count": None,
        "before_size_bytes": before.total_size_bytes,
        "after_size_bytes": None,
        "reclaimed_size_bytes": 0,
        "net_size_change_bytes": 0,
        "before_db_size_bytes": before.db_size_bytes,
        "after_db_size_bytes": None,
        "before_wal_size_bytes": before.wal_size_bytes,
        "after_wal_size_bytes": None,
        "before_shm_size_bytes": before.shm_size_bytes,
        "after_shm_size_bytes": None,
        "error_code": None,
        "message": None,
    }


def _deferred_result(
    base: dict[str, Any],
    before: SqliteStoreMeasurement,
    *,
    db_path: Path,
    status: str,
    error_code: str,
    message: str,
    reclaimed_size_bytes: int | None = None,
    vacuum_started: bool | None = None,
 ) -> dict[str, Any]:
    after = measure_sqlite_store(db_path)
    actual_reclaimed_size_bytes = (
        max(before.total_size_bytes - after.total_size_bytes, 0)
        if reclaimed_size_bytes is None
        else max(int(reclaimed_size_bytes), 0)
    )
    return {
        **base,
        "status": status,
        "vacuum_started": bool(base.get("vacuum_started")) if vacuum_started is None else vacuum_started,
        "after_size_bytes": after.total_size_bytes,
        "after_db_size_bytes": after.db_size_bytes,
        "after_wal_size_bytes": after.wal_size_bytes,
        "after_shm_size_bytes": after.shm_size_bytes,
        "reclaimed_size_bytes": actual_reclaimed_size_bytes,
        "net_size_change_bytes": after.total_size_bytes - before.total_size_bytes,
        "error_code": error_code,
        "message": message,
    }


def _exception_result(
    base: dict[str, Any],
    before: SqliteStoreMeasurement,
    exc: BaseException,
    *,
    db_path: Path,
    status: str = "failed",
    vacuum_started: bool | None = None,
 ) -> dict[str, Any]:
    logger.warning("SQLite 安全空间维护失败: error_type=%s", exc.__class__.__name__, exc_info=True)
    after = measure_sqlite_store(db_path)
    net_change = after.total_size_bytes - before.total_size_bytes
    return {
        **base,
        "status": status,
        "vacuum_started": bool(base.get("vacuum_started")) if vacuum_started is None else vacuum_started,
        "after_size_bytes": after.total_size_bytes,
        "after_db_size_bytes": after.db_size_bytes,
        "after_wal_size_bytes": after.wal_size_bytes,
        "after_shm_size_bytes": after.shm_size_bytes,
        "reclaimed_size_bytes": max(-net_change, 0),
        "net_size_change_bytes": net_change,
        "error_code": exc.__class__.__name__,
        "message": _maintenance_error_message(status, exc),
    }


def _maintenance_error_message(status: str, exc: BaseException) -> str:
    if status == "permission_denied":
        return "数据库或维护锁权限不足，已安全跳过空间释放。"
    if status == "io_error":
        return "数据库或维护锁发生 I/O 错误，已安全跳过空间释放。"
    if isinstance(exc, sqlite3.Error):
        return "SQLite 维护未完成，未将删除数量作为释放字节。"
    return "安全空间维护未完成，未将删除数量作为释放字节。"


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

_MAX_CONTEXT_VALUE_LENGTH = 500
_DEFAULT_COMPLETED_TASK_RETENTION = 256

RUNTIME_TASK_REGISTRY_REQUIRED_TASK_CATEGORIES = (
    "long_lived_background_worker",
    "reconnect_or_subscription_owner",
    "cross_request_or_cross_session_task",
    "shutdown_critical_task",
    "health_relevant_task",
)
RUNTIME_TASK_REGISTRY_DIRECT_CREATE_TASK_EXCEPTIONS = (
    "request_local_helper",
    "request_local_anext_subscription_driver",
    "short_lived_fan_out_helper",
    "higher_level_lifecycle_bound_task",
)
RUNTIME_TASK_REGISTRY_EXCEPTION_MINIMUM_REQUIREMENTS = (
    "owner coroutine/request is explicit",
    "lifecycle cannot outlive the owner coroutine/request",
    "cancellation and cleanup happen in the same owner boundary",
    "nearby code or admission policy explains why registry ownership would change semantics",
)
RUNTIME_TASK_REGISTRY_OWNER_NAMING_RULES = {
    "owner_type": "stable subsystem label such as mcp_session_manager, lacp_client, communication_sse, agent_execution_engine",
    "owner_id": "sanitized resource identity such as service_id, global, scene_id, task_id, terminal_id, or a non-secret connection id",
    "task_class": "semantic lifecycle role such as reconnect_loop, connection_owner, client_run_loop, websocket_reader, poller, watcher",
}


class RuntimeTaskStatus(str, Enum):
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class _RuntimeTaskRecord:
    task_id: str
    task_name: str
    owner_type: str
    owner_id: str
    task_class: str
    started_at: float
    started_at_iso: str
    status: RuntimeTaskStatus = RuntimeTaskStatus.RUNNING
    last_progress_at: float | None = None
    last_progress_at_iso: str | None = None
    completed_at: float | None = None
    completed_at_iso: str | None = None
    cancel_requested_at: float | None = None
    cancel_requested_at_iso: str | None = None
    stop_requested_at: float | None = None
    stop_requested_at_iso: str | None = None
    last_error_type: str | None = None
    last_error: str | None = None
    done_callback_error_type: str | None = None
    done_callback_error: str | None = None
    diagnostic_context: dict[str, Any] = field(default_factory=dict)

    def to_snapshot(self) -> dict[str, Any]:
        health_snapshot = self.health_snapshot()
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "owner_type": self.owner_type,
            "owner_id": self.owner_id,
            "task_class": self.task_class,
            "started_at": self.started_at_iso,
            "last_progress_at": self.last_progress_at_iso,
            "completed_at": self.completed_at_iso,
            "cancel_requested_at": self.cancel_requested_at_iso,
            "stop_requested_at": self.stop_requested_at_iso,
            "status": self.status.value,
            "last_error_type": self.last_error_type,
            "last_error": self.last_error,
            "done_callback_error_type": self.done_callback_error_type,
            "done_callback_error": self.done_callback_error,
            "cancel_requested": self.cancel_requested_at is not None,
            "stop_requested": self.stop_requested_at is not None,
            "health_snapshot": health_snapshot,
            "diagnostic_context": dict(self.diagnostic_context),
        }

    def health_snapshot(self) -> dict[str, Any]:
        now = time.time()
        return {
            "status": self.status.value,
            "running": self.status == RuntimeTaskStatus.RUNNING,
            "terminal": self.is_terminal,
            "cancel_requested": self.cancel_requested_at is not None,
            "stop_requested": self.stop_requested_at is not None,
            "runtime_seconds": max(0.0, now - self.started_at),
            "seconds_since_progress": (
                max(0.0, now - self.last_progress_at)
                if self.last_progress_at is not None
                else None
            ),
            "has_error": self.last_error is not None or self.done_callback_error is not None,
        }

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            RuntimeTaskStatus.COMPLETED,
            RuntimeTaskStatus.FAILED,
            RuntimeTaskStatus.CANCELLED,
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_task_id_part(value: str | None, *, max_length: int = 80) -> str:
    raw = (value or "unknown").strip()
    safe = "".join(char if char.isalnum() or char in {".", "_", "-"} else "-" for char in raw)
    safe = safe.strip("-._") or "unknown"
    return safe[:max_length]


def _new_runtime_task_id(owner_type: str, owner_id: str, task_class: str) -> str:
    return (
        f"{_safe_task_id_part(owner_type, max_length=32)}:"
        f"{_safe_task_id_part(owner_id, max_length=48)}:"
        f"{_safe_task_id_part(task_class, max_length=48)}:"
        f"{uuid4().hex[:12]}"
    )


def _sanitize_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value[:_MAX_CONTEXT_VALUE_LENGTH]
    if not parts.scheme or not parts.netloc:
        return value[:_MAX_CONTEXT_VALUE_LENGTH]
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))[:_MAX_CONTEXT_VALUE_LENGTH]


_SENSITIVE_CONTEXT_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "access_key",
    "secret",
    "password",
    "credential",
    "access_token",
    "refresh_token",
    "id_token",
    "bearer",
    "payload",
    "content",
    "base64",
    "command_line",
    "input_text",
)


def _sanitize_context_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(part in lowered for part in _SENSITIVE_CONTEXT_KEY_PARTS):
        return "[redacted]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if "url" in lowered:
            return _sanitize_url(value)
        if len(value) > _MAX_CONTEXT_VALUE_LENGTH:
            return f"{value[:_MAX_CONTEXT_VALUE_LENGTH]}...(truncated,len={len(value)})"
        return value
    if isinstance(value, Mapping):
        return {str(item_key): _sanitize_context_value(str(item_key), item_value) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        sanitized_items = [_sanitize_context_value(key, item) for item in items[:12]]
        if len(items) > 12:
            sanitized_items.append(f"...(truncated,len={len(items)})")
        return sanitized_items
    text = repr(value)
    if len(text) > _MAX_CONTEXT_VALUE_LENGTH:
        return f"{text[:_MAX_CONTEXT_VALUE_LENGTH]}...(truncated,len={len(text)})"
    return text


def _sanitize_context(context: dict[str, Any] | None) -> dict[str, Any]:
    return {str(key): _sanitize_context_value(str(key), value) for key, value in (context or {}).items()}


_SENSITIVE_ERROR_TEXT_MARKERS = (
    "authorization",
    "bearer ",
    "token",
    "api_key",
    "apikey",
    "secret",
    "password",
    "payload",
    "base64",
    "command_line",
    "access key",
    "refresh key",
)


def _sanitize_error_text(error: str | None) -> str | None:
    if error is None:
        return None
    text = str(error)
    lowered = text.lower()
    if any(marker in lowered for marker in _SENSITIVE_ERROR_TEXT_MARKERS):
        return "[redacted]"
    if len(text) > _MAX_CONTEXT_VALUE_LENGTH:
        return f"{text[:_MAX_CONTEXT_VALUE_LENGTH]}...(truncated,len={len(text)})"
    return text


def runtime_task_registry_admission_policy() -> dict[str, Any]:
    """Return the Stage-4 admission rules for Runtime-managed asyncio tasks.

    Required registry enrollment covers long-lived, reconnect/subscription-owner,
    cross-request/session, shutdown-critical, and health-relevant background tasks.
    Direct ``asyncio.create_task`` remains acceptable only for request-local helper
    tasks whose owner coroutine also owns cancellation and cleanup; communication
    SSE ``anext(subscription)`` drivers are the canonical Stage-4 exception.
    """
    return {
        "required_registry_categories": list(RUNTIME_TASK_REGISTRY_REQUIRED_TASK_CATEGORIES),
        "direct_create_task_exceptions": list(RUNTIME_TASK_REGISTRY_DIRECT_CREATE_TASK_EXCEPTIONS),
        "exception_minimum_requirements": list(RUNTIME_TASK_REGISTRY_EXCEPTION_MINIMUM_REQUIREMENTS),
        "owner_naming_rules": dict(RUNTIME_TASK_REGISTRY_OWNER_NAMING_RULES),
    }


class RuntimeTaskRegistry:
    """Low-intrusion lifecycle registry for Runtime background asyncio tasks.

    New Runtime background tasks must be checked against
    :func:`runtime_task_registry_admission_policy` before using raw
    ``asyncio.create_task``. Registry-managed tasks must provide stable
    ``owner_type``, sanitized ``owner_id``, and semantic ``task_class`` values.
    """

    def __init__(self, *, completed_task_retention: int = _DEFAULT_COMPLETED_TASK_RETENTION) -> None:
        self._records: dict[str, _RuntimeTaskRecord] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_ids: dict[asyncio.Task[Any], str] = {}
        self._completed_task_retention = max(1, int(completed_task_retention))
        self._lock = threading.RLock()

    def create_task(
        self,
        awaitable: Awaitable[_T],
        *,
        task_name: str,
        owner_type: str,
        owner_id: str,
        task_class: str,
        task_id: str | None = None,
        diagnostic_context: dict[str, Any] | None = None,
    ) -> asyncio.Task[_T]:
        task = asyncio.create_task(awaitable, name=task_name)
        self.register_task(
            task,
            task_name=task_name,
            owner_type=owner_type,
            owner_id=owner_id,
            task_class=task_class,
            task_id=task_id,
            diagnostic_context=diagnostic_context,
        )
        return task

    def create(
        self,
        awaitable: Awaitable[_T],
        *,
        task_name: str,
        owner_type: str,
        owner_id: str,
        task_class: str,
        task_id: str | None = None,
        diagnostic_context: dict[str, Any] | None = None,
    ) -> asyncio.Task[_T]:
        return self.create_task(
            awaitable,
            task_name=task_name,
            owner_type=owner_type,
            owner_id=owner_id,
            task_class=task_class,
            task_id=task_id,
            diagnostic_context=diagnostic_context,
        )

    def register(
        self,
        task: asyncio.Task[_T],
        *,
        task_name: str | None = None,
        owner_type: str,
        owner_id: str,
        task_class: str,
        task_id: str | None = None,
        diagnostic_context: dict[str, Any] | None = None,
    ) -> asyncio.Task[_T]:
        return self.register_task(
            task,
            task_name=task_name,
            owner_type=owner_type,
            owner_id=owner_id,
            task_class=task_class,
            task_id=task_id,
            diagnostic_context=diagnostic_context,
        )

    def register_task(
        self,
        task: asyncio.Task[_T],
        *,
        task_name: str | None = None,
        owner_type: str,
        owner_id: str,
        task_class: str,
        task_id: str | None = None,
        diagnostic_context: dict[str, Any] | None = None,
    ) -> asyncio.Task[_T]:
        actual_task_id = task_id or _new_runtime_task_id(owner_type, owner_id, task_class)
        actual_task_name = task_name or task.get_name()
        now = time.time()
        record = _RuntimeTaskRecord(
            task_id=actual_task_id,
            task_name=actual_task_name,
            owner_type=owner_type,
            owner_id=owner_id,
            task_class=task_class,
            started_at=now,
            started_at_iso=_utc_now_iso(),
            last_progress_at=now,
            last_progress_at_iso=_utc_now_iso(),
            diagnostic_context=_sanitize_context(diagnostic_context),
        )
        with self._lock:
            self._records[actual_task_id] = record
            self._tasks[actual_task_id] = task
            self._task_ids[task] = actual_task_id
            self._prune_completed_locked()
        task.add_done_callback(self._safe_on_task_done)
        self._log_transition(logging.DEBUG, "runtime background task registered", record, phase="runtime_task.registered")
        if task.done():
            self._safe_on_task_done(task)
        return task

    def mark_progress(
        self,
        task_or_id: asyncio.Task[Any] | str,
        *,
        diagnostic_context: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            record = self._record_for_locked(task_or_id)
            if record is None or record.is_terminal:
                return
            now = time.time()
            record.last_progress_at = now
            record.last_progress_at_iso = _utc_now_iso()
            if diagnostic_context:
                record.diagnostic_context.update(_sanitize_context(diagnostic_context))
            snapshot = record.to_snapshot()
        self._log_transition(
            logging.DEBUG,
            "runtime background task progress",
            record,
            phase="runtime_task.progress",
            snapshot=snapshot,
        )

    def complete(
        self,
        task_or_id: asyncio.Task[Any] | str,
        *,
        diagnostic_context: dict[str, Any] | None = None,
    ) -> None:
        self._set_terminal_status(
            task_or_id,
            RuntimeTaskStatus.COMPLETED,
            phase="runtime_task.completed",
            message="runtime background task completed",
            diagnostic_context=diagnostic_context,
        )

    def fail(
        self,
        task_or_id: asyncio.Task[Any] | str,
        error: BaseException | str,
        *,
        diagnostic_context: dict[str, Any] | None = None,
    ) -> None:
        error_type = error.__class__.__name__ if isinstance(error, BaseException) else "Error"
        error_text = str(error)
        self._set_terminal_status(
            task_or_id,
            RuntimeTaskStatus.FAILED,
            phase="runtime_task.failed",
            message="runtime background task failed",
            error_type=error_type,
            error=error_text,
            diagnostic_context=diagnostic_context,
        )

    def cancel(
        self,
        task_or_id: asyncio.Task[Any] | str,
        *,
        message: str | None = None,
        stop: bool = True,
        diagnostic_context: dict[str, Any] | None = None,
    ) -> bool:
        with self._lock:
            record = self._record_for_locked(task_or_id)
            if record is None or record.is_terminal:
                return False
            task = self._tasks.get(record.task_id)
            now = time.time()
            now_iso = _utc_now_iso()
            record.status = RuntimeTaskStatus.CANCELLING
            record.cancel_requested_at = now
            record.cancel_requested_at_iso = now_iso
            if stop:
                record.stop_requested_at = now
                record.stop_requested_at_iso = now_iso
            record.last_progress_at = now
            record.last_progress_at_iso = now_iso
            if diagnostic_context:
                record.diagnostic_context.update(_sanitize_context(diagnostic_context))
        if task is not None and not task.done():
            task.cancel(message)
        self._log_transition(
            logging.WARNING,
            "runtime background task cancellation requested",
            record,
            phase="runtime_task.cancel_requested",
            error=message,
        )
        return True

    async def shutdown(
        self,
        *,
        owner_type: str | None = None,
        owner_id: str | None = None,
        task_class: str | None = None,
        timeout: float | None = 5.0,
        cancel: bool = True,
        diagnostic_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current_task = asyncio.current_task()
        with self._lock:
            items = [
                (task_id, task, record)
                for task_id, task in self._tasks.items()
                if task is not current_task
                and not task.done()
                and not self._records[task_id].is_terminal
                and (owner_type is None or self._records[task_id].owner_type == owner_type)
                and (owner_id is None or self._records[task_id].owner_id == owner_id)
                and (task_class is None or self._records[task_id].task_class == task_class)
            ]
        logger.info(
            "Runtime task registry shutdown requested: task_count=%s owner_type=%s owner_id=%s task_class=%s timeout=%s cancel=%s context=%s",
            len(items),
            owner_type,
            owner_id,
            task_class,
            timeout,
            cancel,
            _sanitize_context(diagnostic_context),
        )
        if cancel:
            for task_id, _task, _record in items:
                self.cancel(
                    task_id,
                    message="runtime task registry shutdown",
                    stop=True,
                    diagnostic_context=diagnostic_context,
                )
        tasks = [task for _task_id, task, _record in items if not task.done()]
        done: set[asyncio.Task[Any]] = set()
        pending: set[asyncio.Task[Any]] = set(tasks)
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            logger.warning(
                "Runtime task registry shutdown timed out: pending_count=%s pending_task_ids=%s",
                len(pending),
                [self._task_ids.get(task) for task in pending],
            )
        return {
            "requested": len(items),
            "done": len(done),
            "pending": len(pending),
            "cancel_requested": cancel,
            "timeout": timeout,
        }

    def snapshot(self, *, include_completed: bool = True) -> dict[str, Any]:
        with self._lock:
            records = list(self._records.values())
            if not include_completed:
                records = [record for record in records if not record.is_terminal]
            status_counts = Counter(record.status.value for record in self._records.values())
            owner_counts = Counter(f"{record.owner_type}:{record.owner_id}" for record in self._records.values() if not record.is_terminal)
            class_counts = Counter(record.task_class for record in self._records.values() if not record.is_terminal)
            return {
                "total_count": len(self._records),
                "active_count": sum(1 for record in self._records.values() if not record.is_terminal),
                "by_status": dict(sorted(status_counts.items())),
                "active_by_owner": dict(sorted(owner_counts.items())),
                "active_by_class": dict(sorted(class_counts.items())),
                "tasks": [record.to_snapshot() for record in sorted(records, key=lambda item: item.started_at)],
            }

    def task_id_for(self, task: asyncio.Task[Any]) -> str | None:
        with self._lock:
            return self._task_ids.get(task)

    def _record_for_locked(self, task_or_id: asyncio.Task[Any] | str) -> _RuntimeTaskRecord | None:
        task_id = self._task_ids.get(task_or_id) if isinstance(task_or_id, asyncio.Task) else task_or_id
        if task_id is None:
            return None
        return self._records.get(task_id)

    def _set_terminal_status(
        self,
        task_or_id: asyncio.Task[Any] | str,
        status: RuntimeTaskStatus,
        *,
        phase: str,
        message: str,
        error_type: str | None = None,
        error: str | None = None,
        diagnostic_context: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            record = self._record_for_locked(task_or_id)
            if record is None:
                return
            now = time.time()
            record.status = status
            record.completed_at = now
            record.completed_at_iso = _utc_now_iso()
            record.last_progress_at = now
            record.last_progress_at_iso = record.completed_at_iso
            record.last_error_type = error_type
            record.last_error = _sanitize_error_text(error)
            if diagnostic_context:
                record.diagnostic_context.update(_sanitize_context(diagnostic_context))
            self._prune_completed_locked()
        level = logging.ERROR if status == RuntimeTaskStatus.FAILED else logging.WARNING if status == RuntimeTaskStatus.CANCELLED else logging.DEBUG
        self._log_transition(level, message, record, phase=phase, error_type=error_type, error=error)

    def _safe_on_task_done(self, task: asyncio.Task[Any]) -> None:
        try:
            self._on_task_done(task)
        except BaseException as exc:  # noqa: BLE001 - done callbacks must never leak to the loop
            error_type = exc.__class__.__name__
            sanitized_error = _sanitize_error_text(str(exc))
            with self._lock:
                task_id = self._task_ids.get(task)
                record = self._records.get(task_id) if task_id is not None else None
                if record is not None:
                    record.done_callback_error_type = error_type
                    record.done_callback_error = sanitized_error
            logger.error(
                "Runtime task registry done-callback processing failed: task_id=%s error_type=%s error=%s",
                task_id,
                error_type,
                sanitized_error,
            )

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        with self._lock:
            task_id = self._task_ids.get(task)
            if task_id is None:
                return
            record = self._records.get(task_id)
        if record is None:
            return
        if task.cancelled():
            self._set_terminal_status(
                task_id,
                RuntimeTaskStatus.CANCELLED,
                phase="runtime_task.cancelled",
                message="runtime background task cancelled",
                error_type="CancelledError",
            )
            return
        try:
            task.result()
        except BaseException as exc:
            self._set_terminal_status(
                task_id,
                RuntimeTaskStatus.FAILED,
                phase="runtime_task.failed",
                message="runtime background task failed",
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
        else:
            self._set_terminal_status(
                task_id,
                RuntimeTaskStatus.COMPLETED,
                phase="runtime_task.completed",
                message="runtime background task completed",
            )

    def _prune_completed_locked(self) -> None:
        terminal_ids = [task_id for task_id, record in self._records.items() if record.is_terminal]
        overflow = len(terminal_ids) - self._completed_task_retention
        if overflow <= 0:
            return
        for task_id in terminal_ids[:overflow]:
            task = self._tasks.pop(task_id, None)
            if task is not None:
                self._task_ids.pop(task, None)
            self._records.pop(task_id, None)

    def _log_transition(
        self,
        level: int,
        message: str,
        record: _RuntimeTaskRecord,
        *,
        phase: str,
        error_type: str | None = None,
        error: str | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        if not logger.isEnabledFor(level):
            return
        task_snapshot = snapshot or record.to_snapshot()
        logger.log(
            level,
            "%s: task_id=%s task_name=%s owner_type=%s owner_id=%s task_class=%s phase=%s status=%s "
            "started_at=%s last_progress_at=%s completed_at=%s cancel_requested_at=%s stop_requested_at=%s "
            "error_type=%s error=%s done_callback_error_type=%s done_callback_error=%s context=%s",
            message,
            record.task_id,
            record.task_name,
            record.owner_type,
            record.owner_id,
            record.task_class,
            phase,
            task_snapshot.get("status"),
            task_snapshot.get("started_at"),
            task_snapshot.get("last_progress_at"),
            task_snapshot.get("completed_at"),
            task_snapshot.get("cancel_requested_at"),
            task_snapshot.get("stop_requested_at"),
            error_type or task_snapshot.get("last_error_type"),
            _sanitize_error_text(error) or task_snapshot.get("last_error"),
            task_snapshot.get("done_callback_error_type"),
            task_snapshot.get("done_callback_error"),
            task_snapshot.get("diagnostic_context"),
        )


_runtime_task_registry = RuntimeTaskRegistry()


def get_runtime_task_registry() -> RuntimeTaskRegistry:
    return _runtime_task_registry


def runtime_task_registry_snapshot(*, include_completed: bool = True) -> dict[str, Any]:
    return _runtime_task_registry.snapshot(include_completed=include_completed)


async def shutdown_runtime_tasks(
    *,
    owner_type: str | None = None,
    owner_id: str | None = None,
    task_class: str | None = None,
    timeout: float | None = 5.0,
    cancel: bool = True,
    diagnostic_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await _runtime_task_registry.shutdown(
        owner_type=owner_type,
        owner_id=owner_id,
        task_class=task_class,
        timeout=timeout,
        cancel=cancel,
        diagnostic_context=diagnostic_context,
    )


BackgroundTaskRegistry = RuntimeTaskRegistry

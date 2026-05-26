from __future__ import annotations

import base64
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from agent_runtime.file_resource.north_gateway.resource_create_contracts import Base64CreateRequest
from agent_runtime.file_resource.service import FileResourceCancelledError, FileResourceProgressCallback, FileResourceService

logger = logging.getLogger(__name__)

TERMINAL_UPLOAD_STATUSES = {"completed", "failed", "cancelled"}


@dataclass(frozen=True, slots=True)
class FileResourceUploadTaskEvent:
    task_id: str
    seq: int
    status: str
    phase: str
    message: str
    progress_percent: int
    uploaded_bytes: int | None = None
    total_bytes: int | None = None
    resources: list[dict[str, Any]] | None = None
    error_message: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "seq": self.seq,
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "progress_percent": self.progress_percent,
            "uploaded_bytes": self.uploaded_bytes,
            "total_bytes": self.total_bytes,
            "resources": self.resources,
            "error_message": self.error_message,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class FileResourceUploadTask:
    task_id: str
    resources: list[Base64CreateRequest]
    names: list[str | None]
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    condition: threading.Condition = field(init=False)
    events: list[FileResourceUploadTaskEvent] = field(default_factory=list)
    status: str = "queued"
    phase: str = "queued"
    message: str = "等待上传处理。"
    progress_percent: int = 0
    result_resources: list[dict[str, Any]] | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)


class FileResourceUploadTaskService:
    def __init__(self, file_resource_service: FileResourceService, *, max_retained_events: int = 500) -> None:
        self._file_resource_service = file_resource_service
        self._max_retained_events = max_retained_events
        self._tasks: dict[str, FileResourceUploadTask] = {}
        self._tasks_lock = threading.RLock()

    def create_task(self, *, resources: list[Base64CreateRequest], names: list[str | None]) -> FileResourceUploadTask:
        task = FileResourceUploadTask(task_id=f"file-upload-{uuid4().hex}", resources=resources, names=names)
        with self._tasks_lock:
            self._tasks[task.task_id] = task
        self._publish(task, status="queued", phase="queued", message="等待上传处理。", progress_percent=0)
        worker = threading.Thread(target=self._run_task, args=(task,), daemon=True, name=f"file-upload-{task.task_id}")
        worker.start()
        return task

    def get_task(self, task_id: str) -> FileResourceUploadTask | None:
        with self._tasks_lock:
            return self._tasks.get(task_id)

    def cancel_task(self, task_id: str) -> FileResourceUploadTask | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        task.cancel_event.set()
        with task.lock:
            if task.status not in TERMINAL_UPLOAD_STATUSES:
                self._publish_locked(task, status="cancelling", phase=task.phase, message="正在取消上传。", progress_percent=task.progress_percent)
        return task

    def snapshot(self, task: FileResourceUploadTask) -> dict[str, Any]:
        with task.lock:
            return {
                "task_id": task.task_id,
                "status": task.status,
                "phase": task.phase,
                "message": task.message,
                "progress_percent": task.progress_percent,
                "resources": task.result_resources,
                "error_message": task.error_message,
                "latest_seq": task.events[-1].seq if task.events else 0,
            }

    def list_events_after(self, task: FileResourceUploadTask, after_seq: int) -> list[FileResourceUploadTaskEvent]:
        with task.lock:
            return [event for event in task.events if event.seq > after_seq]

    def wait_for_event(self, task: FileResourceUploadTask, after_seq: int, timeout_seconds: float) -> bool:
        with task.condition:
            if any(event.seq > after_seq for event in task.events):
                return True
            task.condition.wait(timeout=timeout_seconds)
            return any(event.seq > after_seq for event in task.events)

    def _run_task(self, task: FileResourceUploadTask) -> None:
        try:
            self._ensure_not_cancelled(task)
            self._publish(task, status="running", phase="decoding", message="正在解码文件内容。", progress_percent=5)
            decoded_sizes = self._decoded_sizes(task.resources)
            total_bytes = sum(decoded_sizes)
            self._ensure_not_cancelled(task)
            self._publish(task, status="running", phase="processing", message="正在校验并写入本地缓存。", progress_percent=15, total_bytes=total_bytes)

            def progress_callback(phase: str, message: str, progress_percent: int, uploaded_bytes: int | None, total_bytes: int | None) -> None:
                self._ensure_not_cancelled(task)
                self._publish(
                    task,
                    status="running",
                    phase=phase,
                    message=message,
                    progress_percent=progress_percent,
                    uploaded_bytes=uploaded_bytes,
                    total_bytes=total_bytes,
                )

            created = self._file_resource_service.create_resources_from_base64_contents(
                task.resources,
                progress_callback=progress_callback,
                cancel_checker=task.cancel_event.is_set,
            )
            self._ensure_not_cancelled(task)
            resolved = self._file_resource_service.get_resources([item.resource_id for item in created])
            resources = []
            for index, resource in enumerate(resolved):
                if resource is None:
                    continue
                resources.append(_resource_dict(resource, name=task.names[index] if index < len(task.names) else None))
            task.result_resources = resources
            self._publish(task, status="completed", phase="completed", message="上传完成。", progress_percent=100, resources=resources)
        except FileResourceCancelledError:
            task.cancel_event.set()
            self._publish(task, status="cancelled", phase="cancelled", message="上传已取消。", progress_percent=task.progress_percent)
        except Exception as exc:
            logger.exception("文件资源上传任务失败: task_id=%s", task.task_id)
            task.error_message = str(exc) or exc.__class__.__name__
            self._publish(task, status="failed", phase="failed", message="上传失败。", progress_percent=task.progress_percent, error_message=task.error_message)

    def _decoded_sizes(self, resources: list[Base64CreateRequest]) -> list[int]:
        sizes: list[int] = []
        for resource in resources:
            sizes.append(len(base64.b64decode(resource.resource_base64_content)))
        return sizes

    def _ensure_not_cancelled(self, task: FileResourceUploadTask) -> None:
        if task.cancel_event.is_set():
            raise FileResourceCancelledError("文件上传已取消。")

    def _publish(
        self,
        task: FileResourceUploadTask,
        *,
        status: str,
        phase: str,
        message: str,
        progress_percent: int,
        uploaded_bytes: int | None = None,
        total_bytes: int | None = None,
        resources: list[dict[str, Any]] | None = None,
        error_message: str | None = None,
    ) -> None:
        with task.lock:
            self._publish_locked(
                task,
                status=status,
                phase=phase,
                message=message,
                progress_percent=progress_percent,
                uploaded_bytes=uploaded_bytes,
                total_bytes=total_bytes,
                resources=resources,
                error_message=error_message,
            )

    def _publish_locked(
        self,
        task: FileResourceUploadTask,
        *,
        status: str,
        phase: str,
        message: str,
        progress_percent: int,
        uploaded_bytes: int | None = None,
        total_bytes: int | None = None,
        resources: list[dict[str, Any]] | None = None,
        error_message: str | None = None,
    ) -> None:
        event = FileResourceUploadTaskEvent(
            task_id=task.task_id,
            seq=(task.events[-1].seq + 1) if task.events else 1,
            status=status,
            phase=phase,
            message=message,
            progress_percent=max(0, min(100, progress_percent)),
            uploaded_bytes=uploaded_bytes,
            total_bytes=total_bytes,
            resources=resources,
            error_message=error_message,
        )
        task.status = status
        task.phase = phase
        task.message = message
        task.progress_percent = event.progress_percent
        if resources is not None:
            task.result_resources = resources
        if error_message is not None:
            task.error_message = error_message
        task.events.append(event)
        if len(task.events) > self._max_retained_events:
            del task.events[: len(task.events) - self._max_retained_events]
        task.condition.notify_all()


def _resource_dict(resource, *, name: str | None) -> dict[str, Any]:
    metadata = resource.resource_metadata
    metadata_dict = _metadata_dict(metadata)
    return {
        "resource_id": resource.resource_id,
        "name": name,
        "mime_type": resource.resource_mime_type,
        "size_in_bytes": len(base64.b64decode(resource.resource_base64_content)),
        "object_storage_url": resource.object_storage_url,
        "width": metadata_dict.get("width"),
        "height": metadata_dict.get("height"),
        "page_count": metadata_dict.get("page_count"),
        "metadata": metadata_dict,
    }


def _metadata_dict(metadata) -> dict[str, Any]:
    if hasattr(metadata, "image_width") and hasattr(metadata, "image_height"):
        return {
            "resource_category": getattr(metadata, "resource_category", "image"),
            "width": metadata.image_width,
            "height": metadata.image_height,
        }
    if hasattr(metadata, "pdf_page_count"):
        return {
            "resource_category": getattr(metadata, "resource_category", "pdf"),
            "page_count": metadata.pdf_page_count,
        }
    return {}

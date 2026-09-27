from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent_runtime.config import ConfigRegistry
from agent_runtime.file_resource.models import ImageResourceMetadata, PdfResourceMetadata
from agent_runtime.file_resource.north_gateway.resource_create_contracts import Base64CreateRequest, RemoteUrlCreateRequest
from agent_runtime.file_resource.settings_repository import (
    DEFAULT_OBJECT_STORAGE_CONNECT_TIMEOUT_SECONDS,
    ObjectStorageSettingsRepository,
    create_object_storage_settings_repository,
)
from agent_runtime.file_resource.shared_service import get_file_resource_service, get_file_resource_upload_task_service
from agent_runtime.file_resource.south_gateway.object_storage.runtime_configured import (
    ObjectStorageConfigurationError,
)
from agent_runtime.file_resource.upload_task_service import FileResourceUploadTaskEvent, TERMINAL_UPLOAD_STATUSES

logger = logging.getLogger(__name__)

MAX_LOCAL_IMAGE_PREVIEW_BYTES = 50 * 1024 * 1024


class CreateFileResourceItem(BaseModel):
    name: str | None = None
    mime_type: str
    base64_content: str


class CreateFileResourcesBody(BaseModel):
    resources: list[CreateFileResourceItem] = Field(default_factory=list)


class CreateFileResourceUrlItem(BaseModel):
    name: str | None = None
    url: str
    mime_type: str | None = None
    max_bytes: int | None = None


class CreateFileResourcesFromUrlsBody(BaseModel):
    resources: list[CreateFileResourceUrlItem] = Field(default_factory=list)


class FileResourceResponse(BaseModel):
    resource_id: str
    name: str | None = None
    mime_type: str
    size_in_bytes: int
    object_storage_url: str
    local_storage_path: str
    width: int | None = None
    height: int | None = None
    page_count: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateFileResourcesResponse(BaseModel):
    resources: list[FileResourceResponse]


class FileResourceUploadTaskResponse(BaseModel):
    task_id: str
    status: str
    phase: str
    message: str
    progress_percent: int
    resources: list[dict[str, Any]] | None = None
    error_message: str | None = None
    latest_seq: int
    events_url: str


class ObjectStorageSettingsResponse(BaseModel):
    gateway_provider: str
    object_storage_endpoint: str
    object_storage_region: str
    object_storage_bucket_name: str
    object_storage_public_base_url: str
    object_storage_object_key_prefix: str
    object_storage_access_key_id: str | None = None
    object_storage_access_key_secret: str | None = None
    object_storage_access_key_id_configured: bool
    object_storage_access_key_secret_configured: bool
    connect_timeout_seconds: int
    db_path: str
    updated_at: str | None = None


class ObjectStorageSettingsUpdateBody(BaseModel):
    gateway_provider: str = "aliyun_oss"
    object_storage_endpoint: str = ""
    object_storage_region: str = ""
    object_storage_bucket_name: str = ""
    object_storage_public_base_url: str = ""
    object_storage_object_key_prefix: str = ""
    object_storage_access_key_id: str | None = None
    object_storage_access_key_secret: str | None = None
    connect_timeout_seconds: int = DEFAULT_OBJECT_STORAGE_CONNECT_TIMEOUT_SECONDS


def create_file_resource_router() -> APIRouter:
    router = APIRouter()

    @router.post("", response_model=CreateFileResourcesResponse)
    def create_resources(body: CreateFileResourcesBody) -> CreateFileResourcesResponse:
        if not body.resources:
            raise HTTPException(status_code=400, detail="resources 不能为空。")
        try:
            service = get_file_resource_service()
            created = service.create_resources_from_base64_contents([
                Base64CreateRequest(
                    resource_base64_content=item.base64_content,
                    resource_mime_type=item.mime_type,
                )
                for item in body.resources
            ])
            resolved = service.get_resources([item.resource_id for item in created])
        except ValueError as exc:
            logger.warning(
                "创建统一文件资源失败: count=%s mime_types=%s reason=%s",
                len(body.resources),
                [item.mime_type for item in body.resources],
                str(exc),
                exc_info=True,
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ObjectStorageConfigurationError as exc:
            logger.warning(
                "创建统一文件资源对象存储配置失败: count=%s mime_types=%s reason=%s",
                len(body.resources),
                [item.mime_type for item in body.resources],
                str(exc),
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.exception(
                "创建统一文件资源运行时失败: count=%s mime_types=%s",
                len(body.resources),
                [item.mime_type for item in body.resources],
            )
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        responses: list[FileResourceResponse] = []
        for index, resource in enumerate(resolved):
            if resource is None:
                continue
            responses.append(_resource_response(resource, name=body.resources[index].name))
        return CreateFileResourcesResponse(resources=responses)

    @router.post("/from-urls", response_model=CreateFileResourcesResponse)
    def create_resources_from_urls(body: CreateFileResourcesFromUrlsBody) -> CreateFileResourcesResponse:
        if not body.resources:
            raise HTTPException(status_code=400, detail="resources 不能为空。")
        try:
            service = get_file_resource_service()
            created = service.create_resources_from_remote_urls([
                RemoteUrlCreateRequest(
                    resource_url=item.url,
                    resource_mime_type=item.mime_type,
                    max_bytes=item.max_bytes,
                )
                for item in body.resources
            ])
            resolved = service.get_resources([item.resource_id for item in created])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        responses: list[FileResourceResponse] = []
        for index, resource in enumerate(resolved):
            if resource is None:
                continue
            responses.append(_resource_response(resource, name=body.resources[index].name))
        return CreateFileResourcesResponse(resources=responses)

    @router.post("/upload-tasks", response_model=FileResourceUploadTaskResponse)
    def create_upload_task(body: CreateFileResourcesBody) -> FileResourceUploadTaskResponse:
        if not body.resources:
            raise HTTPException(status_code=400, detail="resources 不能为空。")
        task_service = get_file_resource_upload_task_service()
        task = task_service.create_task(
            resources=[
                Base64CreateRequest(
                    resource_base64_content=item.base64_content,
                    resource_mime_type=item.mime_type,
                )
                for item in body.resources
            ],
            names=[item.name for item in body.resources],
        )
        return _upload_task_response(task_service.snapshot(task))

    @router.get("/upload-tasks/{task_id}", response_model=FileResourceUploadTaskResponse)
    def get_upload_task(task_id: str) -> FileResourceUploadTaskResponse:
        task_service = get_file_resource_upload_task_service()
        task = task_service.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"上传任务不存在: {task_id}")
        return _upload_task_response(task_service.snapshot(task))

    @router.post("/upload-tasks/{task_id}/cancel", response_model=FileResourceUploadTaskResponse)
    def cancel_upload_task(task_id: str) -> FileResourceUploadTaskResponse:
        task_service = get_file_resource_upload_task_service()
        task = task_service.cancel_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"上传任务不存在: {task_id}")
        return _upload_task_response(task_service.snapshot(task))

    @router.get("/upload-tasks/{task_id}/events")
    async def stream_upload_task_events(
        task_id: str,
        request: Request,
        after_seq: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        task_service = get_file_resource_upload_task_service()
        task = task_service.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"上传任务不存在: {task_id}")

        async def event_generator():
            cursor = max(after_seq, 0)
            while True:
                for event in task_service.list_events_after(task, cursor):
                    cursor = event.seq
                    yield _format_upload_task_sse(event)
                snapshot = task_service.snapshot(task)
                if snapshot["status"] in TERMINAL_UPLOAD_STATUSES:
                    return
                if await request.is_disconnected():
                    return
                has_event = await task_service.wait_for_event_async(
                    task,
                    cursor,
                    15,
                )
                if await request.is_disconnected():
                    return
                if not has_event:
                    yield ": keep-alive\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/object-storage-settings", response_model=ObjectStorageSettingsResponse)
    def get_object_storage_settings() -> ObjectStorageSettingsResponse:
        repository = _object_storage_settings_repository()
        return _object_storage_settings_response(repository)

    @router.put("/object-storage-settings", response_model=ObjectStorageSettingsResponse)
    def update_object_storage_settings(
        body: ObjectStorageSettingsUpdateBody,
    ) -> ObjectStorageSettingsResponse:
        if body.gateway_provider != "aliyun_oss":
            raise HTTPException(status_code=400, detail=f"不支持的对象存储服务商: {body.gateway_provider}")
        if body.connect_timeout_seconds <= 0:
            raise HTTPException(status_code=400, detail="对象存储连接超时时间必须大于 0。")
        repository = _object_storage_settings_repository()
        current = repository.get_settings()
        next_access_key_id = (
            current.object_storage_access_key_id
            if body.object_storage_access_key_id is None
            else body.object_storage_access_key_id
        )
        next_access_key_secret = (
            current.object_storage_access_key_secret
            if body.object_storage_access_key_secret is None
            else body.object_storage_access_key_secret
        )
        repository.save_settings(
            gateway_provider=body.gateway_provider,
            object_storage_endpoint=body.object_storage_endpoint,
            object_storage_region=body.object_storage_region,
            object_storage_bucket_name=body.object_storage_bucket_name,
            object_storage_public_base_url=body.object_storage_public_base_url,
            object_storage_object_key_prefix=body.object_storage_object_key_prefix,
            object_storage_access_key_id=next_access_key_id,
            object_storage_access_key_secret=next_access_key_secret,
            connect_timeout_seconds=body.connect_timeout_seconds,
        )
        return _object_storage_settings_response(repository)

    @router.get("/local-image/content")
    def get_local_image_content(
        path: str = Query(..., min_length=1, description="本地图片文件绝对路径"),
    ) -> Response:
        content, mime_type = _read_local_image_for_preview(path)
        return Response(
            content=content,
            media_type=mime_type,
            headers={"Cache-Control": "private, max-age=60"},
        )

    @router.get("/{resource_id}", response_model=FileResourceResponse)
    def get_resource(resource_id: str) -> FileResourceResponse:
        service = get_file_resource_service()
        resource = service.get_resources([resource_id])[0]
        if resource is None:
            raise HTTPException(status_code=404, detail=f"资源不存在: {resource_id}")
        return _resource_response(resource, name=None)

    @router.get("/{resource_id}/content")
    def get_resource_content(resource_id: str) -> Response:
        service = get_file_resource_service()
        resource = service.get_resources([resource_id])[0]
        if resource is None:
            raise HTTPException(status_code=404, detail=f"资源不存在: {resource_id}")
        try:
            content = base64.b64decode(resource.resource_base64_content)
        except Exception as exc:
            raise HTTPException(status_code=500, detail="资源内容解码失败。") from exc
        return Response(content=content, media_type=resource.resource_mime_type)

    return router


def _read_local_image_for_preview(raw_path: str) -> tuple[bytes, str]:
    normalized = (raw_path or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="图片路径不能为空。")
    file_path = Path(normalized).expanduser()
    if not file_path.is_absolute():
        raise HTTPException(status_code=400, detail=f"本地图片路径必须为绝对路径: {normalized}")
    try:
        resolved_path = file_path.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"无效的本地图片路径: {normalized}") from exc

    if not resolved_path.exists():
        raise HTTPException(status_code=404, detail=f"图片文件不存在: {resolved_path}")
    if not resolved_path.is_file():
        raise HTTPException(status_code=400, detail=f"路径不是普通文件: {resolved_path}")

    try:
        file_size = resolved_path.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取图片文件状态失败: {resolved_path}") from exc
    if file_size <= 0:
        raise HTTPException(status_code=400, detail=f"图片文件内容为空: {resolved_path}")
    if file_size > MAX_LOCAL_IMAGE_PREVIEW_BYTES:
        raise HTTPException(status_code=413, detail=f"图片文件大小超过预览上限: {resolved_path}")

    try:
        content = resolved_path.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取图片文件失败: {resolved_path}") from exc

    mime_type = _detect_local_preview_image_mime_type(resolved_path, content)
    return content, mime_type


def _detect_local_preview_image_mime_type(path: Path, content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"BM") and len(content) >= 14:
        return "image/bmp"
    if len(content) >= 12 and content[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    if content.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"

    guessed_mime, _ = mimetypes.guess_type(str(path))
    if guessed_mime == "image/svg+xml" and b"<svg" in content[:1024].lower():
        return "image/svg+xml"

    raise HTTPException(status_code=400, detail=f"文件不是受支持的图片格式: {path}")


def _object_storage_settings_repository() -> ObjectStorageSettingsRepository:
    return create_object_storage_settings_repository(
        data_dir_accessor=lambda: ConfigRegistry.instance().file_resource_service.local_resource_storage_root_path,
    )


def _upload_task_response(snapshot: dict[str, Any]) -> FileResourceUploadTaskResponse:
    task_id = snapshot["task_id"]
    return FileResourceUploadTaskResponse(
        task_id=task_id,
        status=snapshot["status"],
        phase=snapshot["phase"],
        message=snapshot["message"],
        progress_percent=snapshot["progress_percent"],
        resources=snapshot.get("resources"),
        error_message=snapshot.get("error_message"),
        latest_seq=snapshot["latest_seq"],
        events_url=f"/api/file-resources/upload-tasks/{task_id}/events",
    )


def _format_upload_task_sse(event: FileResourceUploadTaskEvent) -> str:
    payload = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.seq}\nevent: file_resource_upload_task\ndata: {payload}\n\n"


def _object_storage_settings_response(
    repository: ObjectStorageSettingsRepository,
) -> ObjectStorageSettingsResponse:
    settings = repository.get_settings()
    return ObjectStorageSettingsResponse(
        gateway_provider=settings.gateway_provider,
        object_storage_endpoint=settings.object_storage_endpoint,
        object_storage_region=settings.object_storage_region,
        object_storage_bucket_name=settings.object_storage_bucket_name,
        object_storage_public_base_url=settings.object_storage_public_base_url,
        object_storage_object_key_prefix=settings.object_storage_object_key_prefix,
        object_storage_access_key_id=None,
        object_storage_access_key_secret=None,
        object_storage_access_key_id_configured=bool((settings.object_storage_access_key_id or "").strip()),
        object_storage_access_key_secret_configured=bool((settings.object_storage_access_key_secret or "").strip()),
        connect_timeout_seconds=settings.connect_timeout_seconds,
        db_path=str(repository.db_path()),
        updated_at=settings.updated_at,
    )


def _resource_response(resource, *, name: str | None) -> FileResourceResponse:
    metadata = resource.resource_metadata
    metadata_dict = _metadata_dict(metadata)
    return FileResourceResponse(
        resource_id=resource.resource_id,
        name=name,
        mime_type=resource.resource_mime_type,
        size_in_bytes=resource.resource_size_in_bytes,
        object_storage_url=resource.object_storage_url,
        local_storage_path=resource.local_storage_path,
        width=metadata_dict.get("width"),
        height=metadata_dict.get("height"),
        page_count=metadata_dict.get("page_count"),
        metadata=metadata_dict,
    )


def _metadata_dict(metadata) -> dict[str, Any]:
    if isinstance(metadata, ImageResourceMetadata):
        return {
            "resource_category": metadata.resource_category,
            "width": metadata.image_width,
            "height": metadata.image_height,
        }
    if isinstance(metadata, PdfResourceMetadata):
        return {
            "resource_category": metadata.resource_category,
            "page_count": metadata.pdf_page_count,
        }
    return {}

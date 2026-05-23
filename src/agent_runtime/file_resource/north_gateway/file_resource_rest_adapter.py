from __future__ import annotations

import base64
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from agent_runtime.config import ConfigRegistry
from agent_runtime.file_resource.models import ImageResourceMetadata, PdfResourceMetadata
from agent_runtime.file_resource.north_gateway.resource_create_contracts import Base64CreateRequest
from agent_runtime.file_resource.settings_repository import ObjectStorageSettingsRepository
from agent_runtime.file_resource.shared_service import get_file_resource_service
from agent_runtime.file_resource.south_gateway.object_storage.runtime_configured import (
    ObjectStorageConfigurationError,
)

logger = logging.getLogger(__name__)


class CreateFileResourceItem(BaseModel):
    name: str | None = None
    mime_type: str
    base64_content: str


class CreateFileResourcesBody(BaseModel):
    resources: list[CreateFileResourceItem] = Field(default_factory=list)


class FileResourceResponse(BaseModel):
    resource_id: str
    name: str | None = None
    mime_type: str
    size_in_bytes: int
    object_storage_url: str
    width: int | None = None
    height: int | None = None
    page_count: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateFileResourcesResponse(BaseModel):
    resources: list[FileResourceResponse]


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
    connect_timeout_seconds: int = 10


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

    @router.get("/object-storage-settings", response_model=ObjectStorageSettingsResponse)
    def get_object_storage_settings() -> ObjectStorageSettingsResponse:
        repository = _object_storage_settings_repository()
        return _object_storage_settings_response(repository)

    @router.put("/object-storage-settings", response_model=ObjectStorageSettingsResponse)
    def update_object_storage_settings(
        body: ObjectStorageSettingsUpdateBody,
    ) -> ObjectStorageSettingsResponse:
        if body.gateway_provider != "aliyun_oss" and body.gateway_provider != "memory":
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


def _object_storage_settings_repository() -> ObjectStorageSettingsRepository:
    return ObjectStorageSettingsRepository(
        data_dir_accessor=lambda: ConfigRegistry.instance().file_resource_service.local_resource_storage_root_path,
    )


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
        object_storage_access_key_id=settings.object_storage_access_key_id,
        object_storage_access_key_secret=settings.object_storage_access_key_secret,
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
        size_in_bytes=len(base64.b64decode(resource.resource_base64_content)),
        object_storage_url=resource.object_storage_url,
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

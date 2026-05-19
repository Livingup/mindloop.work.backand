from __future__ import annotations

import base64
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from agent_runtime.file_resource.models import ImageResourceMetadata, PdfResourceMetadata
from agent_runtime.file_resource.north_gateway.resource_create_contracts import Base64CreateRequest
from agent_runtime.file_resource.shared_service import get_file_resource_service

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

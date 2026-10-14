"""基于内存的对象存储南向网关实现。

该实现主要服务于开发和测试阶段，通过内存字典模拟外部对象存储服务，
以便在没有真实 OSS 服务时先把统一文件资源服务主流程跑通。
"""
from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.file_resource.south_gateway.object_storage.contracts import (
    ObjectStorageGateway,
    ObjectStorageUploadCancelledError,
    UploadedObjectStorageResource,
)


@dataclass(frozen=True, slots=True)
class StoredObjectContent:
    """对象存储内容快照（开发测试用）。"""

    resource_id: str
    resource_mime_type: str
    resource_content: bytes


class InMemoryObjectStorageGateway(ObjectStorageGateway):
    """基于内存的对象存储南向网关实现。"""

    def __init__(self, object_storage_public_base_url: str) -> None:
        self._object_storage_public_base_url = object_storage_public_base_url.rstrip("/")
        self._stored_content_by_id: dict[str, StoredObjectContent] = {}

    def upload_resource_content(
        self,
        resource_id: str,
        resource_content: bytes,
        resource_mime_type: str,
        *,
        progress_callback=None,
        cancel_checker=None,
    ) -> UploadedObjectStorageResource:
        """上传资源内容到内存对象存储网关。"""

        if cancel_checker is not None and cancel_checker():
            raise ObjectStorageUploadCancelledError("对象存储上传已取消。")
        self._stored_content_by_id[resource_id] = StoredObjectContent(
            resource_id=resource_id,
            resource_mime_type=resource_mime_type,
            resource_content=resource_content,
        )
        if progress_callback is not None:
            progress_callback(len(resource_content), len(resource_content))
        return UploadedObjectStorageResource(
            resource_id=resource_id,
            object_storage_url=f"{self._object_storage_public_base_url}/{resource_id}",
        )

    def delete_resource_content(self, resource_id: str) -> None:
        """删除内存对象存储中的资源内容。"""

        self._stored_content_by_id.pop(resource_id, None)

    def get_stored_content(self, resource_id: str) -> StoredObjectContent | None:
        """读取已上传的内容快照（面向测试校验，非北向能力）。"""

        return self._stored_content_by_id.get(resource_id)

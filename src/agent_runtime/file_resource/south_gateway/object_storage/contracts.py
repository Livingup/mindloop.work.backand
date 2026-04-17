"""对象存储南向网关接口契约。

该模块定义统一文件资源服务面向外部对象存储服务时的稳定协作契约。
任何具体对象存储厂商实现都必须在该接口后面完成适配，
避免把厂商 SDK 语义直接带入资源管理流程。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class UploadedObjectStorageResource:
    """对象存储上传结果。

    该模型用于表达资源内容上传到外部对象存储服务后，
    从南向网关层获得的标准化结果。
    """

    # 统一文件资源标识。
    resource_id: str

    # 对象存储访问地址。
    object_storage_url: str


class ObjectStorageGateway(Protocol):
    """对象存储南向网关接口。

    该接口定义统一文件资源服务面向外部对象存储服务时的稳定协作契约。
    """

    def upload_resource_content(
        self,
        resource_id: str,
        resource_content: bytes,
        resource_mime_type: str,
    ) -> UploadedObjectStorageResource:
        """上传资源内容到外部对象存储服务。"""

    def delete_resource_content(self, resource_id: str) -> None:
        """删除外部对象存储中的资源内容。"""

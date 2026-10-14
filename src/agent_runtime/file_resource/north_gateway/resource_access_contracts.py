"""统一文件资源访问业务北向契约。

该模块定义统一文件资源服务"资源访问"业务域对外暴露的结果模型和服务接口。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent_runtime.file_resource.models import FileResourceMetadata


@dataclass(frozen=True, slots=True)
class FileResourceAccessResult:
    """统一文件资源访问结果。

    调用方可基于该结果获取本地路径、Base64 内容、对象存储地址和结构化元数据。
    """

    # 统一文件资源唯一标识。
    resource_id: str

    # 资源本地存储路径。
    local_storage_path: str

    # 资源 Base64 内容。
    resource_base64_content: str

    # 资源 MIME 类型。
    resource_mime_type: str

    resource_size_in_bytes: int
    # 资源对象存储访问地址。
    object_storage_url: str

    # 资源元数据（图片宽高或 PDF 页数）。
    resource_metadata: FileResourceMetadata


class FileResourceAccessNorthGateway(Protocol):
    """统一文件资源访问业务北向网关接口。"""

    def get_resources(
        self,
        resource_ids: list[str],
    ) -> list[FileResourceAccessResult | None]:
        """按资源标识批量获取资源访问结果。"""

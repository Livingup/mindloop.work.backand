"""统一文件资源内部领域模型。

该模块集中定义统一文件资源服务的核心领域类型，包括：
- MIME 类型常量与资源类别常量
- 资源元数据模型（图片宽高、PDF 页数）
- 资源档案主模型
- 资源档案仓储接口与内存实现
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final, Protocol, TypeAlias


# ── MIME 类型常量 ──────────────────────────────────────────────

SUPPORTED_IMAGE_MIME_TYPES: Final[set[str]] = {
    "image/jpeg",
    "image/png",
    "image/webp",
}

SUPPORTED_DOCUMENT_MIME_TYPES: Final[set[str]] = {
    "application/pdf",
}

SUPPORTED_MIME_TYPES: Final[set[str]] = (
    SUPPORTED_IMAGE_MIME_TYPES | SUPPORTED_DOCUMENT_MIME_TYPES
)

MIME_TYPE_TO_EXTENSION: Final[dict[str, str]] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "application/pdf": "pdf",
}


# ── 资源元数据模型 ─────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ImageResourceMetadata:
    """图片资源元数据。

    该模型用于表达统一文件资源服务从图片二进制内容中解析出的结构化属性。
    """

    # 图片宽度，单位为像素。
    image_width: int

    # 图片高度，单位为像素。
    image_height: int

    # 资源类别固定为 image。
    resource_category: str = field(default="image", init=False)


@dataclass(frozen=True, slots=True)
class PdfResourceMetadata:
    """PDF 资源元数据。

    该模型用于表达统一文件资源服务从 PDF 二进制内容中解析出的结构化属性。
    """

    # PDF 总页数。
    pdf_page_count: int

    # 资源类别固定为 pdf。
    resource_category: str = field(default="pdf", init=False)


# 统一文件资源元数据联合类型。
FileResourceMetadata: TypeAlias = ImageResourceMetadata | PdfResourceMetadata


# ── 资源档案主模型 ─────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class FileResourceArchive:
    """统一文件资源档案。

    该模型用于表达一个文件资源在统一文件资源服务中的主档记录，
    描述资源身份、存储位置、类型以及元数据。
    """

    # 统一文件资源唯一标识。
    resource_id: str

    # 资源内容 SHA-256 哈希（十六进制小写，64 字符），去重键。
    resource_content_hash: str

    # 资源类别（image / pdf）。
    resource_category: str

    # 资源 MIME 类型。
    resource_mime_type: str

    # 资源大小，单位为字节。
    resource_size_in_bytes: int

    # 资源本地存储路径。
    local_storage_path: str

    # 资源对象存储访问地址。
    object_storage_url: str

    # 资源元数据（图片宽高或 PDF 页数）。
    resource_metadata: FileResourceMetadata

    # 资源建档时间，ISO 8601 格式。
    resource_created_at_iso8601: str

    @classmethod
    def create_new(
        cls,
        resource_id: str,
        resource_content_hash: str,
        resource_category: str,
        resource_mime_type: str,
        resource_size_in_bytes: int,
        local_storage_path: str,
        object_storage_url: str,
        resource_metadata: FileResourceMetadata,
    ) -> FileResourceArchive:
        """创建新的统一文件资源档案，自动生成建档时间。"""

        return cls(
            resource_id=resource_id,
            resource_content_hash=resource_content_hash,
            resource_category=resource_category,
            resource_mime_type=resource_mime_type,
            resource_size_in_bytes=resource_size_in_bytes,
            local_storage_path=local_storage_path,
            object_storage_url=object_storage_url,
            resource_metadata=resource_metadata,
            resource_created_at_iso8601=datetime.now(timezone.utc).isoformat(),
        )


# ── 档案仓储接口 ──────────────────────────────────────────────

class FileResourceArchiveRepository(Protocol):
    """统一文件资源档案仓储接口。

    该接口抽象档案的持久化与查询能力，让资源管理流程不直接依赖具体存储技术。
    """

    def get_archive_by_id(self, resource_id: str) -> FileResourceArchive | None:
        """根据资源标识获取档案。"""

    def get_archive_by_content_hash(
        self,
        content_hash: str,
    ) -> FileResourceArchive | None:
        """根据内容哈希获取档案（用于去重判断）。"""

    def save_archive(self, archive: FileResourceArchive) -> None:
        """保存档案。"""

    def delete_archive(self, resource_id: str) -> None:
        """删除档案。"""


class InMemoryFileResourceArchiveRepository:
    """基于内存的统一文件资源档案仓储实现。

    主要用于开发和测试场景，通过内存字典维护档案主记录与内容哈希索引。
    """

    def __init__(self) -> None:
        self._archive_by_id: dict[str, FileResourceArchive] = {}
        self._resource_id_by_hash: dict[str, str] = {}

    def get_archive_by_id(self, resource_id: str) -> FileResourceArchive | None:
        """根据资源标识获取档案。"""

        return self._archive_by_id.get(resource_id)

    def get_archive_by_content_hash(
        self,
        content_hash: str,
    ) -> FileResourceArchive | None:
        """根据内容哈希获取档案。"""

        resource_id = self._resource_id_by_hash.get(content_hash)
        if resource_id is None:
            return None
        return self._archive_by_id.get(resource_id)

    def save_archive(self, archive: FileResourceArchive) -> None:
        """保存档案并同步维护内容哈希索引。"""

        self._archive_by_id[archive.resource_id] = archive
        self._resource_id_by_hash[archive.resource_content_hash] = archive.resource_id

    def delete_archive(self, resource_id: str) -> None:
        """删除档案并移除对应内容哈希索引。"""

        archive = self._archive_by_id.pop(resource_id, None)
        if archive is not None:
            self._resource_id_by_hash.pop(archive.resource_content_hash, None)

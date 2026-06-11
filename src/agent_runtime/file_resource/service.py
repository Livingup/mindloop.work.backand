"""统一文件资源管理服务。

该模块是统一文件资源服务的核心编排入口，负责：
- 从本地文件 / Base64 创建统一文件资源
- 按资源标识访问统一文件资源
- 按资源标识删除统一文件资源

内部统一编排资源接入、内容哈希去重、元数据解析、
本地内容落盘、对象存储上传、资源档案建档和资源删除协同流程。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

from agent_runtime.file_resource.local_storage import LocalResourceStorage
from agent_runtime.file_resource.metadata_parser import FileResourceMetadataParser
from agent_runtime.file_resource.models import (
    FileResourceArchive,
    FileResourceArchiveRepository,
)
from agent_runtime.file_resource.north_gateway.resource_access_contracts import (
    FileResourceAccessResult,
)
from agent_runtime.file_resource.north_gateway.resource_create_contracts import (
    Base64CreateRequest,
    FileResourceCreateResult,
    LocalFileCreateRequest,
    RemoteUrlCreateRequest,
)

from agent_runtime.file_resource.south_gateway.object_storage.contracts import (
    ObjectStorageGateway,
    ObjectStorageUploadCancelledError,
)

logger = logging.getLogger(__name__)

FileResourceProgressCallback = Callable[[str, str, int, int | None, int | None], None]
FileResourceCancelChecker = Callable[[], bool]


class FileResourceCancelledError(RuntimeError):
    pass


class FileResourceService:
    """统一文件资源管理服务。

    该服务是统一文件资源系统的核心编排入口，实现北向网关定义的全部业务能力：
    - 资源创建：从本地文件路径或 Base64 内容批量接入新资源，完成哈希去重、
      元数据解析、本地落盘、对象存储上传和档案建档的完整业务流程。
    - 资源访问：按资源标识批量获取资源的本地路径、Base64 内容、对象存储地址和元数据。
    - 资源删除：按资源标识批量物理删除资源，清除档案、本地文件和对象存储内容。
    """

    def __init__(
        self,
        archive_repository: FileResourceArchiveRepository,
        local_storage: LocalResourceStorage,
        object_storage_gateway: ObjectStorageGateway,
        metadata_parser: FileResourceMetadataParser | None = None,
        batch_limit: int = 20,
    ) -> None:
        # 资源档案仓储。该仓储负责资源主档记录的持久化与查询，
        # 是资源去重判断和资源检索的数据基础。
        self._archive_repository = archive_repository

        # 本地资源存储服务。该服务负责把资源二进制内容按哈希分片规则落盘到本地文件系统，
        # 为资源访问提供热缓存读取能力。
        self._local_storage = local_storage

        # 对象存储南向网关。该网关负责把资源内容上传到外部对象存储服务（如阿里云 OSS），
        # 为资源提供持久化远端存储和跨设备共享访问能力。
        self._object_storage_gateway = object_storage_gateway

        # 资源元数据解析器。该解析器负责校验 MIME 类型、判断资源类别（image / pdf），
        # 并从资源二进制内容中提取结构化元数据（图片宽高、PDF 页数）。
        self._metadata_parser = metadata_parser or FileResourceMetadataParser()

        # 单次批量处理上限。统一文件资源服务对所有批量操作施加的数量上限，
        # 防止单次请求处理过多资源导致内存或 I/O 压力过大。
        self._batch_limit = batch_limit

    # ── 北向能力：资源创建 ─────────────────────────────────────

    def create_resources_from_local_files(
        self,
        create_requests: list[LocalFileCreateRequest],
    ) -> list[FileResourceCreateResult]:
        """从本地文件批量创建统一文件资源。

        调用方提供一组本地文件路径和对应的 MIME 类型，
        服务逐一读取文件内容并走完资源接入全流程（哈希去重 → 元数据解析 →
        本地落盘 → 对象存储上传 → 建档），返回与输入顺序一一对应的创建结果。
        """

        self._assert_valid_batch_size(len(create_requests))
        return [
            self._create_from_local_file(req)
            for req in create_requests
        ]

    def create_resources_from_base64_contents(
        self,
        create_requests: list[Base64CreateRequest],
        *,
        progress_callback: FileResourceProgressCallback | None = None,
        cancel_checker: FileResourceCancelChecker | None = None,
    ) -> list[FileResourceCreateResult]:
        """从 Base64 内容批量创建统一文件资源。

        调用方直接提交 Base64 编码的资源内容和 MIME 类型，
        服务解码后走完与本地文件创建相同的资源接入全流程，
        返回与输入顺序一一对应的创建结果。
        """

        self._assert_valid_batch_size(len(create_requests))
        results: list[FileResourceCreateResult] = []
        for req in create_requests:
            self._ensure_not_cancelled(cancel_checker)
            results.append(
                self._create_from_base64(
                    req,
                    progress_callback=progress_callback,
                    cancel_checker=cancel_checker,
                )
            )
        return results

    def create_resources_from_remote_urls(
        self,
        create_requests: list[RemoteUrlCreateRequest],
        *,
        progress_callback: FileResourceProgressCallback | None = None,
        cancel_checker: FileResourceCancelChecker | None = None,
    ) -> list[FileResourceCreateResult]:
        """从远程 http/https URL 批量创建统一文件资源。

        如果 URL 已经是本服务已知的对象存储 URL，则直接复用对应资源；否则下载内容后
        复用统一接入流程完成 MIME 校验、哈希去重、本地缓存、对象存储上传和建档。
        """

        self._assert_valid_batch_size(len(create_requests))
        results: list[FileResourceCreateResult] = []
        for req in create_requests:
            self._ensure_not_cancelled(cancel_checker)
            results.append(
                self._create_from_remote_url(
                    req,
                    progress_callback=progress_callback,
                    cancel_checker=cancel_checker,
                )
            )
        return results

    # ── 北向能力：资源访问 ─────────────────────────────────────

    def get_resources(
        self,
        resource_ids: list[str],
    ) -> list[FileResourceAccessResult | None]:
        """按资源标识批量获取资源访问结果。

        返回与输入顺序一一对应的访问结果列表。
        对于不存在的资源标识，对应位置返回 None。
        每个访问结果包含本地路径、Base64 内容、对象存储地址和结构化元数据。
        """

        self._assert_valid_batch_size(len(resource_ids))
        return [self._get_single_resource(rid) for rid in resource_ids]

    def get_resources_by_object_storage_urls(
        self,
        object_storage_urls: list[str],
    ) -> list[FileResourceAccessResult | None]:
        """按对象存储访问 URL 批量获取资源访问结果。"""

        self._assert_valid_batch_size(len(object_storage_urls))
        return [self._get_single_resource_by_object_storage_url(url) for url in object_storage_urls]

    # ── 北向能力：资源删除 ─────────────────────────────────────

    def delete_resources(self, resource_ids: list[str]) -> None:
        """按资源标识批量删除统一文件资源。

        删除采用物理删除语义，资源不存在时静默跳过。
        每一步独立 try-except，确保单步失败不阻塞后续清理。
        """

        self._assert_valid_batch_size(len(resource_ids))
        for rid in resource_ids:
            self._delete_single_resource(rid)

    # ── 内部：资源接入 ─────────────────────────────────────────

    def _create_from_local_file(
        self,
        request: LocalFileCreateRequest,
    ) -> FileResourceCreateResult:
        """处理单个本地文件资源接入请求。

        从调用方指定的本地文件路径读取二进制内容，
        交由 _ingest_content 执行统一的资源接入与建档流程。
        """

        content = Path(request.local_file_path).read_bytes()
        archive = self._ingest_content(content, request.resource_mime_type)
        return self._to_create_result(archive)

    def _create_from_base64(
        self,
        request: Base64CreateRequest,
        *,
        progress_callback: FileResourceProgressCallback | None = None,
        cancel_checker: FileResourceCancelChecker | None = None,
    ) -> FileResourceCreateResult:
        """处理单个 Base64 资源接入请求。

        将调用方提交的 Base64 字符串解码为二进制内容，
        交由 _ingest_content 执行统一的资源接入与建档流程。
        Base64 解码失败时抛出 ValueError 并携带原始错误信息。
        """

        self._ensure_not_cancelled(cancel_checker)
        self._emit_progress(progress_callback, "decoding", "正在解码文件内容。", 5)
        try:
            content = base64.b64decode(request.resource_base64_content)
        except Exception as e:
            raise ValueError(
                f"Base64 内容解码失败: {e}"
            ) from e
        self._ensure_not_cancelled(cancel_checker)
        archive = self._ingest_content(
            content,
            request.resource_mime_type,
            progress_callback=progress_callback,
            cancel_checker=cancel_checker,
        )
        return self._to_create_result(archive)

    def _create_from_remote_url(
        self,
        request: RemoteUrlCreateRequest,
        *,
        progress_callback: FileResourceProgressCallback | None = None,
        cancel_checker: FileResourceCancelChecker | None = None,
    ) -> FileResourceCreateResult:
        """处理单个远程 URL 资源接入请求。"""

        remote_url = _normalize_remote_resource_url(request.resource_url)
        existing = self._archive_repository.get_archive_by_object_storage_url(remote_url)
        if existing is not None:
            self._emit_progress(progress_callback, "completed", "URL 已命中现有资源，复用已有档案。", 100)
            return self._to_create_result(existing)
        self._ensure_not_cancelled(cancel_checker)
        self._emit_progress(progress_callback, "downloading", "正在下载远程文件内容。", 5)
        content, mime_type = _download_remote_resource(
            remote_url,
            declared_mime_type=request.resource_mime_type,
            max_bytes=request.max_bytes,
        )
        self._ensure_not_cancelled(cancel_checker)
        archive = self._ingest_content(
            content,
            mime_type,
            progress_callback=progress_callback,
            cancel_checker=cancel_checker,
        )
        return self._to_create_result(archive)

    def _ingest_content(
        self,
        content: bytes,
        mime_type: str,
        *,
        progress_callback: FileResourceProgressCallback | None = None,
        cancel_checker: FileResourceCancelChecker | None = None,
    ) -> FileResourceArchive:
        """接入资源内容并执行统一建档。

        完整业务顺序：
        1. 校验 MIME 类型
        2. 计算内容哈希
        3. 去重判断
        4. 解析元数据
        5. 写入本地文件系统
        6. 上传对象存储
        7. 创建并保存档案

        如果 OSS 上传失败，会尝试回滚已写入的本地文件。
        """

        self._ensure_not_cancelled(cancel_checker)
        validated_mime = self._metadata_parser.assert_supported_mime_type(mime_type)
        content_hash = hashlib.sha256(content).hexdigest()

        # 去重
        existing = self._archive_repository.get_archive_by_content_hash(content_hash)
        if existing is not None:
            self._emit_progress(progress_callback, "completed", "资源已存在，复用已有档案。", 100)
            return existing

        self._ensure_not_cancelled(cancel_checker)
        self._emit_progress(progress_callback, "processing", "正在解析文件元数据。", 12)
        category = self._metadata_parser.determine_resource_category(validated_mime)
        metadata = self._metadata_parser.parse_metadata(content, validated_mime)

        # 本地落盘
        self._ensure_not_cancelled(cancel_checker)
        self._emit_progress(progress_callback, "local_storing", "正在写入本地缓存。", 18)
        local_result = self._local_storage.store_resource_content(
            resource_category=category,
            resource_content_hash=content_hash,
            resource_mime_type=validated_mime,
            resource_content=content,
        )
        self._ensure_not_cancelled(cancel_checker)
        self._emit_progress(progress_callback, "local_stored", "本地缓存写入完成。", 25)

        # OSS 上传（失败时回滚本地文件）
        resource_id = str(uuid.uuid4())
        try:
            self._emit_progress(progress_callback, "oss_uploading", "正在上传到 OSS。", 30, 0, len(content))
            oss_result = self._object_storage_gateway.upload_resource_content(
                resource_id=resource_id,
                resource_content=content,
                resource_mime_type=validated_mime,
                progress_callback=lambda uploaded, total: self._emit_progress(
                    progress_callback,
                    "oss_uploading",
                    f"正在上传到 OSS: {uploaded}/{total} bytes。",
                    30 + int((uploaded / total) * 60) if total > 0 else 30,
                    uploaded,
                    total,
                ),
                cancel_checker=cancel_checker,
            )
        except ObjectStorageUploadCancelledError as exc:
            self._local_storage.delete_resource_content(local_result.local_storage_path)
            raise FileResourceCancelledError("文件上传已取消。") from exc
        except Exception:
            logger.warning(
                "OSS 上传失败，回滚本地文件: %s",
                local_result.local_storage_path,
                exc_info=True,
            )
            self._local_storage.delete_resource_content(local_result.local_storage_path)
            raise

        # 建档
        try:
            self._ensure_not_cancelled(cancel_checker)
        except FileResourceCancelledError:
            self._local_storage.delete_resource_content(local_result.local_storage_path)
            self._object_storage_gateway.delete_resource_content(resource_id)
            raise
        self._emit_progress(progress_callback, "archiving", "正在创建资源档案。", 95)
        archive = FileResourceArchive.create_new(
            resource_id=resource_id,
            resource_content_hash=content_hash,
            resource_category=category,
            resource_mime_type=validated_mime,
            resource_size_in_bytes=len(content),
            local_storage_path=local_result.local_storage_path,
            object_storage_url=oss_result.object_storage_url,
            resource_metadata=metadata,
        )
        self._archive_repository.save_archive(archive)
        self._emit_progress(progress_callback, "completed", "资源建档完成。", 100)
        return archive

    # ── 内部：资源访问 ─────────────────────────────────────────

    def _get_single_resource(self, resource_id: str) -> FileResourceAccessResult | None:
        """获取单个资源的完整访问结果。

        先从档案仓储读取资源主档记录，再从本地文件系统读取资源二进制内容并转为 Base64，
        最终组装包含本地路径、Base64 内容、对象存储地址和元数据的访问结果返回给调用方。
        资源不存在时返回 None。
        """

        archive = self._archive_repository.get_archive_by_id(resource_id)
        if archive is None:
            return None

        content = self._local_storage.read_resource_content(archive.local_storage_path)
        b64_content = base64.b64encode(content).decode("utf-8")
        return FileResourceAccessResult(
            resource_id=archive.resource_id,
            local_storage_path=archive.local_storage_path,
            resource_base64_content=b64_content,
            resource_mime_type=archive.resource_mime_type,
            resource_size_in_bytes=archive.resource_size_in_bytes,
            object_storage_url=archive.object_storage_url,
            resource_metadata=archive.resource_metadata,
        )

    def _get_single_resource_by_object_storage_url(self, object_storage_url: str) -> FileResourceAccessResult | None:
        archive = self._archive_repository.get_archive_by_object_storage_url(object_storage_url)
        if archive is None:
            return None
        return self._get_single_resource(archive.resource_id)

    # ── 内部：资源删除 ─────────────────────────────────────────

    def _delete_single_resource(self, resource_id: str) -> None:
        """删除单个统一文件资源。

        先删档案（阻止新访问），再清理存储。每步独立 try-except，
        确保单步失败不阻塞后续清理，避免孤儿资源。
        """

        archive = self._archive_repository.get_archive_by_id(resource_id)
        if archive is None:
            return

        # 先删档案，阻止后续访问
        try:
            self._archive_repository.delete_archive(resource_id)
        except Exception:
            logger.warning("删除档案失败: resource_id=%s", resource_id, exc_info=True)

        # 再删本地文件
        try:
            self._local_storage.delete_resource_content(archive.local_storage_path)
        except Exception:
            logger.warning(
                "删除本地文件失败: %s", archive.local_storage_path, exc_info=True
            )

        # 最后删 OSS
        try:
            self._object_storage_gateway.delete_resource_content(resource_id)
        except Exception:
            logger.warning("删除 OSS 对象失败: resource_id=%s", resource_id, exc_info=True)

    # ── 内部：工具方法 ─────────────────────────────────────────

    def _to_create_result(self, archive: FileResourceArchive) -> FileResourceCreateResult:
        """将资源档案主记录转换为北向创建结果。

        从内部档案模型中提取调用方关心的字段（资源标识、对象存储地址、本地路径），
        封装为北向契约定义的创建结果返回。
        """

        return FileResourceCreateResult(
            resource_id=archive.resource_id,
            object_storage_url=archive.object_storage_url,
            local_storage_path=archive.local_storage_path,
        )

    def _assert_valid_batch_size(self, count: int) -> None:
        """校验批量请求数量是否在允许范围内。

        不允许空批次请求（count == 0），也不允许超过服务配置的单次批量上限。
        违规时抛出 ValueError 并携带具体数量信息。
        """

        if count == 0:
            raise ValueError("统一文件资源服务不允许空批次请求。")
        if count > self._batch_limit:
            raise ValueError(
                f"统一文件资源服务批量请求超过上限: {count} > {self._batch_limit}"
            )

    def _ensure_not_cancelled(self, cancel_checker: FileResourceCancelChecker | None) -> None:
        if cancel_checker is not None and cancel_checker():
            raise FileResourceCancelledError("文件上传已取消。")

    def _emit_progress(
        self,
        progress_callback: FileResourceProgressCallback | None,
        phase: str,
        message: str,
        progress_percent: int,
        uploaded_bytes: int | None = None,
        total_bytes: int | None = None,
    ) -> None:
        if progress_callback is not None:
            progress_callback(phase, message, progress_percent, uploaded_bytes, total_bytes)


def _normalize_remote_resource_url(resource_url: str) -> str:
    url = (resource_url or "").strip()
    if not url:
        raise ValueError("远程文件 URL 不能为空。")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("远程文件 URL 仅支持 http 或 https。")
    if not parsed.netloc:
        raise ValueError("远程文件 URL 缺少主机名。")
    return url


def _download_remote_resource(
    resource_url: str,
    *,
    declared_mime_type: str | None,
    max_bytes: int | None,
) -> tuple[bytes, str]:
    if max_bytes is not None and max_bytes <= 0:
        raise ValueError("远程文件下载大小上限必须大于 0。")
    request = urllib.request.Request(
        resource_url,
        headers={"User-Agent": "AgentRuntimeFileResource/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            mime_type = _resolve_remote_mime_type(
                declared_mime_type,
                response.headers.get("Content-Type"),
                resource_url,
            )
            if mime_type not in {"image/jpeg", "image/png", "image/webp", "application/pdf"}:
                raise ValueError(f"不支持的远程文件 MIME 类型: {mime_type}")
            length_header = response.headers.get("Content-Length")
            if max_bytes is not None and length_header:
                try:
                    content_length = int(length_header)
                except ValueError:
                    content_length = 0
                if content_length > max_bytes:
                    raise ValueError(
                        f"远程文件大小 {content_length} bytes 超过上限 {max_bytes} bytes。"
                    )
            chunks: list[bytes] = []
            downloaded = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if max_bytes is not None and downloaded > max_bytes:
                    raise ValueError(
                        f"远程文件大小超过上限 {max_bytes} bytes，已停止下载。"
                    )
                chunks.append(chunk)
    except urllib.error.URLError as exc:
        raise ValueError(f"远程文件下载失败: {exc}") from exc
    content = b"".join(chunks)
    if not content:
        raise ValueError("远程文件内容为空。")
    return content, mime_type


def _resolve_remote_mime_type(
    declared_mime_type: str | None,
    response_content_type: str | None,
    resource_url: str,
) -> str:
    candidates = [
        declared_mime_type,
        (response_content_type or "").split(";", 1)[0].strip() or None,
        mimetypes.guess_type(urllib.parse.urlparse(resource_url).path)[0],
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate).strip().lower()
    raise ValueError("无法确定远程文件 MIME 类型。")

"""面向 Agent 的多模态图片文件读取与保存工具（不经过 FileResourceService）。

本模块提供两个与统一文件资源服务（FileResourceService）解耦的图片文件工具：
- `image_read`：批量读取远程 http(s):// 图片 URL 或本地图片文件绝对路径，以多模态图片输出项
  写入 ToolResultPart.output，使下一轮 LLM 请求通过 `<multimodal_file ...>` 标签包裹的
  `input_image` 块直接读取图片视觉内容与位置信息。
- `image_save`：批量将远程 http(s):// 图片 URL 下载并原子保存到项目工作区（project_root）内
  的目标文件路径，默认支持覆盖已有普通文件。
"""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid

from pydantic import BaseModel, Field

from agent_runtime.conversation_message.message.tool_result_output import normalize_tool_result_output
from agent_runtime.file_tools.file_edit_tools import (
    FileEditBusinessError,
    _ensure_existing_regular_file,
    _ensure_parent_directory,
    _prepare_workspace_target,
    _runtime_metadata,
    _sha256_bytes,
)
from agent_runtime.infrastructure.asyncio_diagnostics import BlockingPool
from agent_runtime.tool.tool_decorator import tool
from agent_runtime.tool.tool_execution_models import BaseToolRequest, BaseToolResult, ToolRuntimeContext


MAX_IMAGE_BATCH_COUNT = 20
MAX_IMAGE_BYTES = 50 * 1024 * 1024
IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 30
SUPPORTED_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
_EXTENSION_TO_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class ImageReadRequest(BaseToolRequest):
    """批量多模态图片读取请求。"""

    images: list[str] = Field(
        min_length=1,
        max_length=MAX_IMAGE_BATCH_COUNT,
        description=(
            f"要读取的图片列表；一次最多 {MAX_IMAGE_BATCH_COUNT} 项。"
            "每项可以是远程 http(s):// 图片 URL，也可以是本地图片文件的绝对路径。"
        ),
    )


class ImageSaveItem(BaseModel):
    """单张远程图片下载保存项。"""

    url: str = Field(min_length=1, description="要下载保存的远程图片 http(s):// URL。")
    file_path: str = Field(
        min_length=1,
        description="目标保存文件路径；可以是位于 project_root 内的绝对路径，也可以是相对 project_root 的路径。",
    )
    overwrite: bool = Field(default=True, description="目标文件已存在时是否允许覆盖；默认允许覆盖。")
    create_parent_directories: bool = Field(default=True, description="目标父目录不存在时是否自动创建；默认自动创建。")


class ImageSaveRequest(BaseToolRequest):
    """批量远程图片下载保存请求。"""

    project_root: str = Field(description="项目工作区根目录，所有目标保存路径必须位于该目录内。")
    items: list[ImageSaveItem] = Field(
        min_length=1,
        max_length=MAX_IMAGE_BATCH_COUNT,
        description=f"要批量下载保存的图片列表；一次最多 {MAX_IMAGE_BATCH_COUNT} 项。",
    )


def _detect_magic_image_mime_type(content: bytes) -> str | None:
    """根据文件头魔数识别图片 MIME 类型。"""

    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def _guess_image_mime_from_name(url_or_path: str) -> str | None:
    """根据路径或 URL 后缀推断图片 MIME 类型。"""

    suffix = Path(url_or_path).suffix.lower()
    if suffix in _EXTENSION_TO_IMAGE_MIME:
        return _EXTENSION_TO_IMAGE_MIME[suffix]
    guessed, _ = mimetypes.guess_type(url_or_path)
    if guessed and guessed.lower() in SUPPORTED_IMAGE_MIME_TYPES:
        return guessed.lower()
    return None


def _normalize_remote_http_url(raw_url: str) -> str:
    """校验并规范化远程 http(s) URL。"""

    url = (raw_url or "").strip()
    if not url:
        raise FileEditBusinessError("InvalidImageUrl", "远程图片 URL 不能为空。")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise FileEditBusinessError("InvalidImageUrl", f"远程图片 URL 仅支持 http 或 https: {url}")
    if not parsed.netloc:
        raise FileEditBusinessError("InvalidImageUrl", f"远程图片 URL 缺少主机名: {url}")
    return url


def _detect_local_image_mime_type(path: Path, header: bytes) -> str:
    """校验本地文件是否为受支持的图片格式并返回 MIME 类型。"""

    magic_mime = _detect_magic_image_mime_type(header)
    if magic_mime in SUPPORTED_IMAGE_MIME_TYPES:
        return magic_mime
    guessed_mime = _guess_image_mime_from_name(str(path))
    if guessed_mime in SUPPORTED_IMAGE_MIME_TYPES and b"\x00" not in header[:16]:
        # 若文件既无图片魔数也不包含二进制特征，则拒绝将纯文本伪装文件当作图片读取。
        pass
    if guessed_mime in SUPPORTED_IMAGE_MIME_TYPES and not header.startswith((b"<!DOCTYPE", b"<html", b"{", b"[")):
        return guessed_mime
    raise FileEditBusinessError(
        "UnsupportedImageType",
        f"文件不是受支持的图片格式（仅支持 png/jpeg/webp/gif）: {path}",
    )


def _resolve_downloaded_image_mime_type(
    content: bytes,
    response_content_type: str | None,
    resource_url: str,
    target_path: Path,
) -> str:
    """综合文件魔数、HTTP Content-Type、URL 路径与目标路径后缀识别图片 MIME 类型。"""

    magic_mime = _detect_magic_image_mime_type(content)
    if magic_mime in SUPPORTED_IMAGE_MIME_TYPES:
        return magic_mime

    header_mime = (response_content_type or "").split(";", 1)[0].strip().lower()
    if header_mime == "image/jpg":
        header_mime = "image/jpeg"
    if header_mime in SUPPORTED_IMAGE_MIME_TYPES:
        return header_mime
    if header_mime and header_mime not in {"application/octet-stream", "binary/octet-stream"}:
        raise FileEditBusinessError(
            "UnsupportedImageType",
            f"远程响应 Content-Type 不是受支持的图片类型: {header_mime} (url={resource_url})",
        )

    url_path = urllib.parse.urlparse(resource_url).path
    for candidate_name in (url_path, str(target_path)):
        guessed = _guess_image_mime_from_name(candidate_name)
        if guessed in SUPPORTED_IMAGE_MIME_TYPES:
            return guessed

    raise FileEditBusinessError(
        "UnsupportedImageType",
        f"无法识别远程文件为受支持的图片格式（仅支持 png/jpeg/webp/gif）: {resource_url}",
    )


def _download_remote_image(
    raw_url: str,
    *,
    target_path: Path,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> tuple[str, bytes, str]:
    """下载远程图片并校验大小与图片格式。"""

    normalized_url = _normalize_remote_http_url(raw_url)
    req = urllib.request.Request(
        normalized_url,
        headers={"User-Agent": "AgentRuntimeImageTool/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=IMAGE_DOWNLOAD_TIMEOUT_SECONDS) as response:
            response_content_type = response.headers.get("Content-Type") if response.headers else None
            length_header = response.headers.get("Content-Length") if response.headers else None
            if length_header:
                try:
                    content_length = int(length_header)
                except ValueError:
                    content_length = 0
                if content_length > max_bytes:
                    raise FileEditBusinessError(
                        "ImageFileTooLarge",
                        f"远程图片大小 {content_length} bytes 超过上限 {max_bytes} bytes: {normalized_url}",
                    )
            chunks: list[bytes] = []
            downloaded = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    raise FileEditBusinessError(
                        "ImageFileTooLarge",
                        f"远程图片大小超过上限 {max_bytes} bytes，已停止下载: {normalized_url}",
                    )
                chunks.append(chunk)
    except FileEditBusinessError:
        raise
    except urllib.error.URLError as exc:
        raise FileEditBusinessError(
            "ImageDownloadFailed",
            f"远程图片下载失败: {normalized_url}: {exc}",
        ) from exc
    except OSError as exc:
        raise FileEditBusinessError(
            "ImageDownloadFailed",
            f"远程图片下载失败: {normalized_url}: {exc}",
        ) from exc

    content = b"".join(chunks)
    if not content:
        raise FileEditBusinessError("EmptyImageContent", f"远程图片内容为空: {normalized_url}")

    mime_type = _resolve_downloaded_image_mime_type(
        content,
        response_content_type,
        normalized_url,
        target_path,
    )
    return normalized_url, content, mime_type


def _write_bytes_atomically(path: Path, content: bytes) -> None:
    """使用同目录临时文件 + os.replace 原子写入二进制文件。"""

    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(str(temp_path), str(path))
    except PermissionError as exc:
        raise FileEditBusinessError("WritePermissionDenied", f"没有权限写入文件: {path}") from exc
    except OSError as exc:
        raise FileEditBusinessError("AtomicReplaceFailed", f"原子写入文件失败: {path}: {exc}") from exc
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _read_single_image_item(raw_item: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """校验单个图片输入项并生成 ToolResultPart 输出块与预览元数据。"""

    value = (raw_item or "").strip()
    if not value:
        raise FileEditBusinessError("EmptyImageSource", "图片 URL 或本地文件路径不能为空。")

    if "://" in value:
        normalized_url = _normalize_remote_http_url(value)
        guessed_mime = _guess_image_mime_from_name(urllib.parse.urlparse(normalized_url).path)
        output_part: dict[str, Any] = {"kind": "image", "url": normalized_url}
        if guessed_mime:
            output_part["mime_type"] = guessed_mime
        preview_item = {"source_type": "url", "url": normalized_url}
        if guessed_mime:
            preview_item["mime_type"] = guessed_mime
        return output_part, preview_item

    raw_path = Path(value).expanduser()
    if not raw_path.is_absolute():
        raise FileEditBusinessError(
            "RelativePathNotAllowed",
            f"本地图片路径必须为绝对路径: {value}",
        )
    resolved_path = raw_path.resolve()
    if not resolved_path.exists():
        raise FileEditBusinessError("PathNotFound", f"图片文件不存在: {resolved_path}")
    if not resolved_path.is_file():
        raise FileEditBusinessError("NotAFile", f"路径不是普通文件: {resolved_path}")

    try:
        file_size = resolved_path.stat().st_size
        if file_size <= 0:
            raise FileEditBusinessError("EmptyImageFile", f"图片文件内容为空: {resolved_path}")
        if file_size > MAX_IMAGE_BYTES:
            raise FileEditBusinessError(
                "ImageFileTooLarge",
                f"图片文件大小 {file_size} bytes 超过上限 {MAX_IMAGE_BYTES} bytes: {resolved_path}",
            )
        with resolved_path.open("rb") as file:
            header = file.read(32)
    except FileEditBusinessError:
        raise
    except OSError as exc:
        raise FileEditBusinessError("ReadError", f"读取图片文件失败: {resolved_path}: {exc}") from exc

    mime_type = _detect_local_image_mime_type(resolved_path, header)
    canonical_path = str(resolved_path)
    return (
        {
            "kind": "image",
            "path": canonical_path,
            "mime_type": mime_type,
        },
        {
            "source_type": "path",
            "file_path": canonical_path,
            "mime_type": mime_type,
            "size_in_bytes": file_size,
        },
    )


@tool(
    name="image_read",
    description=(
        f"批量读取一个或多个图片（支持 http(s):// 远程图片 URL 或本地图片文件绝对路径，一次最多 {MAX_IMAGE_BATCH_COUNT} 项），"
        "将图片以多模态 input_image 内容块及 `<multimodal_file type=\"image\" ...>` 标签返回给模型。"
        "当消息文本中仅有图片 URL 或本地图片路径而模型需要直接查看图片内容时调用此工具；不经过资源系统建档。"
    ),
    group="file",
    category="business",
    execution_mode="blocking",
    blocking_pool=BlockingPool.FILE_RESOURCE,
    blocking_operation="tool.file.image_read",
)
def image_read(request: ImageReadRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    """批量校验图片 URL 或本地绝对路径，并构造多模态图片工具结果。"""

    output_parts: list[dict[str, Any]] = []
    preview_items: list[dict[str, Any]] = []
    failed_items: list[dict[str, Any]] = []

    for index, raw_item in enumerate(request.images):
        try:
            part, preview = _read_single_image_item(raw_item)
            output_parts.append(part)
            preview_items.append(preview)
        except FileEditBusinessError as exc:
            failed_items.append(
                {
                    "index": index,
                    "source": raw_item,
                    "error_code": exc.error_code,
                    "error_message": exc.error_message,
                }
            )

    if failed_items:
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="ImageReadBatchFailed",
            error_message=f"{len(failed_items)} 个图片读取失败。",
            output={
                "failed_items": failed_items,
                "succeeded_count": len(output_parts),
                "failed_count": len(failed_items),
            },
            metadata=_runtime_metadata(ctx, "image_read"),
        )

    return BaseToolResult(
        success=True,
        execution_status="success",
        output=normalize_tool_result_output(output_parts),
        metadata={
            **_runtime_metadata(ctx, "image_read"),
            "ui_kind": "image_read",
            "preview_output": {"images": preview_items},
        },
    )


@tool(
    name="image_save",
    description=(
        f"批量将远程 http(s):// 图片 URL 下载保存到项目工作区（project_root）内的指定文件路径（一次最多 {MAX_IMAGE_BATCH_COUNT} 项）。"
        "目标路径必须位于 project_root 内，默认支持覆盖已存在的同名文件并自动创建父目录；不经过资源系统建档。"
    ),
    group="file",
    category="business",
    execution_mode="blocking",
    blocking_pool=BlockingPool.FILE_RESOURCE,
    blocking_operation="tool.file.image_save",
)
def image_save(request: ImageSaveRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    """批量下载远程图片并原子保存到工作区目标路径。"""

    project_root_path = Path(request.project_root).expanduser().resolve()
    output_base: dict[str, Any] = {"project_root": str(project_root_path)}
    if not project_root_path.exists():
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="ProjectRootNotFound",
            error_message=f"项目根目录不存在: {project_root_path}",
            output=output_base,
            metadata=_runtime_metadata(ctx, "image_save"),
        )
    if not project_root_path.is_dir():
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="ProjectRootNotADirectory",
            error_message=f"项目根路径不是目录: {project_root_path}",
            output=output_base,
            metadata=_runtime_metadata(ctx, "image_save"),
        )

    item_results: list[dict[str, Any]] = []
    failed_items: list[dict[str, Any]] = []

    for index, item in enumerate(request.items):
        item_base: dict[str, Any] = {
            "index": index,
            "url": item.url,
            "file_path": item.file_path,
        }
        try:
            target = _prepare_workspace_target(request.project_root, item.file_path, allow_missing=True)
            item_base.update(
                {
                    "file_path": str(target.path),
                    "relative_path": target.relative_path.as_posix(),
                }
            )
            overwriting = target.path.exists()
            if overwriting:
                _ensure_existing_regular_file(target, directory_error_code="DirectoryOverwriteDenied")
                if not item.overwrite:
                    raise FileEditBusinessError(
                        "FileAlreadyExists",
                        f"文件已存在且 overwrite=false: {target.path}",
                    )

            normalized_url, content, mime_type = _download_remote_image(
                item.url,
                target_path=target.path,
                max_bytes=MAX_IMAGE_BYTES,
            )
            parent_directories = _ensure_parent_directory(
                target,
                create_parent_directories=item.create_parent_directories,
                dry_run=False,
            )
            _write_bytes_atomically(target.path, content)
            item_results.append(
                {
                    **item_base,
                    "url": normalized_url,
                    "success": True,
                    "mime_type": mime_type,
                    "size_in_bytes": len(content),
                    "content_sha256": _sha256_bytes(content),
                    "created": not overwriting,
                    "overwritten": overwriting,
                    "created_parent_directory_count": len(parent_directories),
                }
            )
        except FileEditBusinessError as exc:
            failure_entry = {
                **item_base,
                "success": False,
                "error_code": exc.error_code,
                "error_message": exc.error_message,
            }
            item_results.append(failure_entry)
            failed_items.append(failure_entry)

    output = {
        **output_base,
        "items": item_results,
        "succeeded_count": len(item_results) - len(failed_items),
        "failed_count": len(failed_items),
    }
    if failed_items:
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="ImageSaveBatchFailed",
            error_message=f"{len(failed_items)} 个图片保存失败。",
            output=output,
            metadata=_runtime_metadata(ctx, "image_save"),
        )

    return BaseToolResult(
        success=True,
        execution_status="success",
        output=output,
        metadata=_runtime_metadata(ctx, "image_save"),
    )

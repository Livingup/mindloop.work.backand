"""面向 Agent 的受控文件编辑工具。

本模块承载 file_tools 业务域中的写入类工具，提供项目工作区内的文本修改、
文件创建、文件删除以及文件移动/重命名能力。写入能力必须比只读工具拥有更严格
的业务边界：所有目标路径都必须位于调用方声明的 project_root 内，并且默认拒绝
.git 目录、符号链接路径和不安全的覆盖操作。
"""
from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent_runtime.file_tools.file_tools import ALWAYS_EXCLUDED_NAMES, _encoding_candidates, _is_binary_file
from agent_runtime.tool.tool_decorator import tool
from agent_runtime.tool.tool_execution_models import BaseToolRequest, BaseToolResult, ToolRuntimeContext


LineEndingName = Literal["LF", "CRLF", "CR"]


class FileModifyEdit(BaseModel):
    """单个连续行替换编辑块。

    Agent 必须提供自己已读取过的原始连续代码行作为 search_lines，工具只在唯一命中
    时执行替换，避免模糊修改或误改多个相似代码片段。
    """

    feature_desc: str = Field(min_length=1, description="本次编辑服务的业务需求或修改目的；长度必须至少 1 个字符。")
    search_lines: list[str] = Field(min_length=1, description="要精确匹配的原始连续文本行；列表长度必须至少 1 行，每个元素代表一行且不包含换行符。")
    replace_lines: list[str] = Field(default_factory=list, description="替换后的完整文本行；每个元素代表一行且不包含换行符。")


class FileModifyRequest(BaseToolRequest):
    """文本文件内容修改请求。

    file_modify 只负责文本内容修改，不负责创建、删除、移动文件。修改方式固定为
    连续行精确匹配替换，以保证 Agent 修改具有可审计、可回放和失败可解释的边界。
    """

    project_root: str = Field(description="项目工作区根目录，所有写入目标必须位于该目录内。")
    file_path: str = Field(description="要修改的文本文件路径；可以是绝对路径，也可以是相对 project_root 的路径。")
    edits: list[FileModifyEdit] = Field(min_length=1, description="按顺序应用的连续行替换编辑列表；列表长度必须至少 1 个编辑块。")
    expected_content_sha256: str | None = Field(default=None, description="可选的修改前文件内容 SHA256，用于防止覆盖并行修改。")
    encoding: str | None = Field(default=None, description="可选的文件编码；为空时自动尝试常见文本编码。")
    dry_run: bool = Field(default=False, description="为 true 时只校验并计算是否可修改，不实际写入文件。")


class FileCreateRequest(BaseToolRequest):
    """文本文件创建请求。"""

    project_root: str = Field(description="项目工作区根目录，创建目标必须位于该目录内。")
    file_path: str = Field(description="要创建的文本文件路径；可以是绝对路径，也可以是相对 project_root 的路径。")
    content_lines: list[str] = Field(default_factory=list, description="新文件内容行；每个元素代表一行且不包含换行符。")
    encoding: str = Field(default="utf-8", description="写入文件时使用的文本编码。")
    line_ending: LineEndingName = Field(default="LF", description="写入文件时使用的换行风格；可选值仅限 LF、CRLF、CR。")
    final_newline: bool = Field(default=True, description="是否在非空内容末尾追加结尾换行。")
    overwrite: bool = Field(default=False, description="是否允许覆盖已有文件；默认禁止。覆盖时必须提供 expected_content_sha256。")
    expected_content_sha256: str | None = Field(default=None, description="覆盖已有文件时必须提供的旧文件内容 SHA256。")
    create_parent_directories: bool = Field(default=True, description="父目录不存在时是否自动创建。")
    dry_run: bool = Field(default=False, description="为 true 时只校验并计算是否可创建或覆盖，不实际写入文件。")


class FileDeleteRequest(BaseToolRequest):
    """单文件删除请求。"""

    project_root: str = Field(description="项目工作区根目录，删除目标必须位于该目录内。")
    file_path: str = Field(description="要删除的文件路径；可以是绝对路径，也可以是相对 project_root 的路径。")
    reason: str = Field(min_length=1, description="删除文件的业务原因，用于审计和防止误删；长度必须至少 1 个字符。")
    expected_content_sha256: str | None = Field(default=None, description="可选的删除前文件内容 SHA256，用于防止删除并行修改后的文件。")
    dry_run: bool = Field(default=False, description="为 true 时只返回将要删除的文件信息，不实际删除。")


class FileMoveRequest(BaseToolRequest):
    """单文件移动或重命名请求。

    重命名是同目录下的移动，因此统一由 file_move 承载，避免 file_move/file_rename
    两个工具表达重复能力。
    """

    project_root: str = Field(description="项目工作区根目录，源路径和目标路径都必须位于该目录内。")
    source_path: str = Field(description="要移动或重命名的源文件路径；可以是绝对路径，也可以是相对 project_root 的路径。")
    target_path: str = Field(description="移动或重命名后的目标文件路径；可以是绝对路径，也可以是相对 project_root 的路径。")
    reason: str = Field(min_length=1, description="移动或重命名文件的业务原因，用于审计和防止误操作；长度必须至少 1 个字符。")
    expected_source_sha256: str | None = Field(default=None, description="可选的源文件内容 SHA256，用于防止移动并行修改后的文件。")
    overwrite: bool = Field(default=False, description="是否允许覆盖目标文件；默认禁止。覆盖时必须提供 expected_target_sha256。")
    expected_target_sha256: str | None = Field(default=None, description="覆盖目标文件时必须提供的目标文件旧内容 SHA256。")
    create_parent_directories: bool = Field(default=True, description="目标父目录不存在时是否自动创建。")
    dry_run: bool = Field(default=False, description="为 true 时只返回将要移动的文件信息，不实际移动。")


@dataclass(frozen=True)
class WorkspaceTarget:
    """经过工作区安全策略校验后的文件路径。

    path 是最终用于文件系统操作的规范路径；relative_path 是面向审计和前端展示
    的项目内相对路径，避免写工具误操作项目外文件。
    """

    project_root: Path
    path: Path
    relative_path: Path


class FileEditBusinessError(Exception):
    """文件编辑工具可预期业务错误。

    这类错误会转换为 BaseToolResult 的失败结果，而不是让工具框架捕获为未知异常。
    """

    def __init__(self, error_code: str, error_message: str, output: dict[str, Any] | None = None) -> None:
        super().__init__(error_message)
        self.error_code = error_code
        self.error_message = error_message
        self.output = output or {}


def _build_failure(error: FileEditBusinessError, output_base: dict[str, Any]) -> BaseToolResult:
    """将文件编辑业务错误转换为统一工具失败结果。"""

    output = dict(output_base)
    output.update(error.output)
    return BaseToolResult(
        success=False,
        execution_status="failed",
        error_code=error.error_code,
        error_message=error.error_message,
        output=output,
    )


def _runtime_metadata(ctx: ToolRuntimeContext, tool_name: str, reason: str | None = None) -> dict[str, Any]:
    """构建写工具审计元数据，记录当前 Agent 调用链路。"""

    metadata: dict[str, Any] = {
        "tool_name": tool_name,
        "request_id": ctx.request_id,
        "task_id": ctx.task_id,
        "agent_instance_id": ctx.agent_instance_id,
        "trace_id": ctx.trace_id,
        "communication_stream_id": ctx.communication_stream_id,
    }
    if reason is not None:
        metadata["reason"] = reason
    return metadata


def _prepare_workspace_target(project_root_value: str, path_value: str, *, allow_missing: bool) -> WorkspaceTarget:
    """校验并解析项目工作区内的目标路径。

    写入类工具的核心安全规则集中在这里：路径必须位于 project_root 内，不能触碰 .git
    目录，且路径链路中的既有组件不能是符号链接，防止 Agent 通过软链越权修改项目外文件。
    """

    project_root = Path(project_root_value).expanduser().resolve()
    if not project_root.exists():
        raise FileEditBusinessError("ProjectRootNotFound", f"项目根目录不存在: {project_root}")
    if not project_root.is_dir():
        raise FileEditBusinessError("ProjectRootNotADirectory", f"项目根路径不是目录: {project_root}")

    raw_path = Path(path_value).expanduser()
    if not raw_path.is_absolute():
        raw_path = project_root / raw_path
    lexical_path = Path(os.path.abspath(raw_path))
    resolved_path = lexical_path.resolve(strict=False)

    try:
        lexical_relative = lexical_path.relative_to(project_root)
        relative_path = resolved_path.relative_to(project_root)
    except ValueError as exc:
        raise FileEditBusinessError(
            "PathOutsideWorkspace",
            f"文件路径必须位于项目根目录内: path={resolved_path}, project_root={project_root}",
        ) from exc

    if any(part in ALWAYS_EXCLUDED_NAMES for part in relative_path.parts):
        raise FileEditBusinessError("GitDirectoryModificationDenied", f"禁止修改受保护目录内的文件: {resolved_path}")

    current = project_root
    for part in lexical_relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise FileEditBusinessError("SymlinkOperationDenied", f"写入类工具默认拒绝操作符号链接路径: {current}")

    if not allow_missing and not resolved_path.exists():
        raise FileEditBusinessError("PathNotFound", f"文件不存在: {resolved_path}")

    return WorkspaceTarget(project_root=project_root, path=resolved_path, relative_path=relative_path)


def _ensure_existing_regular_file(target: WorkspaceTarget, *, directory_error_code: str) -> None:
    """确认目标是可操作的普通文件，目录类操作在第一版写工具中默认禁止。"""

    if target.path.is_dir():
        raise FileEditBusinessError(directory_error_code, f"当前工具不支持操作目录: {target.path}")
    if not target.path.is_file():
        raise FileEditBusinessError("NotAFile", f"路径不是普通文件: {target.path}")


def _ensure_parent_directory(target: WorkspaceTarget, *, create_parent_directories: bool, dry_run: bool) -> list[str]:
    """确认目标父目录存在，必要时按请求创建父目录。"""

    parent = target.path.parent
    if parent.exists():
        if not parent.is_dir():
            raise FileEditBusinessError("ParentPathNotADirectory", f"目标父路径不是目录: {parent}")
        return []

    missing_parents: list[Path] = []
    current = parent
    while not current.exists() and current != target.project_root.parent:
        missing_parents.append(current)
        current = current.parent
    missing_parents.reverse()

    if not create_parent_directories:
        raise FileEditBusinessError("ParentDirectoryNotFound", f"目标父目录不存在: {parent}")
    if not dry_run:
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise FileEditBusinessError("WritePermissionDenied", f"没有权限创建父目录: {parent}") from exc
        except OSError as exc:
            raise FileEditBusinessError("CreateParentDirectoryFailed", f"创建父目录失败: {parent}: {exc}") from exc
    return [str(path) for path in missing_parents]


def _normalize_sha256(value: str) -> str:
    """标准化调用方传入的 SHA256 字符串，兼容 sha256: 前缀。"""

    normalized = value.strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized.removeprefix("sha256:")
    return normalized


def _sha256_bytes(content: bytes) -> str:
    """计算字节内容的 SHA256。"""

    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    """以流式方式计算文件内容 SHA256，避免大文件一次性读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_expected_sha256(path: Path, expected_content_sha256: str | None) -> str:
    """校验文件当前内容是否仍是 Agent 读取时的版本。"""

    current_sha256 = _sha256_file(path)
    if expected_content_sha256 is not None and current_sha256 != _normalize_sha256(expected_content_sha256):
        raise FileEditBusinessError(
            "HashMismatch",
            f"文件内容 SHA256 不匹配，说明文件可能已被并行修改，请重新读取后再操作: {path}",
            output={"current_content_sha256": current_sha256, "expected_content_sha256": expected_content_sha256},
        )
    return current_sha256


def _read_text_content(path: Path, encoding: str | None) -> tuple[str, str]:
    """按现有 file_read 的编码候选策略读取完整文本内容。"""

    last_error: UnicodeDecodeError | LookupError | None = None
    for candidate in _encoding_candidates(encoding):
        try:
            with path.open("r", encoding=candidate, errors="strict", newline="") as file:
                return file.read(), candidate
        except (UnicodeDecodeError, LookupError) as exc:
            last_error = exc
    message = "文件编码无法识别。"
    if last_error is not None:
        message = f"文件编码无法识别: {last_error}"
    raise FileEditBusinessError("DecodeError", message)


def _detect_newline_info(text: str) -> dict[str, Any]:
    """识别文本换行风格和结尾换行规则。"""

    crlf_count = text.count("\r\n")
    without_crlf = text.replace("\r\n", "")
    lf_count = without_crlf.count("\n")
    cr_count = without_crlf.count("\r")
    used_styles = [name for name, count in (("CRLF", crlf_count), ("LF", lf_count), ("CR", cr_count)) if count > 0]
    if not used_styles:
        style = "NONE"
        sequence = "\n"
    elif len(used_styles) == 1:
        style = used_styles[0]
        sequence = _line_ending_sequence(style)  # type: ignore[arg-type]
    else:
        style = "MIXED"
        sequence = _line_ending_sequence(used_styles[0])  # type: ignore[arg-type]
    return {
        "newline": style,
        "newline_sequence": sequence,
        "newline_counts": {"CRLF": crlf_count, "LF": lf_count, "CR": cr_count},
        "has_final_newline": text.endswith(("\n", "\r")),
    }


def _line_ending_sequence(line_ending: LineEndingName | str) -> str:
    """将换行风格名称转换为实际换行字符。"""

    if line_ending == "CRLF":
        return "\r\n"
    if line_ending == "CR":
        return "\r"
    return "\n"


def _render_text(lines: list[str], newline_sequence: str, *, final_newline: bool) -> str:
    """按指定换行规则渲染文本行。"""

    if not lines:
        return ""
    text = newline_sequence.join(lines)
    if final_newline:
        text += newline_sequence
    return text


def _find_block_matches(lines: list[str], search_lines: list[str]) -> list[int]:
    """查找连续行块在当前文件内容中的所有起始行下标。"""

    search_size = len(search_lines)
    if search_size > len(lines):
        return []
    return [index for index in range(0, len(lines) - search_size + 1) if lines[index : index + search_size] == search_lines]


def _write_text_atomically(path: Path, text: str, encoding: str) -> None:
    """用临时文件 + os.replace 原子写入文本，避免半写入破坏目标文件。"""

    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding=encoding, errors="strict", newline="") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(str(temp_path), str(path))
    except PermissionError as exc:
        raise FileEditBusinessError("WritePermissionDenied", f"没有权限写入文件: {path}") from exc
    except LookupError as exc:
        raise FileEditBusinessError("InvalidEncoding", f"无效的文件编码: {encoding}") from exc
    except OSError as exc:
        raise FileEditBusinessError("AtomicReplaceFailed", f"原子写入文件失败: {path}: {exc}") from exc
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


@tool(
    name="file_modify",
    description="在项目工作区内通过连续行精确匹配修改文本文件内容；适用于 Agent 小粒度、安全、可审计地修改源码或配置文件。",
    group="file",
    category="system",
)
def file_modify(request: FileModifyRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    """基于 search_lines/replace_lines 对文本文件执行受控修改。"""

    output_base = {"project_root": request.project_root, "file_path": request.file_path, "dry_run": request.dry_run}
    try:
        target = _prepare_workspace_target(request.project_root, request.file_path, allow_missing=False)
        output_base.update({"project_root": str(target.project_root), "file_path": str(target.path), "relative_path": target.relative_path.as_posix()})
        _ensure_existing_regular_file(target, directory_error_code="DirectoryModificationDenied")
        if _is_binary_file(target.path):
            raise FileEditBusinessError("BinaryFileNotEditable", f"文件可能是二进制文件，默认不修改: {target.path}")

        old_sha256 = _validate_expected_sha256(target.path, request.expected_content_sha256)
        old_text, used_encoding = _read_text_content(target.path, request.encoding)
        newline_info = _detect_newline_info(old_text)
        if newline_info["newline"] == "MIXED":
            raise FileEditBusinessError("MixedNewlineUnsupported", f"文件存在混合换行符，拒绝自动修改以避免格式扩大化变更: {target.path}", output={"newline_counts": newline_info["newline_counts"]})

        current_lines = old_text.splitlines()
        applied_edits: list[dict[str, Any]] = []
        for edit in request.edits:
            matches = _find_block_matches(current_lines, edit.search_lines)
            if not matches:
                raise FileEditBusinessError(
                    "SearchBlockNotFound",
                    f"未找到要替换的连续行块: {edit.feature_desc}",
                    output={"feature_desc": edit.feature_desc, "search_line_count": len(edit.search_lines)},
                )
            if len(matches) > 1:
                raise FileEditBusinessError(
                    "SearchBlockNotUnique",
                    f"要替换的连续行块匹配到多处，请提供更长上下文: {edit.feature_desc}",
                    output={"feature_desc": edit.feature_desc, "matched_start_lines": [index + 1 for index in matches[:20]], "match_count": len(matches)},
                )
            start_index = matches[0]
            end_index = start_index + len(edit.search_lines)
            current_lines = current_lines[:start_index] + edit.replace_lines + current_lines[end_index:]
            applied_edits.append(
                {
                    "feature_desc": edit.feature_desc,
                    "start_line": start_index + 1,
                    "end_line": end_index,
                    "deleted_lines": len(edit.search_lines),
                    "inserted_lines": len(edit.replace_lines),
                }
            )

        new_text = _render_text(current_lines, newline_info["newline_sequence"], final_newline=newline_info["has_final_newline"])
        new_sha256 = _sha256_bytes(new_text.encode(used_encoding))
        modified = old_text != new_text
        if modified and not request.dry_run:
            _write_text_atomically(target.path, new_text, used_encoding)

        output = {
            **output_base,
            "operation": "modify",
            "old_content_sha256": old_sha256,
            "new_content_sha256": new_sha256,
            "modified": modified,
            "written": modified and not request.dry_run,
            "would_modify": modified and request.dry_run,
            "edit_count": len(applied_edits),
        }
        return BaseToolResult(output=output, metadata=_runtime_metadata(ctx, "file_modify"))
    except FileEditBusinessError as exc:
        return _build_failure(exc, output_base)


@tool(
    name="file_create",
    description="在项目工作区内创建新的文本文件；默认禁止覆盖已有文件，覆盖时必须携带旧文件 SHA256。",
    group="file",
    category="system",
)
def file_create(request: FileCreateRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    """创建文本文件，必要时在受控边界内创建父目录。"""

    output_base = {"project_root": request.project_root, "file_path": request.file_path, "dry_run": request.dry_run}
    try:
        target = _prepare_workspace_target(request.project_root, request.file_path, allow_missing=True)
        output_base.update({"project_root": str(target.project_root), "file_path": str(target.path), "relative_path": target.relative_path.as_posix()})
        old_sha256: str | None = None
        overwriting = target.path.exists()
        if overwriting:
            _ensure_existing_regular_file(target, directory_error_code="DirectoryOverwriteDenied")
            if not request.overwrite:
                raise FileEditBusinessError("FileAlreadyExists", f"文件已存在，默认禁止覆盖: {target.path}")
            if request.expected_content_sha256 is None:
                raise FileEditBusinessError("ExpectedHashRequiredForOverwrite", f"覆盖已有文件时必须提供 expected_content_sha256: {target.path}")
            if _is_binary_file(target.path):
                raise FileEditBusinessError("BinaryFileNotEditable", f"目标文件可能是二进制文件，默认不覆盖: {target.path}")
            old_sha256 = _validate_expected_sha256(target.path, request.expected_content_sha256)

        parent_directories = _ensure_parent_directory(target, create_parent_directories=request.create_parent_directories, dry_run=request.dry_run)
        new_text = _render_text(request.content_lines, _line_ending_sequence(request.line_ending), final_newline=request.final_newline)
        try:
            new_bytes = new_text.encode(request.encoding)
        except LookupError as exc:
            raise FileEditBusinessError("InvalidEncoding", f"无效的文件编码: {request.encoding}") from exc
        new_sha256 = _sha256_bytes(new_bytes)

        if not request.dry_run:
            if target.path.exists() and not request.overwrite:
                raise FileEditBusinessError("FileAlreadyExists", f"文件已存在，默认禁止覆盖: {target.path}")
            _write_text_atomically(target.path, new_text, request.encoding)

        output = {
            **output_base,
            "operation": "create",
            "created": not overwriting and not request.dry_run,
            "overwritten": overwriting and not request.dry_run,
            "would_create": not overwriting and request.dry_run,
            "would_overwrite": overwriting and request.dry_run,
            "old_content_sha256": old_sha256,
            "new_content_sha256": new_sha256,
            "created_parent_directory_count": len(parent_directories),
        }
        return BaseToolResult(output=output, metadata=_runtime_metadata(ctx, "file_create"))
    except FileEditBusinessError as exc:
        return _build_failure(exc, output_base)


@tool(
    name="file_delete",
    description="在项目工作区内删除单个文件；不支持目录或递归删除，必须提供删除原因。",
    group="file",
    category="system",
)
def file_delete(request: FileDeleteRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    """删除单个文件，目录删除和递归删除不属于本工具边界。"""

    output_base = {"project_root": request.project_root, "file_path": request.file_path, "dry_run": request.dry_run, "reason": request.reason}
    try:
        target = _prepare_workspace_target(request.project_root, request.file_path, allow_missing=False)
        output_base.update({"project_root": str(target.project_root), "file_path": str(target.path), "relative_path": target.relative_path.as_posix()})
        _ensure_existing_regular_file(target, directory_error_code="DirectoryDeletionDenied")
        old_sha256 = _validate_expected_sha256(target.path, request.expected_content_sha256)

        if not request.dry_run:
            try:
                target.path.unlink()
            except PermissionError as exc:
                raise FileEditBusinessError("WritePermissionDenied", f"没有权限删除文件: {target.path}") from exc
            except OSError as exc:
                raise FileEditBusinessError("FileDeleteFailed", f"删除文件失败: {target.path}: {exc}") from exc

        output = {
            **output_base,
            "operation": "delete",
            "deleted": not request.dry_run,
            "would_delete": request.dry_run,
            "old_content_sha256": old_sha256,
        }
        return BaseToolResult(output=output, metadata=_runtime_metadata(ctx, "file_delete", request.reason))
    except FileEditBusinessError as exc:
        return _build_failure(exc, output_base)


@tool(
    name="file_move",
    description="在项目工作区内移动或重命名单个文件；rename 作为同目录移动处理，默认禁止覆盖目标文件。",
    group="file",
    category="system",
)
def file_move(request: FileMoveRequest, ctx: ToolRuntimeContext) -> BaseToolResult:
    """移动或重命名单个文件，统一承载 rename 和 move 两类路径变更。"""

    output_base = {
        "project_root": request.project_root,
        "source_path": request.source_path,
        "target_path": request.target_path,
        "dry_run": request.dry_run,
        "reason": request.reason,
    }
    try:
        source = _prepare_workspace_target(request.project_root, request.source_path, allow_missing=False)
        target = _prepare_workspace_target(request.project_root, request.target_path, allow_missing=True)
        output_base.update(
            {
                "project_root": str(source.project_root),
                "source_path": str(source.path),
                "target_path": str(target.path),
                "source_relative_path": source.relative_path.as_posix(),
                "target_relative_path": target.relative_path.as_posix(),
            }
        )
        if source.path == target.path:
            raise FileEditBusinessError("SamePath", f"源路径和目标路径相同，无需移动: {source.path}")
        _ensure_existing_regular_file(source, directory_error_code="DirectoryMoveDenied")
        source_sha256 = _validate_expected_sha256(source.path, request.expected_source_sha256)

        target_sha256: str | None = None
        target_exists = target.path.exists()
        if target_exists:
            _ensure_existing_regular_file(target, directory_error_code="TargetDirectoryOverwriteDenied")
            if not request.overwrite:
                raise FileEditBusinessError("FileAlreadyExists", f"目标文件已存在，默认禁止覆盖: {target.path}")
            if request.expected_target_sha256 is None:
                raise FileEditBusinessError("ExpectedHashRequiredForOverwrite", f"覆盖目标文件时必须提供 expected_target_sha256: {target.path}")
            target_sha256 = _validate_expected_sha256(target.path, request.expected_target_sha256)
        elif request.expected_target_sha256 is not None:
            raise FileEditBusinessError("TargetHashProvidedButMissing", f"目标文件不存在，不应提供 expected_target_sha256: {target.path}")

        parent_directories = _ensure_parent_directory(target, create_parent_directories=request.create_parent_directories, dry_run=request.dry_run)
        operation_kind = "rename" if source.path.parent == target.path.parent else "move"

        if not request.dry_run:
            try:
                if request.overwrite:
                    os.replace(str(source.path), str(target.path))
                else:
                    source.path.rename(target.path)
            except PermissionError as exc:
                raise FileEditBusinessError("WritePermissionDenied", f"没有权限移动文件: {source.path} -> {target.path}") from exc
            except OSError as exc:
                raise FileEditBusinessError("FileMoveFailed", f"移动文件失败: {source.path} -> {target.path}: {exc}") from exc

        output = {
            **output_base,
            "operation_kind": operation_kind,
            "moved": not request.dry_run,
            "would_move": request.dry_run,
            "overwritten": target_exists and request.overwrite and not request.dry_run,
            "would_overwrite": target_exists and request.overwrite and request.dry_run,
            "source_content_sha256": source_sha256,
            "target_old_content_sha256": target_sha256,
            "created_parent_directory_count": len(parent_directories),
        }
        return BaseToolResult(output=output, metadata=_runtime_metadata(ctx, "file_move", request.reason))
    except FileEditBusinessError as exc:
        return _build_failure(exc, output_base)

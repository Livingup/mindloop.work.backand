from __future__ import annotations

import fnmatch
import hashlib
import json
import locale
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterator, Literal

from pydantic import BaseModel, Field

from agent_runtime.infrastructure.asyncio_diagnostics import BlockingPool
from agent_runtime.subprocess_utils import hidden_subprocess_kwargs
from agent_runtime.tool.tool_decorator import tool
from agent_runtime.tool.tool_execution_models import BaseToolRequest, BaseToolResult


MAX_FILE_READ_LINE_COUNT = 1500
MAX_FILE_READ_FILES = 5
MAX_FILE_READ_MAX_CHARS_PER_LINE = 2000
MAX_LINE_SLICE_CHARS = 50000


class FileReadRange(BaseModel):
    start_line: int = Field(default=1, ge=1, description="1-based 起始行号；取值必须大于等于 1。")
    line_count: int = Field(default=200, ge=1, le=MAX_FILE_READ_LINE_COUNT, description=f"最多读取的行数；取值范围为 1 到 {MAX_FILE_READ_LINE_COUNT}。")


class FileReadFileRequest(BaseModel):
    file_path: str = Field(description="要读取的文件绝对路径或当前进程可解析的文件路径。")
    ranges: list[FileReadRange] = Field(
        default_factory=lambda: [FileReadRange()],
        min_length=1,
        description="要读取的行范围列表；每个范围独立返回结构化 lines 数组。单文件单段读取时传一个文件项和一个范围即可。",
    )
    encoding: str | None = Field(default=None, description="可选的文件编码；为空时自动尝试常见文本编码。")


class FileReadRequest(BaseToolRequest):
    files: list[FileReadFileRequest] = Field(
        min_length=1,
        max_length=MAX_FILE_READ_FILES,
        description=f"要读取的文件列表；一次最多 {MAX_FILE_READ_FILES} 个文件。读取单个文件时传入只包含一个元素的列表。",
    )


class FileReadLineSliceRequest(BaseToolRequest):
    file_path: str = Field(description="要读取大行内容的文件绝对路径或当前进程可解析的文件路径。")
    line_number: int = Field(ge=1, description="1-based 目标行号；取值必须大于等于 1。")
    char_start: int = Field(ge=1, description=f"1-based 行内起始字符位置；取值必须大于等于 1。每次调用由工具内部固定最多返回 {MAX_LINE_SLICE_CHARS} 个字符，不能由 Agent 调整。")
    encoding: str | None = Field(default=None, description="可选的文件编码；为空时自动尝试常见文本编码。")


class ContextDocumentDiscoverRequest(BaseToolRequest):
    project_root: str = Field(description="要查找上下文文档的项目根目录。")


class DirectoryExploreRequest(BaseToolRequest):
    directory_path: str = Field(description="要探索的目录路径。")
    depth: int = Field(default=1, ge=0, le=3, description="递归探索深度；取值范围为 0 到 3。")
    max_entries: int = Field(default=100, ge=1, le=300, description="最多返回的目录项数量；取值范围为 1 到 300。")
    include_files: bool = Field(default=True, description="是否返回文件条目。")
    include_hidden: bool = Field(default=False, description="是否包含隐藏文件或隐藏目录。")
    exclude_names: list[str] = Field(default_factory=list, description="额外排除的文件名或目录名。")
    follow_symlinks: bool = Field(default=False, description="是否跟随符号链接目录。")
    respect_gitignore: bool = Field(default=True, description="是否按项目 .gitignore 规则过滤遍历路径。")


class FileNameSearchRequest(BaseToolRequest):
    search_root: str = Field(description="文件名搜索根目录。")
    name_query: str | None = Field(default=None, description="文件名包含匹配关键词，只匹配 basename。")
    glob_pattern: str | None = Field(default=None, description="相对 search_root 的 glob 路径模式。")
    regex_pattern: str | None = Field(default=None, description="文件名正则表达式，只匹配 basename。")
    extensions: list[str] = Field(default_factory=list, description="扩展名过滤，不要求包含点号。")
    entry_type: Literal["file", "directory", "any"] = Field(default="file", description="匹配文件、目录或任意条目；可选值仅限 file、directory、any。")
    case_sensitive: bool = Field(default=False, description="是否大小写敏感。")
    max_results: int = Field(default=50, ge=1, le=200, description="本次最多返回的匹配数量；取值范围为 1 到 200。")
    offset: int = Field(default=0, ge=0, description="结果分页偏移；取值必须大于等于 0。")
    exclude_names: list[str] = Field(default_factory=list, description="额外排除的文件名或目录名。")
    include_hidden: bool = Field(default=False, description="是否包含隐藏文件或隐藏目录。")
    respect_gitignore: bool = Field(default=True, description="是否按项目 .gitignore 规则过滤遍历路径。")


class TextSearchRequest(BaseToolRequest):
    search_root: str = Field(description="文本搜索根目录。")
    query: str = Field(description="要搜索的文本或正则表达式。")
    regex: bool = Field(default=False, description="是否按正则表达式搜索。")
    case_sensitive: bool = Field(default=False, description="是否大小写敏感。")
    extensions: list[str] = Field(default_factory=list, description="扩展名过滤，不要求包含点号。")
    path_globs: list[str] = Field(default_factory=list, description="相对 search_root 的文件路径 glob 过滤。")
    limit: int = Field(default=50, ge=1, le=200, description="本次最多返回的命中数量；取值范围为 1 到 200。")
    offset: int = Field(default=0, ge=0, description="命中结果分页偏移；取值必须大于等于 0。")
    exclude_names: list[str] = Field(default_factory=list, description="额外排除的文件名或目录名。")
    include_hidden: bool = Field(default=False, description="是否包含隐藏文件或隐藏目录。")
    respect_gitignore: bool = Field(default=True, description="是否按项目 .gitignore 规则过滤遍历路径。")


ALWAYS_EXCLUDED_NAMES = {
    ".git",
}


CONTEXT_DOCUMENT_NAME = "AGENTS.md"
EXCLUDED_REPORT_LIMIT = 50
SKIPPED_REPORT_LIMIT = 20


def _is_binary_file(path: Path) -> bool:
    with path.open("rb") as file:
        sample = file.read(4096)
    return b"\0" in sample


def _sha256_file(path: Path) -> str:
    """计算文件内容 SHA256，供 Agent 修改文件前做并发版本校验。"""

    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_extensions(extensions: list[str]) -> set[str]:
    return {
        extension.strip().lower().lstrip(".")
        for extension in extensions
        if extension.strip()
    }


class GitIgnoreMatcher:
    def __init__(self, root: Path, enabled: bool) -> None:
        self.root = root
        self.enabled = enabled and self._is_inside_git_work_tree()
        self._cache: dict[str, bool] = {}

    def _is_inside_git_work_tree(self) -> bool:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "--is-inside-work-tree"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
                **hidden_subprocess_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def is_ignored(self, path: Path) -> bool:
        if not self.enabled:
            return False
        try:
            relative_path = path.relative_to(self.root).as_posix()
        except ValueError:
            return False
        if not relative_path:
            return False
        cached = self._cache.get(relative_path)
        if cached is not None:
            return cached
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "check-ignore", "--quiet", "--", relative_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
                **hidden_subprocess_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            self.enabled = False
            return False
        ignored = result.returncode == 0
        self._cache[relative_path] = ignored
        return ignored


def _iter_entries(
    root: Path,
    *,
    include_hidden: bool,
    exclude_names: set[str],
    respect_gitignore: bool,
) -> Iterator[Path]:
    gitignore_matcher = GitIgnoreMatcher(root, respect_gitignore)
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda path: (path.is_file(), path.name.lower()))
        except OSError:
            continue
        for child in children:
            if not include_hidden and child.name.startswith("."):
                continue
            if child.name in exclude_names:
                continue
            if gitignore_matcher.is_ignored(child):
                continue
            yield child
            if child.is_dir():
                stack.append(child)


def _matches_path_globs(path: Path, root: Path, path_globs: list[str], case_sensitive: bool) -> bool:
    if not path_globs:
        return True
    relative_path = path.relative_to(root).as_posix()
    comparable_path = relative_path if case_sensitive else relative_path.lower()
    for pattern in path_globs:
        comparable_pattern = pattern if case_sensitive else pattern.lower()
        if fnmatch.fnmatchcase(comparable_path, comparable_pattern):
            return True
    return False


def _matches_text_search_filters(
    path: Path,
    root: Path,
    request: TextSearchRequest,
    extensions: set[str],
    exclude_names: set[str],
) -> bool:
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        return False
    if any(part in exclude_names for part in relative_parts):
        return False
    if not request.include_hidden and any(part.startswith(".") for part in relative_parts):
        return False
    if extensions and path.suffix.lower().lstrip(".") not in extensions:
        return False
    return _matches_path_globs(path, root, request.path_globs, request.case_sensitive)


def _text_from_ripgrep_json_field(value: dict[str, Any]) -> str:
    text = value.get("text")
    if isinstance(text, str):
        return text
    return ""


def _ripgrep_text_search(root: Path, request: TextSearchRequest) -> BaseToolResult | None:
    rg_path = shutil.which("rg")
    if rg_path is None:
        return None

    command = [
        rg_path,
        "--json",
        "--line-number",
        "--color",
        "never",
        "--glob",
        "!.git/**",
    ]
    if not request.regex:
        command.append("--fixed-strings")
    if not request.case_sensitive:
        command.append("--ignore-case")
    if request.include_hidden:
        command.append("--hidden")
    if not request.respect_gitignore:
        command.append("--no-ignore")
    command.extend(["--", request.query, "."])

    extensions = _normalize_extensions(request.extensions)
    exclude_names = ALWAYS_EXCLUDED_NAMES | set(request.exclude_names)
    matches: list[dict[str, Any]] = []
    seen_count = 0
    has_more = False
    warnings: list[str] = []

    try:
        process = subprocess.Popen(
            command,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
    except OSError:
        return None

    assert process.stdout is not None
    for raw_output_line in process.stdout:
        try:
            event = json.loads(raw_output_line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data", {})
        path_text = _text_from_ripgrep_json_field(data.get("path", {}))
        if not path_text:
            continue
        path = (root / path_text).resolve()
        if not _matches_text_search_filters(path, root, request, extensions, exclude_names):
            continue
        seen_count += 1
        if seen_count <= request.offset:
            continue
        if len(matches) >= request.limit:
            has_more = True
            process.terminate()
            break
        line_number = data.get("line_number")
        if not isinstance(line_number, int):
            continue
        line = _text_from_ripgrep_json_field(data.get("lines", {})).rstrip("\r\n")
        matches.append(
            {
                "path": str(path),
                "line_number": line_number,
                "line": line,
            }
        )

    stderr = ""
    if process.stderr is not None:
        stderr = process.stderr.read().strip()
    return_code = process.wait()
    if return_code == 2 and not has_more:
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="RipgrepSearchError",
            error_message=stderr or "ripgrep 搜索失败。",
            output={"search_root": str(root), "query": request.query, "backend": "ripgrep"},
        )
    if stderr:
        warnings.append(stderr)

    return BaseToolResult(
        output={
            "search_root": str(root),
            "query": request.query,
            "regex": request.regex,
            "case_sensitive": request.case_sensitive,
            "matches": matches,
            "limit": request.limit,
            "offset": request.offset,
            "returned_count": len(matches),
            "has_more": has_more,
            "next_offset": request.offset + request.limit if has_more else None,
            "skipped_files": [],
            "skipped_count": 0,
            "skipped_truncated": False,
            "backend": "ripgrep",
            "warnings": warnings,
        }
    )


def _encoding_candidates(encoding: str | None) -> list[str]:
    encodings = []
    if encoding:
        encodings.append(encoding)
    encodings.extend(["utf-8", "utf-8-sig", "gbk", locale.getpreferredencoding(False)])
    candidates = []
    seen: set[str] = set()
    for candidate in encodings:
        if candidate and candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)
    return candidates


def _read_text_window(path: Path, request: FileReadRange, encoding: str | None) -> tuple[dict[str, Any], str]:
    last_error: UnicodeDecodeError | LookupError | None = None
    for candidate in _encoding_candidates(encoding):
        try:
            return _read_text_window_with_encoding(path, request, candidate), candidate
        except (UnicodeDecodeError, LookupError) as exc:
            last_error = exc

    message = "文件编码无法识别。"
    if last_error is not None:
        message = f"文件编码无法识别: {last_error}"
    raise UnicodeDecodeError("unknown", b"", 0, 1, message)


class FileReadLineTooLongError(ValueError):
    def __init__(self, long_lines: list[tuple[int, int]]) -> None:
        self.long_lines = long_lines
        long_line_details = "，".join(f"第 {line_number} 行 {line_char_count} 字符" for line_number, line_char_count in long_lines)
        super().__init__(
            "文件读取失败，部分行内容过大，这些大行请单独使用 file_read_line_slice 工具读取："
            f"{long_line_details}。"
            f"file_read 行范围读取单行上限为 {MAX_FILE_READ_MAX_CHARS_PER_LINE} 字符。"
            "调用 file_read_line_slice 时传入 file_path、line_number、char_start；"
            "如需继续读取，使用返回的 next_char_start。"
        )


def _read_text_window_with_encoding(path: Path, request: FileReadRange, encoding: str) -> dict[str, Any]:
    selected_lines: list[str] = []
    long_lines: list[tuple[int, int]] = []
    total_lines = 0
    start_line = request.start_line
    last_requested_line = request.start_line + request.line_count - 1

    with path.open("r", encoding=encoding, errors="strict") as file:
        for total_lines, raw_line in enumerate(file, start=1):
            line = raw_line.rstrip("\n")
            if start_line <= total_lines <= last_requested_line:
                line_char_count = len(line)
                if line_char_count > MAX_FILE_READ_MAX_CHARS_PER_LINE:
                    long_lines.append((total_lines, line_char_count))
                else:
                    selected_lines.append(line)

    if long_lines:
        raise FileReadLineTooLongError(long_lines)

    returned_line_count = len(selected_lines)
    end_line = start_line + returned_line_count - 1 if returned_line_count else start_line - 1
    return {
        "start_line": request.start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "lines": selected_lines,
    }


def _read_line_slice(path: Path, request: FileReadLineSliceRequest) -> tuple[dict[str, Any], str]:
    last_error: UnicodeDecodeError | LookupError | None = None
    for candidate in _encoding_candidates(request.encoding):
        try:
            return _read_line_slice_with_encoding(path, request, candidate), candidate
        except (UnicodeDecodeError, LookupError) as exc:
            last_error = exc

    message = "文件编码无法识别。"
    if last_error is not None:
        message = f"文件编码无法识别: {last_error}"
    raise UnicodeDecodeError("unknown", b"", 0, 1, message)


def _read_line_slice_with_encoding(path: Path, request: FileReadLineSliceRequest, encoding: str) -> dict[str, Any]:
    total_lines = 0
    target_line: str | None = None

    with path.open("r", encoding=encoding, errors="strict") as file:
        for total_lines, raw_line in enumerate(file, start=1):
            if total_lines == request.line_number:
                target_line = raw_line.rstrip("\n")
                break

    output_base = {
        "file_path": str(path),
        "line_number": request.line_number,
        "char_start": request.char_start,
        "total_lines": total_lines,
    }
    if target_line is None:
        return {**output_base, "line_found": False}

    line_char_count = len(target_line)
    start_index = request.char_start - 1
    if start_index > line_char_count:
        return {
            **output_base,
            "line_found": True,
            "char_start_valid": False,
            "line_char_count": line_char_count,
        }

    text = target_line[start_index : start_index + MAX_LINE_SLICE_CHARS]
    char_end = start_index + len(text)
    has_more = char_end < line_char_count
    return {
        **output_base,
        "line_found": True,
        "char_start_valid": True,
        "char_end": char_end,
        "line_char_count": line_char_count,
        "has_more": has_more,
        "next_char_start": char_end + 1 if has_more else None,
        "text": text,
    }


def _line_matches(line: str, query: str, compiled_regex: re.Pattern[str] | None, case_sensitive: bool) -> bool:
    if compiled_regex is not None:
        return compiled_regex.search(line) is not None
    if case_sensitive:
        return query in line
    return query.lower() in line.lower()


def _collect_text_search_matches_with_encoding(
    path: Path,
    *,
    encoding: str,
    request: TextSearchRequest,
    compiled_regex: re.Pattern[str] | None,
    global_seen_start: int,
    returned_count: int,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    seen_count = 0
    has_more = False

    with path.open("r", encoding=encoding, errors="strict") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.rstrip("\n")
            if has_more:
                break

            if _line_matches(line, request.query, compiled_regex, request.case_sensitive):
                global_match_index = global_seen_start + seen_count
                seen_count += 1
                if global_match_index < request.offset:
                    pass
                elif returned_count + len(matches) >= request.limit:
                    has_more = True
                    break
                else:
                    match = {
                        "path": str(path),
                        "line_number": line_number,
                        "line": line,
                    }
                    matches.append(match)

    return {"seen_count": seen_count, "matches": matches, "has_more": has_more}


def _collect_text_search_matches(
    path: Path,
    *,
    request: TextSearchRequest,
    compiled_regex: re.Pattern[str] | None,
    global_seen_start: int,
    returned_count: int,
) -> tuple[dict[str, Any], str | None]:
    last_error: UnicodeDecodeError | LookupError | None = None
    for encoding in _encoding_candidates(None):
        try:
            return (
                _collect_text_search_matches_with_encoding(
                    path,
                    encoding=encoding,
                    request=request,
                    compiled_regex=compiled_regex,
                    global_seen_start=global_seen_start,
                    returned_count=returned_count,
                ),
                None,
            )
        except (UnicodeDecodeError, LookupError) as exc:
            last_error = exc
        except OSError as exc:
            return {"seen_count": 0, "matches": [], "has_more": False}, f"read_error: {exc}"
    if last_error is not None:
        return {"seen_count": 0, "matches": [], "has_more": False}, f"decode_error: {last_error}"
    return {"seen_count": 0, "matches": [], "has_more": False}, "decode_error"

def _file_read_failure(output_base: dict[str, Any], error_code: str, error_message: str) -> dict[str, Any]:
    return {
        **output_base,
        "success": False,
        "error_code": error_code,
        "error_message": error_message,
    }


def _read_file_request(file_request: FileReadFileRequest) -> tuple[bool, dict[str, Any]]:
    path = Path(file_request.file_path).expanduser().resolve()
    output_base = {"file_path": str(path)}

    if not path.exists():
        return False, _file_read_failure(output_base, "PathNotFound", f"文件不存在: {path}")
    if not path.is_file():
        return False, _file_read_failure(output_base, "NotAFile", f"路径不是文件: {path}")
    if _is_binary_file(path):
        return False, _file_read_failure(output_base, "BinaryFileNotReadable", f"文件可能是二进制文件，默认不读取: {path}")

    try:
        segments: list[dict[str, Any]] = []
        total_lines: int | None = None
        for range_request in file_request.ranges:
            segment_output, _segment_encoding = _read_text_window(path, range_request, file_request.encoding)
            total_lines = segment_output.pop("total_lines")
            segments.append(segment_output)
    except FileReadLineTooLongError as exc:
        return False, _file_read_failure(output_base, "LineTooLong", str(exc))
    except UnicodeDecodeError as exc:
        return False, _file_read_failure(output_base, "DecodeError", str(exc))
    except OSError as exc:
        return False, _file_read_failure(output_base, "ReadError", str(exc))

    output: dict[str, Any] = {
        **output_base,
        "content_sha256": _sha256_file(path),
        "total_lines": total_lines if total_lines is not None else 0,
        "segments": segments,
    }
    return True, output


@tool(
    name="file_read",
    description=f"批量读取一个或多个文本文件的指定行范围，返回紧凑文本结构：file 层包含 total_lines，segment 层包含 start_line、end_line 和 lines 字符串数组。files 一次最多 {MAX_FILE_READ_FILES} 个；每段 line_count 取值范围为 1 到 {MAX_FILE_READ_LINE_COUNT}。行范围读取中单行超过 {MAX_FILE_READ_MAX_CHARS_PER_LINE} 字符时，该文件读取失败；请根据 error_message 使用 file_read_line_slice 按行号和行内偏移单独读取大行内容。",
    group="file",
    category="business",
    execution_mode="blocking",
    blocking_pool=BlockingPool.FILE_RESOURCE,
    blocking_operation="tool.file.read",
)
def file_read(request: FileReadRequest) -> BaseToolResult:
    files: list[dict[str, Any]] = []
    failed_files: list[dict[str, Any]] = []

    for file_request in request.files:
        success, file_output = _read_file_request(file_request)
        files.append(file_output)
        if not success:
            failed_files.append(file_output)

    output = {"files": files}
    if failed_files:
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="FileReadBatchFailed",
            error_message=f"{len(failed_files)} 个文件读取失败。",
            output=output,
        )
    return BaseToolResult(output=output)


@tool(
    name="file_read_line_slice",
    description=f"用于单独读取大行内容的行内偏移读取工具；当 file_read 提示部分行内容过大时，传入 file_path、line_number 和 char_start 读取该行从指定字符开始的片段。每次调用由工具内部固定最多返回 {MAX_LINE_SLICE_CHARS} 个字符；如返回 has_more=true，再用 next_char_start 继续读取。",
    group="file",
    category="business",
    execution_mode="blocking",
    blocking_pool=BlockingPool.FILE_RESOURCE,
    blocking_operation="tool.file.read_line_slice",
)
def file_read_line_slice(request: FileReadLineSliceRequest) -> BaseToolResult:
    path = Path(request.file_path).expanduser().resolve()
    output_base = {"file_path": str(path), "line_number": request.line_number, "char_start": request.char_start}

    if not path.exists():
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="PathNotFound",
            error_message=f"文件不存在: {path}",
            output=output_base,
        )
    if not path.is_file():
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="NotAFile",
            error_message=f"路径不是文件: {path}",
            output=output_base,
        )
    if _is_binary_file(path):
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="BinaryFileNotReadable",
            error_message=f"文件可能是二进制文件，默认不读取: {path}",
            output=output_base,
        )

    try:
        output, _used_encoding = _read_line_slice(path, request)
    except UnicodeDecodeError as exc:
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DecodeError",
            error_message=str(exc),
            output=output_base,
        )

    return BaseToolResult(output=output)


@tool(
    name="context_document_discover",
    description="给定项目根目录，返回该项目内所有 AGENTS.md 上下文文档的绝对路径列表。",
    group="file",
    category="business",
    execution_mode="blocking",
    blocking_pool=BlockingPool.FILE_RESOURCE,
    blocking_operation="tool.file.context_document_discover",
)
def context_document_discover(request: ContextDocumentDiscoverRequest) -> BaseToolResult:
    project_root = Path(request.project_root).expanduser().resolve()
    output_base = {"project_root": str(project_root)}

    if not project_root.exists():
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="PathNotFound",
            error_message=f"项目根目录不存在: {project_root}",
            output=output_base,
        )
    if not project_root.is_dir():
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="NotADirectory",
            error_message=f"项目根路径不是目录: {project_root}",
            output=output_base,
        )

    documents: list[str] = []
    for entry in _iter_entries(
        project_root,
        include_hidden=True,
        exclude_names=ALWAYS_EXCLUDED_NAMES,
        respect_gitignore=True,
    ):
        if entry.is_file() and entry.name == CONTEXT_DOCUMENT_NAME:
            documents.append(str(entry))

    documents.sort(key=lambda value: (value.count("\\") + value.count("/"), value.lower()))
    return BaseToolResult(output={"project_root": str(project_root), "documents": documents})


@tool(
    name="directory_explore",
    description="递进式探索指定目录结构，返回受 depth 和 max_entries 控制的目录/文件摘要。",
    group="file",
    category="business",
    execution_mode="blocking",
    blocking_pool=BlockingPool.FILE_RESOURCE,
    blocking_operation="tool.file.directory_explore",
)
def directory_explore(request: DirectoryExploreRequest) -> BaseToolResult:
    root = Path(request.directory_path).expanduser().resolve()
    output_base = {"directory_path": str(root)}

    if not root.exists():
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="PathNotFound",
            error_message=f"目录不存在: {root}",
            output=output_base,
        )
    if not root.is_dir():
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="NotADirectory",
            error_message=f"路径不是目录: {root}",
            output=output_base,
        )

    excluded_names = ALWAYS_EXCLUDED_NAMES | set(request.exclude_names)
    entries: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    truncated = False
    warnings: list[str] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    gitignore_matcher = GitIgnoreMatcher(root, request.respect_gitignore)

    while stack and not truncated:
        directory, current_depth = stack.pop()
        if current_depth >= request.depth:
            continue

        try:
            children = sorted(directory.iterdir(), key=lambda path: (path.is_file(), path.name.lower()))
        except OSError as exc:
            warnings.append(f"目录无法读取: {directory}: {exc}")
            continue

        child_directories: list[Path] = []
        for child in children:
            if len(entries) >= request.max_entries:
                truncated = True
                break
            if not request.include_hidden and child.name.startswith("."):
                if len(excluded) < EXCLUDED_REPORT_LIMIT:
                    excluded.append({"path": str(child), "reason": "hidden"})
                continue
            if child.name in excluded_names:
                if len(excluded) < EXCLUDED_REPORT_LIMIT:
                    excluded.append({"path": str(child), "reason": "excluded_name"})
                continue
            if gitignore_matcher.is_ignored(child):
                if len(excluded) < EXCLUDED_REPORT_LIMIT:
                    excluded.append({"path": str(child), "reason": "gitignore"})
                continue
            if child.is_symlink() and not request.follow_symlinks:
                if len(excluded) < EXCLUDED_REPORT_LIMIT:
                    excluded.append({"path": str(child), "reason": "symlink"})
                continue
            if child.is_dir():
                try:
                    child_count = sum(1 for _ in child.iterdir())
                except OSError:
                    child_count = None
                entries.append(
                    {
                        "path": str(child),
                        "type": "directory",
                        "depth": current_depth + 1,
                        "child_count": child_count,
                    }
                )
                child_directories.append(child)
            elif request.include_files and child.is_file():
                entries.append(
                    {
                        "path": str(child),
                        "type": "file",
                        "depth": current_depth + 1,
                        "size_bytes": child.stat().st_size,
                    }
                )

        for child_directory in reversed(child_directories):
            stack.append((child_directory, current_depth + 1))

    if truncated:
        warnings.append("目录项超过 max_entries，请缩小 directory_path、降低 depth 或增加 exclude_names 后继续探索。")

    return BaseToolResult(
        output={
            "directory_path": str(root),
            "depth": request.depth,
            "entries": entries,
            "excluded": excluded,
            "truncated": truncated,
            "returned_count": len(entries),
            "max_entries": request.max_entries,
            "warnings": warnings,
        }
    )


@tool(
    name="file_name_search",
    description="按文件名关键词、文件名正则、相对路径 glob 或扩展名在指定目录下定位候选文件/目录。",
    group="file",
    category="business",
    execution_mode="blocking",
    blocking_pool=BlockingPool.FILE_RESOURCE,
    blocking_operation="tool.file.name_search",
)
def file_name_search(request: FileNameSearchRequest) -> BaseToolResult:
    root = Path(request.search_root).expanduser().resolve()
    output_base = {"search_root": str(root)}

    if not root.exists():
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="PathNotFound",
            error_message=f"搜索根目录不存在: {root}",
            output=output_base,
        )
    if not root.is_dir():
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="NotADirectory",
            error_message=f"搜索根路径不是目录: {root}",
            output=output_base,
        )
    if not request.name_query and not request.glob_pattern and not request.regex_pattern and not request.extensions:
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="InvalidRequest",
            error_message="name_query、glob_pattern、regex_pattern、extensions 至少需要提供一个。",
            output=output_base,
        )

    regex = None
    if request.regex_pattern:
        flags = 0 if request.case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(request.regex_pattern, flags)
        except re.error as exc:
            return BaseToolResult(
                success=False,
                execution_status="failed",
                error_code="InvalidRegex",
                error_message=f"文件名正则表达式非法: {exc}",
                output=output_base,
            )

    query = request.name_query if request.case_sensitive else request.name_query.lower() if request.name_query else None
    extensions = _normalize_extensions(request.extensions)
    excluded_names = ALWAYS_EXCLUDED_NAMES | set(request.exclude_names)
    matches: list[dict[str, Any]] = []
    seen_count = 0
    has_more = False

    for entry in _iter_entries(
        root,
        include_hidden=request.include_hidden,
        exclude_names=excluded_names,
        respect_gitignore=request.respect_gitignore,
    ):
        if request.entry_type == "file" and not entry.is_file():
            continue
        if request.entry_type == "directory" and not entry.is_dir():
            continue
        if extensions and (not entry.is_file() or entry.suffix.lower().lstrip(".") not in extensions):
            continue

        basename = entry.name if request.case_sensitive else entry.name.lower()
        relative_path = entry.relative_to(root).as_posix()
        comparable_relative_path = relative_path if request.case_sensitive else relative_path.lower()
        glob_pattern = request.glob_pattern if request.case_sensitive or not request.glob_pattern else request.glob_pattern.lower()

        matched = True
        if query and query not in basename:
            matched = False
        if matched and regex is not None and regex.search(entry.name) is None:
            matched = False
        if matched and glob_pattern and not fnmatch.fnmatchcase(comparable_relative_path, glob_pattern):
            matched = False
        if not matched:
            continue

        if seen_count < request.offset:
            seen_count += 1
            continue
        if len(matches) >= request.max_results:
            has_more = True
            break

        item: dict[str, Any] = {
            "path": str(entry),
            "type": "directory" if entry.is_dir() else "file",
        }
        if entry.is_file():
            item["size_bytes"] = entry.stat().st_size
        matches.append(item)
        seen_count += 1

    return BaseToolResult(
        output={
            "search_root": str(root),
            "matches": matches,
            "offset": request.offset,
            "max_results": request.max_results,
            "returned_count": len(matches),
            "has_more": has_more,
            "next_offset": request.offset + request.max_results if has_more else None,
            "warnings": [],
        }
    )


@tool(
    name="text_search",
    description="在指定目录内搜索文本或正则表达式，返回全局 offset/limit 分页控制的命中行和可选上下文行。",
    group="file",
    category="business",
    execution_mode="blocking",
    blocking_pool=BlockingPool.FILE_RESOURCE,
    blocking_operation="tool.file.text_search",
)
def text_search(request: TextSearchRequest) -> BaseToolResult:
    root = Path(request.search_root).expanduser().resolve()
    output_base = {"search_root": str(root), "query": request.query}

    if not root.exists():
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="PathNotFound",
            error_message=f"搜索根目录不存在: {root}",
            output=output_base,
        )
    if not root.is_dir():
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="NotADirectory",
            error_message=f"搜索根路径不是目录: {root}",
            output=output_base,
        )

    ripgrep_result = _ripgrep_text_search(root, request)
    if ripgrep_result is not None:
        return ripgrep_result

    compiled_regex = None
    if request.regex:
        flags = 0 if request.case_sensitive else re.IGNORECASE
        try:
            compiled_regex = re.compile(request.query, flags)
        except re.error as exc:
            return BaseToolResult(
                success=False,
                execution_status="failed",
                error_code="InvalidRegex",
                error_message=f"文本搜索正则表达式非法: {exc}",
                output=output_base,
            )

    extensions = _normalize_extensions(request.extensions)
    excluded_names = ALWAYS_EXCLUDED_NAMES | set(request.exclude_names)
    matches: list[dict[str, Any]] = []
    skipped_files: list[dict[str, str]] = []
    skipped_count = 0
    total_seen_matches = 0
    has_more = False

    for entry in _iter_entries(
        root,
        include_hidden=request.include_hidden,
        exclude_names=excluded_names,
        respect_gitignore=request.respect_gitignore,
    ):
        if has_more:
            break
        if not entry.is_file():
            continue
        if extensions and entry.suffix.lower().lstrip(".") not in extensions:
            continue
        if not _matches_path_globs(entry, root, request.path_globs, request.case_sensitive):
            continue

        try:
            if _is_binary_file(entry):
                skipped_count += 1
                if len(skipped_files) < SKIPPED_REPORT_LIMIT:
                    skipped_files.append({"path": str(entry), "reason": "binary_file"})
                continue
        except OSError as exc:
            skipped_count += 1
            if len(skipped_files) < SKIPPED_REPORT_LIMIT:
                skipped_files.append({"path": str(entry), "reason": f"read_error: {exc}"})
            continue

        file_matches, file_skipped_reason = _collect_text_search_matches(
            entry,
            request=request,
            compiled_regex=compiled_regex,
            global_seen_start=total_seen_matches,
            returned_count=len(matches),
        )
        if file_skipped_reason is not None:
            skipped_count += 1
            if len(skipped_files) < SKIPPED_REPORT_LIMIT:
                skipped_files.append({"path": str(entry), "reason": file_skipped_reason})
            continue

        total_seen_matches += file_matches["seen_count"]
        matches.extend(file_matches["matches"])
        if len(matches) >= request.limit and file_matches["has_more"]:
            has_more = True

    return BaseToolResult(
        output={
            "search_root": str(root),
            "query": request.query,
            "regex": request.regex,
            "case_sensitive": request.case_sensitive,
            "matches": matches[: request.limit],
            "limit": request.limit,
            "offset": request.offset,
            "returned_count": min(len(matches), request.limit),
            "has_more": has_more,
            "next_offset": request.offset + request.limit if has_more else None,
            "skipped_files": skipped_files,
            "skipped_count": skipped_count,
            "skipped_truncated": skipped_count > len(skipped_files),
            "backend": "python",
            "warnings": [],
        }
    )

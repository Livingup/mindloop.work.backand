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

from pydantic import Field

from agent_runtime.tool.tool_decorator import tool
from agent_runtime.tool.tool_execution_models import BaseToolRequest, BaseToolResult


class FileReadRequest(BaseToolRequest):
    file_path: str = Field(description="要读取的文件绝对路径或当前进程可解析的文件路径。")
    start_line: int = Field(default=1, ge=1, description="1-based 起始行号；取值必须大于等于 1。")
    line_count: int = Field(default=200, ge=1, le=500, description="最多读取的行数；取值范围为 1 到 500。")
    max_chars: int = Field(default=20000, ge=1000, le=50000, description="最多返回的内容字符数；取值范围为 1000 到 50000。")
    encoding: str | None = Field(default=None, description="可选的文件编码；为空时自动尝试常见文本编码。")
    include_line_numbers: bool = Field(default=True, description="是否在返回内容中包含行号。")


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
    context_lines: int = Field(default=0, ge=0, le=5, description="命中前后各返回的上下文行数；取值范围为 0 到 5。")
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


def _detect_text_newline_info(path: Path, encoding: str) -> dict[str, Any]:
    """识别文本文件换行风格和结尾换行，用于辅助后续安全编辑。"""

    newline_counts = {"CRLF": 0, "LF": 0, "CR": 0}
    has_final_newline = False
    with path.open("r", encoding=encoding, errors="strict", newline="") as file:
        for raw_line in file:
            if raw_line.endswith("\r\n"):
                newline_counts["CRLF"] += 1
                has_final_newline = True
            elif raw_line.endswith("\n"):
                newline_counts["LF"] += 1
                has_final_newline = True
            elif raw_line.endswith("\r"):
                newline_counts["CR"] += 1
                has_final_newline = True
            else:
                has_final_newline = False
    used_styles = [name for name, count in newline_counts.items() if count > 0]
    if not used_styles:
        newline = "NONE"
    elif len(used_styles) == 1:
        newline = used_styles[0]
    else:
        newline = "MIXED"
    return {
        "content_sha256": _sha256_file(path),
        "newline": newline,
        "newline_counts": newline_counts,
        "has_final_newline": has_final_newline,
    }


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


def _read_match_context(path: Path, line_number: int, context_lines: int) -> tuple[list[str], list[str]]:
    if context_lines <= 0:
        return [], []
    start_line = max(1, line_number - context_lines)
    end_line = line_number + context_lines
    for encoding in _encoding_candidates(None):
        before: list[str] = []
        after: list[str] = []
        try:
            with path.open("r", encoding=encoding, errors="strict") as file:
                for current_line_number, raw_line in enumerate(file, start=1):
                    if current_line_number < start_line:
                        continue
                    if current_line_number > end_line:
                        break
                    if current_line_number == line_number:
                        continue
                    line = raw_line.rstrip("\n")
                    if current_line_number < line_number:
                        before.append(line)
                    else:
                        after.append(line)
            return before, after
        except (OSError, UnicodeDecodeError, LookupError):
            continue
    return [], []


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
        before, after = _read_match_context(path, line_number, request.context_lines)
        matches.append(
            {
                "path": str(path),
                "line_number": line_number,
                "line": line,
                "before": before,
                "after": after,
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


def _read_text_window(path: Path, request: FileReadRequest) -> tuple[dict[str, Any], str]:
    last_error: UnicodeDecodeError | LookupError | None = None
    for candidate in _encoding_candidates(request.encoding):
        try:
            return _read_text_window_with_encoding(path, request, candidate), candidate
        except (UnicodeDecodeError, LookupError) as exc:
            last_error = exc

    message = "文件编码无法识别。"
    if last_error is not None:
        message = f"文件编码无法识别: {last_error}"
    raise UnicodeDecodeError("unknown", b"", 0, 1, message)


def _read_text_window_with_encoding(path: Path, request: FileReadRequest, encoding: str) -> dict[str, Any]:
    selected_lines: list[str] = []
    current_chars = 0
    truncation_reason: str | None = None
    total_lines = 0
    start_line = request.start_line
    last_requested_line = request.start_line + request.line_count - 1

    with path.open("r", encoding=encoding, errors="strict") as file:
        for total_lines, raw_line in enumerate(file, start=1):
            if total_lines < start_line:
                continue
            if total_lines > last_requested_line:
                if truncation_reason is None:
                    truncation_reason = "line_count_limit"
                continue
            if truncation_reason is not None:
                continue

            line = raw_line.rstrip("\n")
            rendered_line = f"{total_lines}→{line}" if request.include_line_numbers else line
            additional_chars = len(rendered_line) + (1 if selected_lines else 0)
            if selected_lines and current_chars + additional_chars > request.max_chars:
                truncation_reason = "max_chars_limit"
                continue
            if not selected_lines and len(rendered_line) > request.max_chars:
                selected_lines.append(rendered_line[: request.max_chars])
                truncation_reason = "max_chars_limit"
                continue
            selected_lines.append(rendered_line)
            current_chars += additional_chars

    returned_line_count = len(selected_lines)
    end_line = start_line + returned_line_count - 1 if returned_line_count else start_line - 1
    truncated = truncation_reason is not None
    next_start_line = end_line + 1 if truncated and end_line < total_lines else None

    return {
        "file_path": str(path),
        "start_line": request.start_line,
        "end_line": end_line,
        "requested_line_count": request.line_count,
        "returned_line_count": returned_line_count,
        "total_lines": total_lines,
        "next_start_line": next_start_line,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "size_bytes": path.stat().st_size,
        "content": "\n".join(selected_lines),
        "warnings": [],
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
    lines_before: list[str] = []
    pending_matches: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    seen_count = 0
    has_more = False

    with path.open("r", encoding=encoding, errors="strict") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.rstrip("\n")
            for pending_match in list(pending_matches):
                if len(pending_match["after"]) < request.context_lines:
                    pending_match["after"].append(line)
                if len(pending_match["after"]) >= request.context_lines:
                    pending_matches.remove(pending_match)

            if has_more:
                if not pending_matches:
                    break
                if request.context_lines > 0:
                    lines_before.append(line)
                    if len(lines_before) > request.context_lines:
                        lines_before.pop(0)
                continue

            if _line_matches(line, request.query, compiled_regex, request.case_sensitive):
                global_match_index = global_seen_start + seen_count
                seen_count += 1
                if global_match_index < request.offset:
                    pass
                elif returned_count + len(matches) >= request.limit:
                    has_more = True
                    if not pending_matches:
                        break
                else:
                    match = {
                        "path": str(path),
                        "line_number": line_number,
                        "line": line,
                        "before": list(lines_before),
                        "after": [],
                    }
                    matches.append(match)
                    if request.context_lines > 0:
                        pending_matches.append(match)

            if request.context_lines > 0:
                lines_before.append(line)
                if len(lines_before) > request.context_lines:
                    lines_before.pop(0)

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

@tool(
    name="file_read",
    description="按行范围读取指定文本文件内容，使用 line_count 与 max_chars 控制输出大小。",
    group="file",
    category="system",
)
def file_read(request: FileReadRequest) -> BaseToolResult:
    path = Path(request.file_path).expanduser().resolve()
    output_base = {"file_path": str(path)}

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
        output, used_encoding = _read_text_window(path, request)
    except UnicodeDecodeError as exc:
        return BaseToolResult(
            success=False,
            execution_status="failed",
            error_code="DecodeError",
            error_message=str(exc),
            output=output_base,
        )

    output["encoding"] = used_encoding
    output.update(_detect_text_newline_info(path, used_encoding))
    return BaseToolResult(output=output)


@tool(
    name="context_document_discover",
    description="给定项目根目录，返回该项目内所有 AGENTS.md 上下文文档的绝对路径列表。",
    group="file",
    category="system",
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
    category="system",
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
    category="system",
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
    category="system",
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

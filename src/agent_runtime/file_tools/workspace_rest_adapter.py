from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_runtime.subprocess_utils import hidden_subprocess_kwargs


class WorkspaceTreeRequest(BaseModel):
    directory_path: str = Field(description="要读取工作区目录树的目录路径。")
    max_depth: int = Field(default=2, ge=0, le=4, description="目录树最大递归深度；取值范围为 0 到 4。")
    max_entries_per_directory: int = Field(default=80, ge=1, le=300, description="每个目录最多返回的子项数量；取值范围为 1 到 300。")


class WorkspaceTreeNodeResponse(BaseModel):
    name: str
    path: str
    type: Literal["folder", "file"] = Field(description="节点类型；可选值仅限 folder、file。")
    children: list["WorkspaceTreeNodeResponse"] = Field(default_factory=list)
    children_loaded: bool = Field(default=True, description="目录子项是否已经随当前响应加载；为 false 时前端可按需再次请求该目录。")


class WorkspaceTreeResponse(BaseModel):
    name: str
    path: str
    children: list[WorkspaceTreeNodeResponse] = Field(default_factory=list)


class WorkspaceGitStatusRequest(BaseModel):
    workspace_path: str = Field(description="要扫描 Git 状态的工作区根目录路径。")


class WorkspaceGitRepositoryStatusResponse(BaseModel):
    repository_path: str = Field(description="Git 仓库根目录绝对路径。")
    relative_path: str = Field(description="仓库根目录相对工作区根目录的规范路径；工作区根仓库为 .。")
    branch: str | None = Field(default=None, description="当前检出的 Git 分支；detached HEAD 或不可读取时为空。")
    dirty: bool = Field(default=False, description="工作区是否存在未提交变更。")
    display_text: str | None = Field(default=None, description="状态栏可直接展示的仓库状态文本，例如 main* 或 HEAD@abc1234。")
    reason: str | None = Field(default=None, description="仓库状态存在降级或不可读取时的机器可读原因。")


class WorkspaceGitStatusResponse(BaseModel):
    workspace_path: str
    repositories: list[WorkspaceGitRepositoryStatusResponse] = Field(default_factory=list, description="工作区下按层级顺序排列的 Git 仓库状态列表。")
    is_git_repository_root: bool = Field(default=False, description="兼容字段：工作区根目录本身是否为 Git 仓库根目录。")
    branch: str | None = Field(default=None, description="兼容字段：工作区根仓库当前检出的 Git 分支。")
    dirty: bool = Field(default=False, description="兼容字段：工作区根仓库是否存在未提交变更。")
    display_text: str | None = Field(default=None, description="兼容字段：工作区根仓库可直接展示的分支文本，例如 main*。")
    reason: str | None = Field(default=None, description="兼容字段或扫描降级原因。")
    scan_truncated: bool = Field(default=False, description="扫描目录或仓库数量达到边界时为 true，表示结果可能被截断。")


class WorkspaceMemoryListRequest(BaseModel):
    workspace_path: str = Field(description="当前打开项目的工作区根目录路径。")


class WorkspaceMemoryReadRequest(BaseModel):
    workspace_path: str = Field(description="当前打开项目的工作区根目录路径。")
    agent_id: str = Field(description="记忆文件所属 Agent 目录名；只能是 .spread-ai/memory 下的单层目录名。")


class WorkspaceMemorySaveRequest(BaseModel):
    workspace_path: str = Field(description="当前打开项目的工作区根目录路径。")
    agent_id: str = Field(description="记忆文件所属 Agent 目录名；只能是 .spread-ai/memory 下的单层目录名。")
    content: str = Field(description="要保存到 memory.md 的 Markdown 原文。")
    expected_sha256: str = Field(description="读取时返回的内容 SHA256，用于乐观并发控制。")


class WorkspaceMemoryEntryResponse(BaseModel):
    key: str = Field(description="前端选择与保存时使用的受控条目 key，当前等同 agent_id。")
    agent_id: str
    relative_path: str = Field(description="相对 workspace 根目录的记忆文件路径。")
    size_bytes: int
    modified_at: float = Field(description="文件最后修改时间 Unix timestamp。")
    content_sha256: str
    summary: str = Field(default="", description="记忆文件内容摘要，用于前端列表展示和搜索。")


class WorkspaceMemoryListResponse(BaseModel):
    workspace_path: str
    memory_root_exists: bool
    entries: list[WorkspaceMemoryEntryResponse] = Field(default_factory=list)


class WorkspaceMemoryReadResponse(WorkspaceMemoryEntryResponse):
    content: str


class WorkspaceMemorySaveResponse(WorkspaceMemoryEntryResponse):
    status: str = "saved"


_EXCLUDED_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "node_modules",
    "venv",
    ".venv",
}


def create_workspace_router() -> APIRouter:
    router = APIRouter()

    @router.post("/tree", response_model=WorkspaceTreeResponse)
    def get_workspace_tree(body: WorkspaceTreeRequest) -> WorkspaceTreeResponse:
        root = _resolve_workspace_directory(body.directory_path)
        return WorkspaceTreeResponse(
            name=root.name or str(root),
            path=str(root),
            children=_read_children(
                root,
                depth=0,
                max_depth=body.max_depth,
                max_entries_per_directory=body.max_entries_per_directory,
            ),
        )

    @router.post("/git-status", response_model=WorkspaceGitStatusResponse)
    def get_workspace_git_status(body: WorkspaceGitStatusRequest) -> WorkspaceGitStatusResponse:
        root = _resolve_workspace_directory(body.workspace_path)
        return _read_workspace_git_status(root)

    @router.post("/memory/list", response_model=WorkspaceMemoryListResponse)
    def list_workspace_memory(body: WorkspaceMemoryListRequest) -> WorkspaceMemoryListResponse:
        root = _resolve_workspace_directory(body.workspace_path)
        return _list_workspace_memory(root)

    @router.post("/memory/read", response_model=WorkspaceMemoryReadResponse)
    def read_workspace_memory(body: WorkspaceMemoryReadRequest) -> WorkspaceMemoryReadResponse:
        root = _resolve_workspace_directory(body.workspace_path)
        return _read_workspace_memory(root, body.agent_id)

    @router.post("/memory/save", response_model=WorkspaceMemorySaveResponse)
    def save_workspace_memory(body: WorkspaceMemorySaveRequest) -> WorkspaceMemorySaveResponse:
        root = _resolve_workspace_directory(body.workspace_path)
        return _save_workspace_memory(root, body.agent_id, body.content, body.expected_sha256)

    return router


_MEMORY_FILE_NAME = "memory.md"
_MEMORY_SUMMARY_MAX_LENGTH = 160


def _resolve_workspace_directory(directory_path: str) -> Path:
    root = Path(directory_path).expanduser().resolve()
    if not root.exists():
        raise HTTPException(status_code=404, detail=f"工作目录不存在: {root}")
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"工作目录不是文件夹: {root}")
    return root


def _list_workspace_memory(workspace_root: Path) -> WorkspaceMemoryListResponse:
    memory_root = _resolve_memory_root(workspace_root, require_exists=False)
    if not memory_root.exists():
        return WorkspaceMemoryListResponse(workspace_path=str(workspace_root), memory_root_exists=False, entries=[])
    _ensure_memory_root_directory(memory_root, workspace_root)
    entries: list[WorkspaceMemoryEntryResponse] = []
    try:
        agent_directories = sorted(
            [entry for entry in memory_root.iterdir() if entry.is_dir() and not entry.is_symlink()],
            key=lambda entry: entry.name.casefold(),
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取项目记忆目录失败: {exc}") from exc
    for agent_directory in agent_directories:
        memory_file = agent_directory / _MEMORY_FILE_NAME
        if not memory_file.exists() or memory_file.is_symlink() or not memory_file.is_file():
            continue
        try:
            entries.append(_memory_entry_response(workspace_root, agent_directory.name))
        except HTTPException:
            continue
    return WorkspaceMemoryListResponse(
        workspace_path=str(workspace_root),
        memory_root_exists=True,
        entries=entries,
    )


def _read_workspace_memory(workspace_root: Path, agent_id: str) -> WorkspaceMemoryReadResponse:
    target = _resolve_agent_memory_file(workspace_root, agent_id, require_exists=True)
    raw = _read_memory_bytes(target)
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=415, detail="记忆文件不是 UTF-8 文本，无法在前端编辑。") from exc
    entry = _memory_entry_response(workspace_root, agent_id, raw_bytes=raw)
    return WorkspaceMemoryReadResponse(**entry.model_dump(), content=content)


def _save_workspace_memory(
    workspace_root: Path,
    agent_id: str,
    content: str,
    expected_sha256: str,
) -> WorkspaceMemorySaveResponse:
    target = _resolve_agent_memory_file(workspace_root, agent_id, require_exists=True)
    current_bytes = _read_memory_bytes(target)
    current_sha256 = _sha256_hex(current_bytes)
    if expected_sha256 != current_sha256:
        raise HTTPException(status_code=409, detail="记忆文件已被外部修改，请刷新后再保存。")
    target.parent.mkdir(parents=True, exist_ok=True)
    next_bytes = content.encode("utf-8")
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", delete=False, dir=target.parent, prefix=".memory.", suffix=".tmp") as temp_file:
            temp_path = temp_file.name
            temp_file.write(next_bytes)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, target)
        temp_path = None
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"保存项目记忆失败: {exc}") from exc
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    entry = _memory_entry_response(workspace_root, agent_id, raw_bytes=next_bytes)
    return WorkspaceMemorySaveResponse(**entry.model_dump())


def _resolve_memory_root(workspace_root: Path, *, require_exists: bool) -> Path:
    memory_root = workspace_root / ".spread-ai" / "memory"
    if require_exists and not memory_root.exists():
        raise HTTPException(status_code=404, detail="当前项目暂无 Agent 记忆目录。")
    resolved_memory_root = memory_root.resolve(strict=False)
    if not _is_path_within(resolved_memory_root, workspace_root.resolve()):
        raise HTTPException(status_code=400, detail="项目记忆目录越界，拒绝访问。")
    return memory_root


def _ensure_memory_root_directory(memory_root: Path, workspace_root: Path) -> None:
    if memory_root.is_symlink():
        raise HTTPException(status_code=400, detail="项目记忆目录不能是符号链接。")
    if not memory_root.is_dir():
        raise HTTPException(status_code=400, detail="项目记忆路径不是目录。")
    resolved_memory_root = memory_root.resolve(strict=True)
    if not _is_path_within(resolved_memory_root, workspace_root.resolve()):
        raise HTTPException(status_code=400, detail="项目记忆目录越界，拒绝访问。")


def _resolve_agent_memory_file(workspace_root: Path, agent_id: str, *, require_exists: bool) -> Path:
    safe_agent_id = _validate_memory_agent_id(agent_id)
    memory_root = _resolve_memory_root(workspace_root, require_exists=require_exists)
    _ensure_memory_root_directory(memory_root, workspace_root)
    agent_directory = memory_root / safe_agent_id
    if agent_directory.is_symlink():
        raise HTTPException(status_code=400, detail="Agent 记忆目录不能是符号链接。")
    target = agent_directory / _MEMORY_FILE_NAME
    if require_exists:
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Agent 记忆文件不存在: {safe_agent_id}/{_MEMORY_FILE_NAME}")
        if not target.is_file():
            raise HTTPException(status_code=400, detail="Agent 记忆路径不是文件。")
    if target.is_symlink():
        raise HTTPException(status_code=400, detail="Agent 记忆文件不能是符号链接。")
    resolved_target = target.resolve(strict=require_exists)
    resolved_memory_root = memory_root.resolve(strict=True)
    if not _is_path_within(resolved_target, resolved_memory_root):
        raise HTTPException(status_code=400, detail="Agent 记忆文件越界，拒绝访问。")
    return target


def _validate_memory_agent_id(agent_id: str) -> str:
    normalized = agent_id.strip()
    if not normalized or normalized in {".", ".."}:
        raise HTTPException(status_code=400, detail="Agent ID 不能为空或路径保留名。")
    invalid_characters = {"/", "\\", ":", "\x00"}
    if any(character in normalized for character in invalid_characters) or ".." in normalized:
        raise HTTPException(status_code=400, detail="Agent ID 只能是 .spread-ai/memory 下的单层目录名。")
    return normalized


def _memory_entry_response(
    workspace_root: Path,
    agent_id: str,
    *,
    raw_bytes: bytes | None = None,
) -> WorkspaceMemoryEntryResponse:
    target = _resolve_agent_memory_file(workspace_root, agent_id, require_exists=True)
    stat = target.stat()
    content_bytes = raw_bytes if raw_bytes is not None else _read_memory_bytes(target)
    return WorkspaceMemoryEntryResponse(
        key=agent_id,
        agent_id=agent_id,
        relative_path=_relative_path_for_workspace(workspace_root, target),
        size_bytes=stat.st_size,
        modified_at=stat.st_mtime,
        content_sha256=_sha256_hex(content_bytes),
        summary=_memory_summary_from_bytes(content_bytes),
    )


def _read_memory_bytes(target: Path) -> bytes:
    try:
        return target.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取项目记忆失败: {exc}") from exc


def _sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _memory_summary_from_bytes(content: bytes) -> str:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return _memory_summary_from_text(text)


def _memory_summary_from_text(content: str) -> str:
    for line in content.splitlines():
        normalized = " ".join(line.strip().lstrip("#").strip().split())
        if not normalized:
            continue
        if len(normalized) <= _MEMORY_SUMMARY_MAX_LENGTH:
            return normalized
        return f"{normalized[: _MEMORY_SUMMARY_MAX_LENGTH - 1]}…"
    return ""


def _is_path_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        normalized_path = _normalise_path_for_compare(path)
        normalized_parent = _normalise_path_for_compare(parent)
        return normalized_path == normalized_parent or normalized_path.startswith(f"{normalized_parent}/")


_GIT_COMMAND_TIMEOUT_SECONDS = 3.0
_GIT_REPOSITORY_SCAN_MAX_DIRECTORIES = 5_000
_GIT_REPOSITORY_SCAN_MAX_REPOSITORIES = 100
_GIT_REPOSITORY_DISCOVERY_CACHE_TTL_SECONDS = 45.0
_GIT_REPOSITORY_DISCOVERY_CACHE_MAX_ENTRIES = 32
_GIT_SCAN_EXCLUDED_NAMES = {
    name.casefold()
    for name in _EXCLUDED_NAMES
    | {
        ".next",
        ".turbo",
        ".vite",
        "coverage",
        "out",
        "release",
        "target",
    }
}
_GitRepositoryDiscoveryCacheEntry = tuple[float, tuple[str, ...], bool]
_git_repository_discovery_cache: dict[str, _GitRepositoryDiscoveryCacheEntry] = {}
_git_repository_discovery_cache_lock = threading.Lock()


def _read_workspace_git_status(root: Path) -> WorkspaceGitStatusResponse:
    """扫描工作区下的 Git 仓库，并返回按层级顺序排列的仓库状态汇总。"""
    repository_paths, scan_truncated = _get_cached_git_repositories(root)
    repositories = [
        _read_git_repository_status(root, repository_path)
        for repository_path in _sort_repository_paths(root, repository_paths)
    ]
    root_repository = next((repository for repository in repositories if repository.relative_path == "."), None)

    if scan_truncated:
        reason = "scan_truncated"
    elif root_repository is not None:
        reason = root_repository.reason
    elif repositories:
        reason = "workspace_root_not_git_repository"
    else:
        reason = "no_git_repositories"

    return WorkspaceGitStatusResponse(
        workspace_path=str(root),
        repositories=repositories,
        is_git_repository_root=root_repository is not None,
        branch=root_repository.branch if root_repository is not None else None,
        dirty=root_repository.dirty if root_repository is not None else False,
        display_text=root_repository.display_text if root_repository is not None else None,
        reason=reason,
        scan_truncated=scan_truncated,
    )


def _get_cached_git_repositories(root: Path) -> tuple[list[Path], bool]:
    cache_key = _normalise_path_for_compare(root)
    now = time.monotonic()
    with _git_repository_discovery_cache_lock:
        cached = _git_repository_discovery_cache.get(cache_key)
        if cached is not None:
            cached_at, repository_path_texts, scan_truncated = cached
            if now - cached_at <= _GIT_REPOSITORY_DISCOVERY_CACHE_TTL_SECONDS:
                repositories = [Path(repository_path_text) for repository_path_text in repository_path_texts]
                return repositories, scan_truncated
            _git_repository_discovery_cache.pop(cache_key, None)

    repositories, scan_truncated = _discover_git_repositories(root)
    with _git_repository_discovery_cache_lock:
        _remember_git_repository_discovery(cache_key, repositories, scan_truncated, time.monotonic())
    return repositories, scan_truncated


def _remember_git_repository_discovery(
    cache_key: str,
    repositories: list[Path],
    scan_truncated: bool,
    cached_at: float,
) -> None:
    cache_full = len(_git_repository_discovery_cache) >= _GIT_REPOSITORY_DISCOVERY_CACHE_MAX_ENTRIES
    if cache_key not in _git_repository_discovery_cache and cache_full:
        oldest_cache_key = min(_git_repository_discovery_cache, key=lambda key: _git_repository_discovery_cache[key][0])
        _git_repository_discovery_cache.pop(oldest_cache_key, None)
    _git_repository_discovery_cache[cache_key] = (
        cached_at,
        tuple(str(repository) for repository in repositories),
        scan_truncated,
    )


def _discover_git_repositories(root: Path) -> tuple[list[Path], bool]:
    repositories: list[str] = []
    stack: list[str] = [str(root)]
    scanned_directories = 0

    while stack:
        directory_path = stack.pop()
        scanned_directories += 1
        if scanned_directories > _GIT_REPOSITORY_SCAN_MAX_DIRECTORIES:
            return [Path(repository_path) for repository_path in repositories], True

        if _has_git_metadata_marker(directory_path):
            repositories.append(directory_path)
            if len(repositories) >= _GIT_REPOSITORY_SCAN_MAX_REPOSITORIES:
                return [Path(repository_path) for repository_path in repositories], True

        try:
            with os.scandir(directory_path) as entries:
                for entry in entries:
                    if entry.name.casefold() in _GIT_SCAN_EXCLUDED_NAMES:
                        continue
                    try:
                        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    stack.append(entry.path)
        except OSError:
            continue

    return [Path(repository_path) for repository_path in repositories], False


def _has_git_metadata_marker(directory_path: str) -> bool:
    git_metadata_path = os.path.join(directory_path, ".git")
    return os.path.isdir(git_metadata_path) or os.path.isfile(git_metadata_path)


def _sort_repository_paths(workspace_root: Path, repository_paths: list[Path]) -> list[Path]:
    unique_repository_paths = {
        _normalise_path_for_compare(repository_path): repository_path
        for repository_path in repository_paths
    }
    return sorted(
        unique_repository_paths.values(),
        key=lambda repository_path: _repository_sort_key(workspace_root, repository_path),
    )


def _relative_path_for_workspace(workspace_root: Path, repository_path: Path) -> str:
    try:
        relative_path = repository_path.relative_to(workspace_root).as_posix()
    except ValueError:
        relative_path = repository_path.name or str(repository_path)
    relative_path = relative_path.replace("\\", "/").rstrip("/")
    return relative_path or "."


def _normalise_relative_path_for_sort(relative_path: str) -> str:
    if relative_path == ".":
        return ""
    return relative_path.replace("\\", "/").rstrip("/").casefold()


def _repository_sort_key(workspace_root: Path, repository_path: Path) -> tuple[int, str]:
    relative_path = _relative_path_for_workspace(workspace_root, repository_path)
    depth = 0 if relative_path == "." else len([segment for segment in relative_path.split("/") if segment])
    return depth, _normalise_relative_path_for_sort(relative_path)


def _read_git_repository_status(workspace_root: Path, repository_root: Path) -> WorkspaceGitRepositoryStatusResponse:
    relative_path = _relative_path_for_workspace(workspace_root, repository_root)
    top_level_ok, top_level = _run_git(repository_root, "rev-parse", "--show-toplevel")
    if not top_level_ok or not top_level:
        return _git_repository_status_response(
            repository_root,
            relative_path=relative_path,
            display_text="unknown",
            reason="git_root_unavailable",
        )

    top_level_path = Path(top_level).expanduser().resolve()
    if not _is_same_path(repository_root, top_level_path):
        return _git_repository_status_response(
            repository_root,
            relative_path=relative_path,
            display_text="unknown",
            reason="not_git_repository_root",
        )

    branch_ok, branch_output = _run_git(repository_root, "branch", "--show-current")
    branch = branch_output if branch_ok and branch_output else None
    display_ref = branch
    reason = None
    if not display_ref:
        commit_ok, commit_output = _run_git(repository_root, "rev-parse", "--short", "HEAD")
        if commit_ok and commit_output:
            display_ref = f"HEAD@{commit_output}"
            reason = "detached_head"
        else:
            display_ref = "HEAD"
            reason = "branch_unavailable"

    dirty_ok, dirty_output = _run_git(repository_root, "status", "--porcelain")
    dirty = bool(dirty_output) if dirty_ok else False
    if not dirty_ok and reason is None:
        reason = "dirty_status_unavailable"
    display_text = f"{display_ref}{'*' if dirty else ''}"

    return _git_repository_status_response(
        repository_root,
        relative_path=relative_path,
        branch=branch,
        dirty=dirty,
        display_text=display_text,
        reason=reason,
    )


def _git_repository_status_response(
    repository_root: Path,
    *,
    relative_path: str,
    branch: str | None = None,
    dirty: bool = False,
    display_text: str | None = None,
    reason: str | None = None,
) -> WorkspaceGitRepositoryStatusResponse:
    return WorkspaceGitRepositoryStatusResponse(
        repository_path=str(repository_root),
        relative_path=relative_path,
        branch=branch,
        dirty=dirty,
        display_text=display_text,
        reason=reason,
    )


def _run_git(root: Path, *args: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    return completed.returncode == 0, completed.stdout.strip()


def _is_same_path(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return _normalise_path_for_compare(left) == _normalise_path_for_compare(right)


def _normalise_path_for_compare(path: Path) -> str:
    return str(path).replace("\\", "/").rstrip("/").casefold()


def _read_children(
    directory: Path,
    *,
    depth: int,
    max_depth: int,
    max_entries_per_directory: int,
) -> list[WorkspaceTreeNodeResponse]:
    if depth > max_depth:
        return []
    try:
        entries = sorted(
            [entry for entry in directory.iterdir() if entry.name not in _EXCLUDED_NAMES],
            key=lambda entry: (entry.is_file(), entry.name.lower()),
        )[:max_entries_per_directory]
    except OSError:
        return []
    nodes: list[WorkspaceTreeNodeResponse] = []
    for entry in entries:
        if entry.is_dir():
            should_load_children = depth < max_depth
            nodes.append(
                WorkspaceTreeNodeResponse(
                    name=entry.name,
                    path=str(entry),
                    type="folder",
                    children=_read_children(
                        entry,
                        depth=depth + 1,
                        max_depth=max_depth,
                        max_entries_per_directory=max_entries_per_directory,
                    ) if should_load_children else [],
                    children_loaded=should_load_children,
                ),
            )
        elif entry.is_file():
            nodes.append(WorkspaceTreeNodeResponse(name=entry.name, path=str(entry), type="file"))
    return nodes

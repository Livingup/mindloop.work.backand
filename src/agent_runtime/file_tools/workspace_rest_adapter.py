from __future__ import annotations

import os
import subprocess
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
        root = Path(body.directory_path).expanduser().resolve()
        if not root.exists():
            raise HTTPException(status_code=404, detail=f"工作目录不存在: {root}")
        if not root.is_dir():
            raise HTTPException(status_code=400, detail=f"工作目录不是文件夹: {root}")
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
        root = Path(body.workspace_path).expanduser().resolve()
        if not root.exists():
            raise HTTPException(status_code=404, detail=f"工作目录不存在: {root}")
        if not root.is_dir():
            raise HTTPException(status_code=400, detail=f"工作目录不是文件夹: {root}")
        return _read_workspace_git_status(root)

    return router


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

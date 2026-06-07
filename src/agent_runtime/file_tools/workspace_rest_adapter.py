from __future__ import annotations

import subprocess
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


class WorkspaceTreeResponse(BaseModel):
    name: str
    path: str
    children: list[WorkspaceTreeNodeResponse] = Field(default_factory=list)


class WorkspaceGitStatusRequest(BaseModel):
    workspace_path: str = Field(description="要检查 Git 状态的工作区根目录路径。")


class WorkspaceGitStatusResponse(BaseModel):
    workspace_path: str
    is_git_repository_root: bool = Field(description="该目录本身是否为 Git 仓库根目录。")
    branch: str | None = Field(default=None, description="当前检出的 Git 分支；detached HEAD 或不可读取时为空。")
    dirty: bool = Field(default=False, description="工作区是否存在未提交变更。")
    display_text: str | None = Field(default=None, description="状态栏可直接展示的分支文本，例如 main*。")
    reason: str | None = Field(default=None, description="不可展示分支时的机器可读原因。")


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
        return _read_git_status(root)

    return router


_GIT_COMMAND_TIMEOUT_SECONDS = 3.0


def _read_git_status(root: Path) -> WorkspaceGitStatusResponse:
    """读取工作区 Git 状态；只有目录本身是 Git 仓库根目录时才返回可展示文本。"""
    if not (root / ".git").exists():
        return _git_status_response(root, is_git_repository_root=False, reason="missing_git_metadata")

    top_level_ok, top_level = _run_git(root, "rev-parse", "--show-toplevel")
    if not top_level_ok or not top_level:
        return _git_status_response(root, is_git_repository_root=False, reason="git_root_unavailable")

    top_level_path = Path(top_level).expanduser().resolve()
    if not _is_same_path(root, top_level_path):
        return _git_status_response(root, is_git_repository_root=False, reason="not_git_repository_root")

    branch_ok, branch_output = _run_git(root, "branch", "--show-current")
    branch = branch_output if branch_ok and branch_output else None
    dirty_ok, dirty_output = _run_git(root, "status", "--porcelain")
    dirty = bool(dirty_output) if dirty_ok else False
    display_text = f"{branch}{'*' if dirty else ''}" if branch else None
    return _git_status_response(
        root,
        is_git_repository_root=True,
        branch=branch,
        dirty=dirty,
        display_text=display_text,
        reason=None if display_text else "branch_unavailable",
    )


def _git_status_response(
    root: Path,
    *,
    is_git_repository_root: bool,
    branch: str | None = None,
    dirty: bool = False,
    display_text: str | None = None,
    reason: str | None = None,
) -> WorkspaceGitStatusResponse:
    return WorkspaceGitStatusResponse(
        workspace_path=str(root),
        is_git_repository_root=is_git_repository_root,
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
                    ),
                ),
            )
        elif entry.is_file():
            nodes.append(WorkspaceTreeNodeResponse(name=entry.name, path=str(entry), type="file"))
    return nodes

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


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

    return router


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

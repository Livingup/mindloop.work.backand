# -*- coding: utf-8 -*-
"""全局最近工作区索引 — 领域实体与仓储契约。

索引文件 ``~/.spread-ai/recent-workspaces.json`` 记录用户最近打开过哪些 workspace 目录，
用于前端展示"最近项目"列表。索引是纯展示辅助数据，丢失不影响项目本身的数据完整性
（项目数据存于 ``<workspace>/.spread-ai/`` 下）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RecentWorkspaceEntry:
    """最近打开过的 workspace 索引条目。

    workspace_path 是主键；同一 workspace_path 在索引中只有一条（更新而非追加）。
    project_id / project_name 从 manifest 同步而来，便于前端无需逐个读 manifest 就能展示。
    """

    workspace_path: str
    project_id: str
    project_name: str
    last_opened_at: datetime


class RecentWorkspacesIndex(Protocol):
    """``~/.spread-ai/recent-workspaces.json`` 读写契约。"""

    def list(self) -> list[RecentWorkspaceEntry]:
        """列出全部条目，按 last_opened_at 倒序排列。"""
        ...

    def put(self, entry: RecentWorkspaceEntry) -> None:
        """插入或更新条目（按 workspace_path 为唯一键）。"""
        ...

    def remove(self, workspace_path: str) -> bool:
        """按 workspace_path 删除条目。

        Returns:
            True 表示有条目被删除；False 表示条目不存在（幂等）。
        """
        ...

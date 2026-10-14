# -*- coding: utf-8 -*-
"""文件系统实现的全局最近工作区索引。

索引文件 ``~/.spread-ai/recent-workspaces.json``，结构：

.. code-block:: json

    {
      "version": 1,
      "workspaces": [
        {
          "workspace_path": "...",
          "project_id": "...",
          "project_name": "...",
          "last_opened_at": "ISO 8601"
        }
      ]
    }

写入使用 tmp + rename 原子替换；读取损坏时降级为空列表（索引丢失不影响项目本身）。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from agent_runtime.scene.recent_workspaces import RecentWorkspaceEntry

logger = logging.getLogger(__name__)


class FileRecentWorkspacesIndex:
    """``~/.spread-ai/recent-workspaces.json`` 文件系统实现。

    通过 accessor 函数注入索引文件路径，支持配置热更新。
    """

    def __init__(self, index_path_accessor: Callable[[], str]) -> None:
        self._index_path_accessor = index_path_accessor
        self._lock = RLock()

    def list(self) -> list[RecentWorkspaceEntry]:
        """列出全部条目，按 last_opened_at 倒序。"""
        with self._lock:
            entries = self._load()
            entries.sort(key=lambda e: e.last_opened_at, reverse=True)
            return entries

    def put(self, entry: RecentWorkspaceEntry) -> None:
        """插入或更新条目（按 workspace_path 主键去重）。"""
        with self._lock:
            entries = self._load()
            filtered = [e for e in entries if e.workspace_path != entry.workspace_path]
            filtered.append(entry)
            self._save(filtered)

    def remove(self, workspace_path: str) -> bool:
        """按 workspace_path 删除；幂等。"""
        with self._lock:
            entries = self._load()
            filtered = [e for e in entries if e.workspace_path != workspace_path]
            if len(filtered) == len(entries):
                return False
            self._save(filtered)
            return True

    # ── 内部 ───────────────────────────────────────────────────────

    def _load(self) -> list[RecentWorkspaceEntry]:
        """加载索引文件；不存在或损坏返回空列表。"""
        path = self._path()
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            items = data.get("workspaces") or []
            return [_dict_to_entry(item) for item in items]
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("读取 recent-workspaces 索引失败 path=%s err=%s", path, exc)
            return []

    def _save(self, entries: list[RecentWorkspaceEntry]) -> None:
        """原子写入索引文件。"""
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "version": 1,
            "workspaces": [_entry_to_dict(e) for e in entries],
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=".recent-",
            suffix=".json.tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _path(self) -> Path:
        """索引文件绝对路径。"""
        return Path(self._index_path_accessor()).expanduser()


def _entry_to_dict(entry: RecentWorkspaceEntry) -> dict[str, Any]:
    return {
        "workspace_path": entry.workspace_path,
        "project_id": entry.project_id,
        "project_name": entry.project_name,
        "last_opened_at": entry.last_opened_at.isoformat(),
    }


def _dict_to_entry(data: dict[str, Any]) -> RecentWorkspaceEntry:
    return RecentWorkspaceEntry(
        workspace_path=data["workspace_path"],
        project_id=data["project_id"],
        project_name=data["project_name"],
        last_opened_at=datetime.fromisoformat(data["last_opened_at"]),
    )

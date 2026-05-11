# -*- coding: utf-8 -*-
"""文件系统实现的项目 manifest 仓储。

读写 ``<workspace>/.spread-ai/project.json``。
写入采用 tmp 文件 + ``os.replace`` 保证原子性，避免进程崩溃导致 manifest 损坏。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any

from agent_runtime.scene.project import Project

logger = logging.getLogger(__name__)

# 项目隐藏目录名，与 ``.git`` / ``.vscode`` 等惯例一致，存储项目本地数据。
HIDDEN_DIR_NAME = ".spread-ai"

# 项目 manifest 文件名（位于隐藏目录内）。
MANIFEST_FILE_NAME = "project.json"


class FileProjectManifestRepository:
    """``<workspace>/.spread-ai/project.json`` 文件系统仓储实现。

    线程安全：内部 RLock 保护 read/write/delete 互斥；本机多进程访问由 OS 文件原子替换语义兜底。
    """

    def __init__(self) -> None:
        self._lock = RLock()

    def read(self, workspace_path: str) -> Project | None:
        """读取 manifest；文件不存在或解析失败时返回 None 不抛错。"""
        path = self._manifest_path(workspace_path)
        with self._lock:
            if not path.exists():
                return None
            try:
                raw = path.read_text(encoding="utf-8")
                data = json.loads(raw)
                return _dict_to_project(data)
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                logger.warning(
                    "读取 project manifest 失败 workspace=%s path=%s err=%s",
                    workspace_path, path, exc,
                )
                return None

    def write(self, workspace_path: str, project: Project) -> None:
        """原子写入 manifest（自动创建 ``.spread-ai/`` 目录）。"""
        path = self._manifest_path(workspace_path)
        payload = _project_to_dict(project)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            # tmpfile 必须与目标在同一目录，否则 rename 跨文件系统不原子。
            fd, tmp_name = tempfile.mkstemp(
                prefix=".project-",
                suffix=".json.tmp",
                dir=str(path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                # os.replace 在 Windows 与 POSIX 都是原子替换。
                os.replace(tmp_name, path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

    def exists(self, workspace_path: str) -> bool:
        """判断 manifest 文件是否存在。"""
        return self._manifest_path(workspace_path).exists()

    def delete(self, workspace_path: str) -> bool:
        """删除 manifest 文件；幂等。"""
        path = self._manifest_path(workspace_path)
        with self._lock:
            if not path.exists():
                return False
            path.unlink()
            return True

    def _manifest_path(self, workspace_path: str) -> Path:
        """``<workspace>/.spread-ai/project.json`` 的绝对路径。"""
        return Path(workspace_path).expanduser() / HIDDEN_DIR_NAME / MANIFEST_FILE_NAME


def _project_to_dict(project: Project) -> dict[str, Any]:
    """Project 实体 -> JSON 可序列化 dict。"""
    return {
        "version": 1,
        "project_id": project.project_id,
        "name": project.name,
        "created_at": project.created_at.isoformat(),
        "last_opened_at": project.last_opened_at.isoformat(),
        "metadata": dict(project.metadata),
    }


def _dict_to_project(data: dict[str, Any]) -> Project:
    """JSON dict -> Project 实体。"""
    return Project(
        project_id=data["project_id"],
        name=data["name"],
        created_at=datetime.fromisoformat(data["created_at"]),
        last_opened_at=datetime.fromisoformat(data["last_opened_at"]),
        metadata=dict(data.get("metadata") or {}),
    )

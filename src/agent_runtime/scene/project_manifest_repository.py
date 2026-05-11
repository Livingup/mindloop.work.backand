# -*- coding: utf-8 -*-
"""项目 manifest 仓储契约（Protocol）。

manifest 层的职责：读写 ``<workspace>/.spread-ai/project.json`` 作为项目永久身份凭证，
使项目目录即便被移动或重命名也能保证 project_id 与项目数据绑定关系不丢失。
领域服务通过该契约操作项目身份，不关心具体的文件系统实现细节。
"""
from __future__ import annotations

from typing import Protocol

from agent_runtime.scene.project import Project


class ProjectManifestRepository(Protocol):
    """``<workspace>/.spread-ai/project.json`` 读写契约。"""

    def read(self, workspace_path: str) -> Project | None:
        """读取指定 workspace 下的项目 manifest。

        Args:
            workspace_path: workspace 根目录绝对路径。

        Returns:
            解析成功时返回 Project 聚合；文件不存在或内容损坏时返回 None。
        """
        ...

    def write(self, workspace_path: str, project: Project) -> None:
        """原子写入项目 manifest。

        实现方应自动创建 ``.spread-ai/`` 目录并保证原子性（tmp 文件 + rename）。

        Args:
            workspace_path: workspace 根目录绝对路径。
            project: 要持久化的项目聚合。
        """
        ...

    def exists(self, workspace_path: str) -> bool:
        """判断指定 workspace 下是否已存在 manifest 文件。"""
        ...

    def delete(self, workspace_path: str) -> bool:
        """删除 manifest 文件（不清理其他 ``.spread-ai/`` 内容）。

        Returns:
            True 表示有文件被删除；False 表示文件不存在（幂等）。
        """
        ...

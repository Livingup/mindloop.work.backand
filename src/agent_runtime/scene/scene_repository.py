# -*- coding: utf-8 -*-
"""Scene 模块仓储接口（Protocol）。

定义在领域层，实现在基础设施层（依赖反转）。
单一仓储同时管理 Project 与 Scene 两类聚合根，简化工厂装配；删除 project 由仓储层
通过 SQLite 外键 ON DELETE CASCADE 自动级联删除关联 scenes。
"""
from __future__ import annotations

from typing import Protocol

from agent_runtime.scene.project import Project
from agent_runtime.scene.scene import Scene


class SceneRepository(Protocol):
    """Project / Scene 仓储接口。"""

    # ── Project ────────────────────────────────────────────────────

    def create_project(self, project: Project) -> Project:
        """创建项目。

        Raises:
            ValueError: project_id 已存在或 workspace_path 已被其他项目占用。
        """
        ...

    def save_project(self, project: Project) -> Project:
        """保存项目当前状态（upsert 语义）。用于更新 name / last_opened_at / metadata。"""
        ...

    def get_project(self, project_id: str) -> Project | None:
        """按 project_id 获取项目；不存在返回 None。"""
        ...

    def get_project_by_workspace_path(self, workspace_path: str) -> Project | None:
        """按 workspace_path 查找项目；不存在返回 None。

        前端打开工作区时优先按路径查找，命中则复用，未命中则新建项目。
        """
        ...

    def list_projects(self) -> list[Project]:
        """列出全部项目，按 last_opened_at 倒序。"""
        ...

    def delete_project(self, project_id: str) -> bool:
        """删除项目，关联 scenes 通过外键级联删除。

        Returns:
            True 表示有项目被删除；False 表示项目不存在（幂等）。
        """
        ...

    # ── Scene ──────────────────────────────────────────────────────

    def create_scene(self, scene: Scene) -> Scene:
        """创建场景。

        Raises:
            ValueError: scene_id 已存在。
            KeyError: project_id 不存在（外键校验由仓储层负责）。
        """
        ...

    def save_scene(self, scene: Scene) -> Scene:
        """保存场景当前状态（upsert 语义）。"""
        ...

    def get_scene(self, scene_id: str) -> Scene | None:
        """按 scene_id 获取场景；不存在返回 None。"""
        ...

    def list_scenes_by_project(self, project_id: str) -> list[Scene]:
        """列出指定项目下的全部场景，按 last_active_at 倒序。"""
        ...

    def delete_scene(self, scene_id: str) -> bool:
        """删除场景。

        Returns:
            True 表示有场景被删除；False 表示场景不存在（幂等）。
        """
        ...

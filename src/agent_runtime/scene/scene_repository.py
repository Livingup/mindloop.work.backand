# -*- coding: utf-8 -*-
"""Scene 模块仓储接口（Protocol）。

定义在领域层，实现在基础设施层（依赖反转）。

**重要**：本仓储只负责 Scene 聚合的持久化，**不再管理 Project**；
Project 身份由 :class:`agent_runtime.scene.project_manifest_repository.ProjectManifestRepository`
存储到 ``<workspace>/.spread-ai/project.json``。

Scene 数据按 workspace 隔离存储于 ``<workspace>/.spread-ai/scenes.db``，
所有 CRUD 方法都需要传入 ``workspace_path`` 来路由到正确的物理数据库。
"""
from __future__ import annotations

from typing import Protocol

from agent_runtime.scene.scene import Scene


class SceneRepository(Protocol):
    """Scene 仓储接口。所有方法以 ``workspace_path`` 路由到对应 workspace 的 scenes.db。"""

    def ensure_initialized(self, workspace_path: str) -> None:
        """确保指定 workspace 的 scenes.db 与 schema 已就绪。

        领域服务在 ``open_or_create_project`` 流程中调用，触发首次连接 + 跑 schema 初始化 +
        必要的迁移；后续 CRUD 方法可信赖该 workspace 的连接状态正常。
        """
        ...

    def create_scene(self, workspace_path: str, scene: Scene) -> Scene:
        """在指定 workspace 下创建场景。

        Raises:
            ValueError: scene_id 已存在。
        """
        ...

    def save_scene(self, workspace_path: str, scene: Scene) -> Scene:
        """保存场景当前状态（upsert 语义）。"""
        ...

    def get_scene(self, workspace_path: str, scene_id: str) -> Scene | None:
        """按 scene_id 获取场景；不存在返回 None。"""
        ...

    def list_scenes_by_project(self, workspace_path: str, project_id: str) -> list[Scene]:
        """列出指定 workspace 下属于该项目的全部场景，按 last_active_at 倒序。

        ``project_id`` 在 per-workspace 数据库中冗余（一份 scenes.db 通常只服务一个 project_id），
        但保留参数以便领域服务做一致性校验：若数据库内出现非该项目的 scene 则视为污染并过滤。
        """
        ...

    def delete_scene(self, workspace_path: str, scene_id: str) -> bool:
        """删除场景。

        Returns:
            True 表示有场景被删除；False 表示场景不存在（幂等）。
        """
        ...

    def close(self) -> None:
        """关闭全部连接（进程退出时由组合根调用）。"""
        ...

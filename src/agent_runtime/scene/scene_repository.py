# -*- coding: utf-8 -*-
"""Scene 模块仓储接口（Protocol）。

定义在领域层，实现在基础设施层（依赖反转）。

**重要**：本仓储只负责 Scene 聚合的持久化，**不再管理 Project**；
Project 身份由 :class:`agent_runtime.scene.project_manifest_repository.ProjectManifestRepository`
存储到 ``<workspace>/.spread-ai/project.json``。

Scene 详情数据持久化到全局统一 SQLite 数据库（``SceneConfig.scenes_db_path``，
默认 ``~/.spread-ai/scene/scene.db``），所有 workspace 下的 scene 共享一份物理数据库；
每条 scene 通过 ``project_id`` 列归属到具体项目，``project_id`` 与 manifest 中的
``project.project_id`` 强一致。
"""
from __future__ import annotations

from typing import Protocol

from agent_runtime.scene.scene import Scene


class SceneRepository(Protocol):
    """Scene 仓储接口。

    所有方法以 ``scene_id`` / ``project_id`` 直接路由到全局 scene 数据库的具体行，
    不再以 ``workspace_path`` 作为路由参数；workspace 与 project 的对应关系由
    :class:`agent_runtime.scene.scene_service.SceneService` 通过 manifest 校验。
    """

    def ensure_initialized(self) -> None:
        """确保全局 scene 数据库与 schema 已就绪。

        领域服务在 ``open_or_create_project`` 流程中（以及组合根装配时）调用，
        触发首次连接 + 建目录 + 跑 schema 初始化 + 必要的历史迁移；
        后续 CRUD 方法可信赖连接状态正常。多次调用幂等。
        """
        ...

    def create_scene(self, scene: Scene) -> Scene:
        """创建场景。

        Raises:
            ValueError: scene_id 已存在。
        """
        ...

    def save_scene(self, scene: Scene) -> Scene:
        """保存场景当前状态（upsert 语义）。"""
        ...

    def get_scene(self, scene_id: str) -> Scene | None:
        """按 scene_id 获取场景；不存在返回 None。"""
        ...

    def list_scenes_by_project(self, project_id: str) -> list[Scene]:
        """列出属于该项目的全部场景，按 last_active_at 倒序。"""
        ...

    def delete_scene(self, scene_id: str) -> bool:
        """删除场景。

        Returns:
            True 表示有场景被删除；False 表示场景不存在（幂等）。
        """
        ...

    def close(self) -> None:
        """关闭底层连接（进程退出时由组合根调用）。"""
        ...

# -*- coding: utf-8 -*-
"""Scene 模块依赖装配。

装配链路（自下而上）：

1. :class:`FileProjectManifestRepository` — 读写 ``<workspace>/.spread-ai/project.json``。
2. :class:`FileRecentWorkspacesIndex`     — 读写全局 ``recent-workspaces.json``。
3. :class:`SqliteSceneRepository`         — 读写全局 ``scenes_db_path`` 单一 SQLite 数据库。
4. :class:`SceneService`                  — 编排上述三层仓储。
5. :class:`SceneGatewayImpl`              — 暴露北向 Gateway 契约。
"""
from __future__ import annotations

from agent_runtime.scene.infrastructure.file_project_manifest_repository import (
    FileProjectManifestRepository,
)
from agent_runtime.scene.infrastructure.file_recent_workspaces_index import (
    FileRecentWorkspacesIndex,
)
from agent_runtime.scene.infrastructure.sqlite_scene_repository import (
    SqliteSceneRepository,
)
from agent_runtime.scene.north_gateway.scene_gateway_impl import SceneGatewayImpl
from agent_runtime.scene.scene_service import SceneService


class SceneFactory:
    """Scene 模块工厂。"""

    @staticmethod
    def create() -> SceneGatewayImpl:
        """创建 Scene 北向网关实例。

        配置通过访问器从 ConfigRegistry 实时读取，不存储配置副本。
        """
        from agent_runtime.config import ConfigRegistry

        manifest_repo = FileProjectManifestRepository()
        recent_index = FileRecentWorkspacesIndex(
            index_path_accessor=lambda: ConfigRegistry.instance().scene.recent_workspaces_path,
        )
        scene_repo = SqliteSceneRepository(
            db_path_accessor=lambda: ConfigRegistry.instance().scene.scenes_db_path,
        )
        service = SceneService(
            manifest_repo=manifest_repo,
            recent_index=recent_index,
            scene_repo=scene_repo,
        )
        return SceneGatewayImpl(service)

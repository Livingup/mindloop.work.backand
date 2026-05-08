# -*- coding: utf-8 -*-
"""Scene 模块依赖装配。"""
from __future__ import annotations

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
        装配链路：SQLite 仓储 → 领域服务 → 北向网关。
        """
        from agent_runtime.config import ConfigRegistry

        repo = SqliteSceneRepository(
            data_dir_accessor=lambda: ConfigRegistry.instance().scene.data_dir,
        )
        service = SceneService(repo)
        return SceneGatewayImpl(service)

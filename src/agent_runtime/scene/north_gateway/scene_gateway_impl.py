# -*- coding: utf-8 -*-
"""Scene 北向网关实现。"""
from __future__ import annotations

from agent_runtime.scene.project import Project
from agent_runtime.scene.scene import Scene
from agent_runtime.scene.scene_service import SceneService
from agent_runtime.scene.north_gateway.scene_contracts import (
    CreateProjectRequest,
    CreateSceneRequest,
    OpenOrCreateProjectRequest,
    ProjectInfo,
    SceneInfo,
    UpdateProjectRequest,
    UpdateSceneRequest,
)


class SceneGatewayImpl:
    """Scene 模块北向网关实现。

    将领域服务的返回值转换为北向网关 DTO，请求模型解构为服务层方法参数。
    """

    def __init__(self, service: SceneService) -> None:
        self._service = service

    # ── Project ────────────────────────────────────────────────────

    def open_or_create_project(self, request: OpenOrCreateProjectRequest) -> ProjectInfo:
        """打开或创建项目（幂等）。"""
        project = self._service.open_or_create_project(
            workspace_path=request.workspace_path,
            name=request.name,
        )
        return _project_to_info(project)

    def create_project(self, request: CreateProjectRequest) -> ProjectInfo:
        """显式新建项目。"""
        project = self._service.create_project(
            workspace_path=request.workspace_path,
            name=request.name,
        )
        return _project_to_info(project)

    def get_project(self, project_id: str) -> ProjectInfo:
        """按 project_id 获取项目。"""
        return _project_to_info(self._service.get_project(project_id))

    def list_projects(self) -> list[ProjectInfo]:
        """列出全部项目。"""
        return [_project_to_info(p) for p in self._service.list_projects()]

    def update_project(self, project_id: str, request: UpdateProjectRequest) -> ProjectInfo:
        """更新项目可变字段。"""
        project = self._service.get_project(project_id)
        if request.name is not None:
            project = self._service.rename_project(project_id, request.name)
        if request.metadata is not None:
            project = self._service.update_project_metadata(project_id, request.metadata)
        return _project_to_info(project)

    def touch_project_opened(self, project_id: str) -> ProjectInfo:
        """显式刷新项目 last_opened_at。"""
        return _project_to_info(self._service.touch_project_opened(project_id))

    def delete_project(self, project_id: str) -> bool:
        """删除项目。"""
        return self._service.delete_project(project_id)

    # ── Scene ──────────────────────────────────────────────────────

    def create_scene(self, request: CreateSceneRequest) -> SceneInfo:
        """创建场景（不含 instance 创建，instance 创建由 attach 编排层负责）。"""
        scene = self._service.create_scene(
            project_id=request.project_id,
            name=request.name,
            bound_agent_definition_id=request.bound_agent_definition_id,
            description=request.description,
            metadata=request.metadata,
        )
        return _scene_to_info(scene)

    def get_scene(self, scene_id: str) -> SceneInfo:
        """按 scene_id 获取场景。"""
        return _scene_to_info(self._service.get_scene(scene_id))

    def list_scenes_by_project(self, project_id: str) -> list[SceneInfo]:
        """列出指定项目下的全部场景。"""
        return [_scene_to_info(s) for s in self._service.list_scenes_by_project(project_id)]

    def update_scene(self, scene_id: str, request: UpdateSceneRequest) -> SceneInfo:
        """更新场景。bound_agent_definition_id / bound_root_conversation_id 不可通过本接口修改。"""
        scene = self._service.update_scene(
            scene_id,
            name=request.name,
            description=request.description,
            status=request.status,
            metadata=request.metadata,
        )
        return _scene_to_info(scene)

    def set_bound_root_conversation_id(self, scene_id: str, conversation_id: str) -> SceneInfo:
        """在 scene 下首次创建 root instance 后回填 conversation_id。"""
        scene = self._service.set_bound_root_conversation_id(scene_id, conversation_id)
        return _scene_to_info(scene)

    def touch_scene_active(self, scene_id: str) -> SceneInfo:
        """显式刷新场景 last_active_at。"""
        return _scene_to_info(self._service.touch_scene_active(scene_id))

    def delete_scene(self, scene_id: str) -> bool:
        """删除场景。"""
        return self._service.delete_scene(scene_id)


# ── 实体到 DTO 的转换 ─────────────────────────────────────────────


def _project_to_info(project: Project) -> ProjectInfo:
    """把 Project 实体转为 ProjectInfo DTO。"""
    return ProjectInfo(
        project_id=project.project_id,
        name=project.name,
        workspace_path=project.workspace_path,
        created_at=project.created_at,
        last_opened_at=project.last_opened_at,
        metadata=dict(project.metadata),
    )


def _scene_to_info(scene: Scene) -> SceneInfo:
    """把 Scene 实体转为 SceneInfo DTO。"""
    return SceneInfo(
        scene_id=scene.scene_id,
        project_id=scene.project_id,
        name=scene.name,
        description=scene.description,
        status=scene.status,
        bound_agent_definition_id=scene.bound_agent_definition_id,
        created_at=scene.created_at,
        last_active_at=scene.last_active_at,
        bound_root_conversation_id=scene.bound_root_conversation_id,
        metadata=dict(scene.metadata),
    )

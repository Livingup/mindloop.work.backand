# -*- coding: utf-8 -*-
"""Scene 北向网关实现。"""
from __future__ import annotations

from agent_runtime.scene.project import Project
from agent_runtime.scene.recent_workspaces import RecentWorkspaceEntry
from agent_runtime.scene.scene import Scene
from agent_runtime.scene.scene_service import SceneService
from agent_runtime.scene.north_gateway.scene_contracts import (
    CreateSceneRequest,
    OpenOrCreateProjectRequest,
    ProjectInfo,
    RecentWorkspaceInfo,
    SceneInfo,
    UpdateProjectRequest,
    UpdateSceneRequest,
)


class SceneGatewayImpl:
    """Scene 模块北向网关实现。

    将领域服务的返回值转换为北向网关 DTO，请求模型解构为服务层方法参数。
    Project 操作以 ``workspace_path`` 读 manifest 定位项目身份；Scene 详情统一存于
    全局 SQLite 数据库，服务通过 manifest.project_id 与 scene.project_id 交叉校验
    保证跨 workspace 隔离。
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

    def get_project_by_workspace_path(self, workspace_path: str) -> ProjectInfo:
        """读取指定 workspace 的项目 manifest。"""
        return _project_to_info(self._service.get_project_by_workspace_path(workspace_path))

    def list_recent_workspaces(self) -> list[RecentWorkspaceInfo]:
        """列出最近打开的 workspace 索引。"""
        return [_entry_to_info(e) for e in self._service.list_recent_workspaces()]

    def update_project(
        self,
        workspace_path: str,
        request: UpdateProjectRequest,
    ) -> ProjectInfo:
        """更新项目可变字段（name / metadata）。"""
        project: Project = self._service.get_project_by_workspace_path(workspace_path)
        if request.name is not None:
            project = self._service.rename_project(workspace_path, request.name)
        if request.metadata is not None:
            project = self._service.update_project_metadata(workspace_path, request.metadata)
        return _project_to_info(project)

    def touch_project_opened(self, workspace_path: str) -> ProjectInfo:
        """显式刷新项目 ``last_opened_at`` 并同步 recent 索引。"""
        return _project_to_info(self._service.touch_project_opened(workspace_path))

    def forget_workspace(self, workspace_path: str) -> bool:
        """从 recent 索引中移除该 workspace。"""
        return self._service.forget_workspace(workspace_path)

    # ── Scene ──────────────────────────────────────────────────────

    def create_scene(self, request: CreateSceneRequest) -> SceneInfo:
        """创建场景（instance 创建由 attach 编排层负责）。"""
        scene = self._service.create_scene(
            request.workspace_path,
            project_id=request.project_id,
            name=request.name,
            bound_agent_definition_id=request.bound_agent_definition_id,
            description=request.description,
            metadata=request.metadata,
        )
        return _scene_to_info(scene)

    def get_scene(self, workspace_path: str, scene_id: str) -> SceneInfo:
        """按 scene_id 获取场景。"""
        return _scene_to_info(self._service.get_scene(workspace_path, scene_id))

    def list_scenes_by_project(
        self,
        workspace_path: str,
        project_id: str,
    ) -> list[SceneInfo]:
        """列出指定 workspace 下属于该项目的全部场景。"""
        return [
            _scene_to_info(s)
            for s in self._service.list_scenes_by_project(workspace_path, project_id)
        ]

    def update_scene(
        self,
        workspace_path: str,
        scene_id: str,
        request: UpdateSceneRequest,
    ) -> SceneInfo:
        """更新场景。``bound_agent_definition_id / bound_root_conversation_id`` 不可通过本接口修改。"""
        scene = self._service.update_scene(
            workspace_path,
            scene_id,
            name=request.name,
            description=request.description,
            status=request.status,
            metadata=request.metadata,
        )
        return _scene_to_info(scene)

    def set_bound_root_conversation_id(
        self,
        workspace_path: str,
        scene_id: str,
        conversation_id: str,
    ) -> SceneInfo:
        """在 scene 下首次创建 root instance 后回填 conversation_id。"""
        scene = self._service.set_bound_root_conversation_id(
            workspace_path, scene_id, conversation_id,
        )
        return _scene_to_info(scene)

    def touch_scene_active(self, workspace_path: str, scene_id: str) -> SceneInfo:
        """显式刷新场景 ``last_active_at``。"""
        return _scene_to_info(self._service.touch_scene_active(workspace_path, scene_id))

    def delete_scene(self, workspace_path: str, scene_id: str) -> bool:
        """删除场景。"""
        return self._service.delete_scene(workspace_path, scene_id)


# ── 实体到 DTO 的转换 ─────────────────────────────────────────────


def _project_to_info(project: Project) -> ProjectInfo:
    """把 Project 实体转为 ProjectInfo DTO。"""
    return ProjectInfo(
        project_id=project.project_id,
        name=project.name,
        created_at=project.created_at,
        last_opened_at=project.last_opened_at,
        metadata=dict(project.metadata),
    )


def _entry_to_info(entry: RecentWorkspaceEntry) -> RecentWorkspaceInfo:
    """把 RecentWorkspaceEntry 实体转为 RecentWorkspaceInfo DTO。"""
    return RecentWorkspaceInfo(
        workspace_path=entry.workspace_path,
        project_id=entry.project_id,
        project_name=entry.project_name,
        last_opened_at=entry.last_opened_at,
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

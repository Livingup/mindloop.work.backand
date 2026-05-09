# -*- coding: utf-8 -*-
"""Scene REST 接入适配。

挂载 /api/scene/projects 和 /api/scene/scenes 两组路由。
依赖 SceneGateway Protocol，不直接依赖具体网关实现。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.scene.north_gateway.scene_contracts import (
    CreateProjectRequest,
    CreateSceneRequest,
    OpenOrCreateProjectRequest,
    ProjectInfo,
    SceneGateway,
    SceneInfo,
    UpdateProjectRequest,
    UpdateSceneRequest,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════
# Pydantic 模型 — REST 入参 / 出参
# ══════════════════════════════════════════


class ProjectResponse(BaseModel):
    """项目响应模型。"""

    model_config = ConfigDict(populate_by_name=True)

    project_id: str
    name: str
    workspace_path: str
    created_at: datetime
    last_opened_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class SceneResponse(BaseModel):
    """场景响应模型。

    bound_agent_definition_id：scene 创建时选定、永久绑定不可变的主 Agent 定义。
    bound_root_conversation_id：scene 首次创建 root instance 后由后端 attach 编排层回填，跨 instance 重建复用以延续 LLM 历史。
    """

    model_config = ConfigDict(populate_by_name=True)

    scene_id: str
    project_id: str
    name: str
    description: str
    status: str
    bound_agent_definition_id: str
    created_at: datetime
    last_active_at: datetime
    bound_root_conversation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class OpenOrCreateProjectBody(BaseModel):
    """打开或创建项目请求体。"""

    workspace_path: str = Field(..., description="项目目录绝对路径。")
    name: str | None = Field(None, description="可选项目名；为空时取 workspace_path basename。")


class CreateProjectBody(BaseModel):
    """显式新建项目请求体。"""

    workspace_path: str = Field(..., description="项目目录绝对路径。")
    name: str | None = Field(None, description="可选项目名；为空时取 workspace_path basename。")


class UpdateProjectBody(BaseModel):
    """更新项目请求体。"""

    name: str | None = None
    metadata: dict[str, Any] | None = None


class CreateSceneBody(BaseModel):
    """创建场景请求体。

    bound_agent_definition_id 必填且创建后不可修改；LLM 策略不在 scene 维度存储。
    首个 instance 的 LLM 策略由 POST /api/agent-runtime/scenes/{scene_id}/attach 接口决定。
    """

    project_id: str = Field(..., description="所属项目 ID。")
    name: str = Field(..., description="场景可读名。")
    bound_agent_definition_id: str = Field(
        ...,
        description="永久绑定的主 Agent 定义 ID，创建后不可修改。",
    )
    description: str = Field("", description="场景长描述。")
    metadata: dict[str, Any] | None = None


class UpdateSceneBody(BaseModel):
    """更新场景请求体。

    bound_agent_definition_id / bound_root_conversation_id 不允许修改，本请求体不提供这些字段。
    """

    name: str | None = None
    description: str | None = None
    status: str | None = Field(None, description="active / archived")
    metadata: dict[str, Any] | None = None


def _project_info_to_response(info: ProjectInfo) -> ProjectResponse:
    """ProjectInfo DTO -> ProjectResponse Pydantic。"""
    return ProjectResponse(
        project_id=info.project_id,
        name=info.name,
        workspace_path=info.workspace_path,
        created_at=info.created_at,
        last_opened_at=info.last_opened_at,
        metadata=dict(info.metadata),
    )


def _scene_info_to_response(info: SceneInfo) -> SceneResponse:
    """SceneInfo DTO -> SceneResponse Pydantic。"""
    return SceneResponse(
        scene_id=info.scene_id,
        project_id=info.project_id,
        name=info.name,
        description=info.description,
        status=info.status,
        bound_agent_definition_id=info.bound_agent_definition_id,
        created_at=info.created_at,
        last_active_at=info.last_active_at,
        bound_root_conversation_id=info.bound_root_conversation_id,
        metadata=dict(info.metadata),
    )


# ══════════════════════════════════════════
# 路由工厂
# ══════════════════════════════════════════


def create_scene_router(gateway: SceneGateway) -> APIRouter:
    """构造 Scene 模块 REST 路由。

    路由前缀：/api/scene
    - /api/scene/projects        — 项目集合
    - /api/scene/projects/{id}   — 项目实体
    - /api/scene/scenes          — 场景集合
    - /api/scene/scenes/{id}     — 场景实体
    """
    router = APIRouter(prefix="/api/scene", tags=["scene"])

    # ── Project ────────────────────────────────────────────────────

    @router.get("/projects", response_model=list[ProjectResponse])
    def list_projects() -> list[ProjectResponse]:
        """列出全部项目。"""
        return [_project_info_to_response(p) for p in gateway.list_projects()]

    @router.post("/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
    def create_project(body: CreateProjectBody) -> ProjectResponse:
        """显式新建项目。"""
        try:
            info = gateway.create_project(
                CreateProjectRequest(workspace_path=body.workspace_path, name=body.name),
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return _project_info_to_response(info)

    @router.post("/projects/open-or-create", response_model=ProjectResponse)
    def open_or_create_project(body: OpenOrCreateProjectBody) -> ProjectResponse:
        """打开或创建项目（幂等）。"""
        try:
            info = gateway.open_or_create_project(
                OpenOrCreateProjectRequest(workspace_path=body.workspace_path, name=body.name),
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return _project_info_to_response(info)

    @router.get("/projects/{project_id}", response_model=ProjectResponse)
    def get_project(project_id: str) -> ProjectResponse:
        """按 project_id 获取项目。"""
        try:
            info = gateway.get_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return _project_info_to_response(info)

    @router.patch("/projects/{project_id}", response_model=ProjectResponse)
    def update_project(project_id: str, body: UpdateProjectBody) -> ProjectResponse:
        """更新项目。"""
        try:
            info = gateway.update_project(
                project_id,
                UpdateProjectRequest(name=body.name, metadata=body.metadata),
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return _project_info_to_response(info)

    @router.post("/projects/{project_id}/touch", response_model=ProjectResponse)
    def touch_project(project_id: str) -> ProjectResponse:
        """显式刷新项目 last_opened_at。"""
        try:
            info = gateway.touch_project_opened(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return _project_info_to_response(info)

    @router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_project(project_id: str) -> None:
        """删除项目（关联场景级联删除）。"""
        deleted = gateway.delete_project(project_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"项目不存在: {project_id}",
            )

    @router.get("/projects/{project_id}/scenes", response_model=list[SceneResponse])
    def list_scenes_by_project(project_id: str) -> list[SceneResponse]:
        """列出指定项目下的全部场景。"""
        try:
            infos = gateway.list_scenes_by_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return [_scene_info_to_response(s) for s in infos]

    # ── Scene ──────────────────────────────────────────────────────

    @router.post("/scenes", response_model=SceneResponse, status_code=status.HTTP_201_CREATED)
    def create_scene(body: CreateSceneBody) -> SceneResponse:
        """创建场景。

        仅创建 scene 记录并绑定主 Agent 定义；instance 创建与连接交由
        POST /api/agent-runtime/scenes/{scene_id}/attach 负责。
        """
        try:
            info = gateway.create_scene(
                CreateSceneRequest(
                    project_id=body.project_id,
                    name=body.name,
                    bound_agent_definition_id=body.bound_agent_definition_id,
                    description=body.description,
                    metadata=body.metadata,
                ),
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return _scene_info_to_response(info)

    @router.get("/scenes/{scene_id}", response_model=SceneResponse)
    def get_scene(scene_id: str) -> SceneResponse:
        """按 scene_id 获取场景。"""
        try:
            info = gateway.get_scene(scene_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return _scene_info_to_response(info)

    @router.patch("/scenes/{scene_id}", response_model=SceneResponse)
    def update_scene(scene_id: str, body: UpdateSceneBody) -> SceneResponse:
        """更新场景（仅 name / description / status / metadata）。绑定字段不允许修改。"""
        try:
            info = gateway.update_scene(
                scene_id,
                UpdateSceneRequest(
                    name=body.name,
                    description=body.description,
                    status=body.status,
                    metadata=body.metadata,
                ),
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return _scene_info_to_response(info)

    @router.post("/scenes/{scene_id}/touch", response_model=SceneResponse)
    def touch_scene(scene_id: str) -> SceneResponse:
        """显式刷新场景 last_active_at。"""
        try:
            info = gateway.touch_scene_active(scene_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return _scene_info_to_response(info)

    @router.delete("/scenes/{scene_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_scene(scene_id: str) -> None:
        """删除场景。"""
        deleted = gateway.delete_scene(scene_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"场景不存在: {scene_id}",
            )

    return router

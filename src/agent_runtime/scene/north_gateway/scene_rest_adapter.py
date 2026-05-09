# -*- coding: utf-8 -*-
"""Scene REST 接入适配。

挂载 ``/api/scene/projects`` 与 ``/api/scene/scenes`` 两组路由。
依赖 :class:`SceneGateway` Protocol，不直接依赖具体网关实现。

**workspace_path 透传约定**：项目身份不再以 ``project_id`` 单独定位，所有需要落到磁盘的接口
都需要传入 ``workspace_path`` 用于路由到 ``<workspace>/.spread-ai/`` 下的 manifest 与 scenes.db。
- GET / DELETE 类接口：``workspace_path`` 通过 query 参数透传。
- POST / PATCH 类接口：``workspace_path`` 通过 body 字段透传。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.scene.north_gateway.scene_contracts import (
    CreateSceneRequest,
    OpenOrCreateProjectRequest,
    ProjectInfo,
    RecentWorkspaceInfo,
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
    """项目响应模型（不含 workspace_path — 调用方在请求里已经知道它）。"""

    model_config = ConfigDict(populate_by_name=True)

    project_id: str
    name: str
    created_at: datetime
    last_opened_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecentWorkspaceResponse(BaseModel):
    """最近工作区索引响应模型。"""

    model_config = ConfigDict(populate_by_name=True)

    workspace_path: str
    project_id: str
    project_name: str
    last_opened_at: datetime


class SceneResponse(BaseModel):
    """场景响应模型。

    ``bound_agent_definition_id``：scene 创建时选定、永久绑定不可变的主 Agent 定义。
    ``bound_root_conversation_id``：scene 首次创建 root instance 后由后端 attach 编排层回填，
    跨 instance 重建复用以延续 LLM 历史。
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


class UpdateProjectBody(BaseModel):
    """更新项目请求体。"""

    workspace_path: str = Field(..., description="项目目录绝对路径，用于路由 manifest。")
    name: str | None = None
    metadata: dict[str, Any] | None = None


class CreateSceneBody(BaseModel):
    """创建场景请求体。

    ``bound_agent_definition_id`` 必填且创建后不可修改；LLM 策略不在 scene 维度存储。
    首个 instance 的 LLM 策略由 ``POST /api/agent-runtime/scenes/{scene_id}/attach`` 决定。
    """

    workspace_path: str = Field(..., description="项目目录绝对路径，用于路由 scenes.db。")
    project_id: str = Field(..., description="所属项目 ID（必须与 manifest 一致）。")
    name: str = Field(..., description="场景可读名。")
    bound_agent_definition_id: str = Field(
        ...,
        description="永久绑定的主 Agent 定义 ID，创建后不可修改。",
    )
    description: str = Field("", description="场景长描述。")
    metadata: dict[str, Any] | None = None


class UpdateSceneBody(BaseModel):
    """更新场景请求体。

    ``bound_agent_definition_id / bound_root_conversation_id`` 不允许修改，本请求体不提供这些字段。
    """

    workspace_path: str = Field(..., description="项目目录绝对路径，用于路由 scenes.db。")
    name: str | None = None
    description: str | None = None
    status: str | None = Field(None, description="active / archived")
    metadata: dict[str, Any] | None = None


def _project_info_to_response(info: ProjectInfo) -> ProjectResponse:
    """ProjectInfo DTO -> ProjectResponse Pydantic。"""
    return ProjectResponse(
        project_id=info.project_id,
        name=info.name,
        created_at=info.created_at,
        last_opened_at=info.last_opened_at,
        metadata=dict(info.metadata),
    )


def _recent_workspace_info_to_response(info: RecentWorkspaceInfo) -> RecentWorkspaceResponse:
    """RecentWorkspaceInfo DTO -> RecentWorkspaceResponse Pydantic。"""
    return RecentWorkspaceResponse(
        workspace_path=info.workspace_path,
        project_id=info.project_id,
        project_name=info.project_name,
        last_opened_at=info.last_opened_at,
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

    路由前缀：``/api/scene``。

    Project：
    - ``POST   /api/scene/projects/open-or-create`` — 打开或创建项目（幂等）。
    - ``GET    /api/scene/projects/recent``         — 列出最近打开的 workspace 索引。
    - ``GET    /api/scene/projects/by-workspace?workspace_path=...`` — 读取项目 manifest。
    - ``PATCH  /api/scene/projects``                — 更新项目（body 带 workspace_path）。
    - ``POST   /api/scene/projects/touch``          — 刷新 last_opened_at（body 带 workspace_path）。
    - ``DELETE /api/scene/projects/recent?workspace_path=...`` — 从 recent 索引中移除（不删除数据）。
    - ``GET    /api/scene/projects/scenes?workspace_path=...&project_id=...`` — 列项目下场景。

    Scene：
    - ``POST   /api/scene/scenes``                  — 创建场景（body 带 workspace_path）。
    - ``GET    /api/scene/scenes/{scene_id}?workspace_path=...``
    - ``PATCH  /api/scene/scenes/{scene_id}``       — 更新场景（body 带 workspace_path）。
    - ``POST   /api/scene/scenes/{scene_id}/touch?workspace_path=...``
    - ``DELETE /api/scene/scenes/{scene_id}?workspace_path=...``
    """
    router = APIRouter(prefix="/api/scene", tags=["scene"])

    # ── Project ────────────────────────────────────────────────────

    @router.post("/projects/open-or-create", response_model=ProjectResponse)
    def open_or_create_project(body: OpenOrCreateProjectBody) -> ProjectResponse:
        """打开或创建项目（幂等）。同步初始化 scenes.db 与刷新 recent 索引。"""
        try:
            info = gateway.open_or_create_project(
                OpenOrCreateProjectRequest(workspace_path=body.workspace_path, name=body.name),
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return _project_info_to_response(info)

    @router.get("/projects/recent", response_model=list[RecentWorkspaceResponse])
    def list_recent_workspaces() -> list[RecentWorkspaceResponse]:
        """列出最近打开的 workspace 索引（按最近使用倒序）。"""
        return [
            _recent_workspace_info_to_response(e)
            for e in gateway.list_recent_workspaces()
        ]

    @router.get("/projects/by-workspace", response_model=ProjectResponse)
    def get_project_by_workspace(
        workspace_path: str = Query(..., description="项目目录绝对路径。"),
    ) -> ProjectResponse:
        """按 workspace_path 读取项目 manifest。"""
        try:
            info = gateway.get_project_by_workspace_path(workspace_path)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return _project_info_to_response(info)

    @router.patch("/projects", response_model=ProjectResponse)
    def update_project(body: UpdateProjectBody) -> ProjectResponse:
        """更新项目。``workspace_path`` 在 body 中。"""
        try:
            info = gateway.update_project(
                body.workspace_path,
                UpdateProjectRequest(name=body.name, metadata=body.metadata),
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return _project_info_to_response(info)

    @router.post("/projects/touch", response_model=ProjectResponse)
    def touch_project(
        workspace_path: str = Query(..., description="项目目录绝对路径。"),
    ) -> ProjectResponse:
        """显式刷新项目 ``last_opened_at`` 并同步 recent 索引。"""
        try:
            info = gateway.touch_project_opened(workspace_path)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return _project_info_to_response(info)

    @router.delete("/projects/recent", status_code=status.HTTP_204_NO_CONTENT)
    def forget_workspace(
        workspace_path: str = Query(..., description="项目目录绝对路径。"),
    ) -> None:
        """从 recent 索引中移除该 workspace（不删除 manifest 与 scenes.db）。"""
        gateway.forget_workspace(workspace_path)

    @router.get("/projects/scenes", response_model=list[SceneResponse])
    def list_scenes_by_project(
        workspace_path: str = Query(..., description="项目目录绝对路径。"),
        project_id: str = Query(..., description="项目 ID（必须与 manifest 一致）。"),
    ) -> list[SceneResponse]:
        """列出指定 workspace 下属于该项目的全部场景。"""
        try:
            infos = gateway.list_scenes_by_project(workspace_path, project_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return [_scene_info_to_response(s) for s in infos]

    # ── Scene ──────────────────────────────────────────────────────

    @router.post("/scenes", response_model=SceneResponse, status_code=status.HTTP_201_CREATED)
    def create_scene(body: CreateSceneBody) -> SceneResponse:
        """创建场景。

        仅创建 scene 记录并绑定主 Agent 定义；instance 创建与连接交由
        ``POST /api/agent-runtime/scenes/{scene_id}/attach`` 负责。
        """
        try:
            info = gateway.create_scene(
                CreateSceneRequest(
                    workspace_path=body.workspace_path,
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
    def get_scene(
        scene_id: str,
        workspace_path: str = Query(..., description="项目目录绝对路径。"),
    ) -> SceneResponse:
        """按 scene_id 获取场景。"""
        try:
            info = gateway.get_scene(workspace_path, scene_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return _scene_info_to_response(info)

    @router.patch("/scenes/{scene_id}", response_model=SceneResponse)
    def update_scene(scene_id: str, body: UpdateSceneBody) -> SceneResponse:
        """更新场景（仅 name / description / status / metadata）。绑定字段不允许修改。"""
        try:
            info = gateway.update_scene(
                body.workspace_path,
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
    def touch_scene(
        scene_id: str,
        workspace_path: str = Query(..., description="项目目录绝对路径。"),
    ) -> SceneResponse:
        """显式刷新场景 ``last_active_at``。"""
        try:
            info = gateway.touch_scene_active(workspace_path, scene_id)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return _scene_info_to_response(info)

    @router.delete("/scenes/{scene_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_scene(
        scene_id: str,
        workspace_path: str = Query(..., description="项目目录绝对路径。"),
    ) -> None:
        """删除场景。"""
        try:
            deleted = gateway.delete_scene(workspace_path, scene_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"场景不存在: {scene_id}",
            )

    return router

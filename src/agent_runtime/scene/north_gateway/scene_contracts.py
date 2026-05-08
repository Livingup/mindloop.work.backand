# -*- coding: utf-8 -*-
"""Scene 北向网关契约 — Protocol + DTO + 请求模型。

定义 scene 模块对外暴露的接口形状和数据模型。
外部调用方（REST adapter、Agent Runtime 等）只依赖此契约。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


# ══════════════════════════════════════════
# DTO — 北向数据传输对象
# ══════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    """项目信息 DTO。"""

    project_id: str
    name: str
    workspace_path: str
    created_at: datetime
    last_opened_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SceneInfo:
    """场景信息 DTO。"""

    scene_id: str
    project_id: str
    name: str
    description: str
    status: str
    created_at: datetime
    last_active_at: datetime
    default_agent_definition_id: str | None = None
    default_llm_policy_group_id: str | None = None
    default_llm_provider_id: str | None = None
    default_llm_model_binding_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ══════════════════════════════════════════
# 请求模型 — 北向网关入参
# ══════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class OpenOrCreateProjectRequest:
    """打开或创建项目请求。

    workspace_path 已存在则复用并刷新 last_opened_at；否则新建。
    适合前端"打开项目"按钮的幂等语义。
    """

    workspace_path: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class CreateProjectRequest:
    """显式新建项目请求；workspace_path 已存在时抛错。"""

    workspace_path: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateProjectRequest:
    """更新项目可变字段请求。

    name 与 metadata 任一字段非 None 时触发更新。
    """

    name: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CreateSceneRequest:
    """创建场景请求。"""

    project_id: str
    name: str
    description: str = ""
    default_agent_definition_id: str | None = None
    default_llm_policy_group_id: str | None = None
    default_llm_provider_id: str | None = None
    default_llm_model_binding_id: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class UpdateSceneRequest:
    """更新场景请求；任何字段为 None 表示不变。"""

    name: str | None = None
    description: str | None = None
    status: str | None = None
    default_agent_definition_id: str | None = None
    default_llm_policy_group_id: str | None = None
    default_llm_provider_id: str | None = None
    default_llm_model_binding_id: str | None = None
    metadata: dict[str, Any] | None = None


# ══════════════════════════════════════════
# Gateway Protocol — 北向网关契约
# ══════════════════════════════════════════


class SceneGateway(Protocol):
    """Scene 模块北向网关契约。

    所有外部调用方通过此 Protocol 使用 Scene 模块能力。
    """

    # ── Project ────────────────────────────────────────────────────

    def open_or_create_project(self, request: OpenOrCreateProjectRequest) -> ProjectInfo:
        """打开或创建项目（幂等）。"""
        ...

    def create_project(self, request: CreateProjectRequest) -> ProjectInfo:
        """显式新建项目；workspace_path 已存在抛 ValueError。"""
        ...

    def get_project(self, project_id: str) -> ProjectInfo:
        """按 project_id 获取项目；不存在抛 KeyError。"""
        ...

    def list_projects(self) -> list[ProjectInfo]:
        """列出全部项目，按 last_opened_at 倒序。"""
        ...

    def update_project(self, project_id: str, request: UpdateProjectRequest) -> ProjectInfo:
        """更新项目可变字段。"""
        ...

    def touch_project_opened(self, project_id: str) -> ProjectInfo:
        """显式刷新项目 last_opened_at。"""
        ...

    def delete_project(self, project_id: str) -> bool:
        """删除项目（关联场景级联删除）；不存在返回 False。"""
        ...

    # ── Scene ──────────────────────────────────────────────────────

    def create_scene(self, request: CreateSceneRequest) -> SceneInfo:
        """创建场景。"""
        ...

    def get_scene(self, scene_id: str) -> SceneInfo:
        """按 scene_id 获取场景；不存在抛 KeyError。"""
        ...

    def list_scenes_by_project(self, project_id: str) -> list[SceneInfo]:
        """列出指定项目下的全部场景。"""
        ...

    def update_scene(self, scene_id: str, request: UpdateSceneRequest) -> SceneInfo:
        """更新场景。"""
        ...

    def touch_scene_active(self, scene_id: str) -> SceneInfo:
        """显式刷新场景 last_active_at。"""
        ...

    def delete_scene(self, scene_id: str) -> bool:
        """删除场景；不存在返回 False。"""
        ...

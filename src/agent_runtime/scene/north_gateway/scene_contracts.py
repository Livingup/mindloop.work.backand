# -*- coding: utf-8 -*-
"""Scene 北向网关契约 — Protocol + DTO + 请求模型。

定义 scene 模块对外暴露的接口形状和数据模型。
外部调用方（REST adapter、Agent Runtime 等）只依赖此契约。

**重要约定**：项目身份持久化在 ``<workspace>/.spread-ai/project.json``，
``workspace_path`` 不属于 Project 聚合状态；所有需要定位项目/场景数据的接口
都以 ``workspace_path`` 作为路由参数；``project_id`` 不能脱离 ``workspace_path`` 单独使用。
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
    """项目信息 DTO（不含 workspace_path — 调用方在请求里已经知道它）。"""

    project_id: str
    name: str
    created_at: datetime
    last_opened_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecentWorkspaceInfo:
    """最近工作区索引条目 DTO。

    与 ProjectInfo 不同，本 DTO 自带 ``workspace_path``，前端用于"最近项目"列表展示
    与点击恢复打开。索引数据来自 ``~/.spread-ai/recent-workspaces.json``。
    """

    workspace_path: str
    project_id: str
    project_name: str
    last_opened_at: datetime


@dataclass(frozen=True, slots=True)
class SceneInfo:
    """场景信息 DTO。

    ``bound_agent_definition_id`` 是 scene 永久绑定的主 Agent 定义 ID，创建后不可变。
    ``bound_root_conversation_id`` 是该 scene 持有的对话上下文，首次创建 root instance 后由编排层回填。
    LLM 策略不与 scene 绑定（属于 instance 维度）。
    """

    scene_id: str
    project_id: str
    name: str
    description: str
    status: str
    bound_agent_definition_id: str
    created_at: datetime
    last_active_at: datetime
    bound_root_conversation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ══════════════════════════════════════════
# 请求模型 — 北向网关入参
# ══════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class OpenOrCreateProjectRequest:
    """打开或创建项目请求。

    ``<workspace>/.spread-ai/project.json`` 已存在则复用并刷新 ``last_opened_at``；
    否则生成新 project_id 并写入 manifest。
    """

    workspace_path: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class UpdateProjectRequest:
    """更新项目可变字段请求。

    ``name`` 与 ``metadata`` 任一字段非 None 时触发更新。
    """

    name: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CreateSceneRequest:
    """创建场景请求。

    ``workspace_path`` 用于路由到 ``<workspace>/.spread-ai/scenes.db``；
    ``project_id`` 必须与 manifest 一致，否则抛 KeyError。
    ``bound_agent_definition_id`` 创建后永久不可变；LLM 策略由 attach 接口决定。
    """

    workspace_path: str
    project_id: str
    name: str
    bound_agent_definition_id: str
    description: str = ""
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class UpdateSceneRequest:
    """更新场景请求；任何字段为 None 表示不变。

    ``bound_agent_definition_id`` 不可变，本请求模型不提供该字段。
    ``bound_root_conversation_id`` 也不能通过本请求修改，由 attach 编排层回填。
    """

    name: str | None = None
    description: str | None = None
    status: str | None = None
    metadata: dict[str, Any] | None = None


# ══════════════════════════════════════════
# Gateway Protocol — 北向网关契约
# ══════════════════════════════════════════


class SceneGateway(Protocol):
    """Scene 模块北向网关契约。

    所有外部调用方通过此 Protocol 使用 Scene 模块能力。
    所有 Project / Scene 相关方法都以 ``workspace_path`` 作为定位参数。
    """

    # ── Project ────────────────────────────────────────────────────

    def open_or_create_project(self, request: OpenOrCreateProjectRequest) -> ProjectInfo:
        """打开或创建项目（幂等）。

        无论是首次打开还是后续打开，都会同步：写 manifest、初始化 scenes.db、刷新 recent 索引。
        """
        ...

    def get_project_by_workspace_path(self, workspace_path: str) -> ProjectInfo:
        """读取指定 workspace 下的项目 manifest；不存在抛 KeyError。"""
        ...

    def list_recent_workspaces(self) -> list[RecentWorkspaceInfo]:
        """列出最近打开的 workspace 索引（按最近使用倒序）。"""
        ...

    def update_project(self, workspace_path: str, request: UpdateProjectRequest) -> ProjectInfo:
        """更新项目可变字段（name / metadata）。"""
        ...

    def touch_project_opened(self, workspace_path: str) -> ProjectInfo:
        """显式刷新项目 ``last_opened_at`` 并同步 recent 索引。"""
        ...

    def forget_workspace(self, workspace_path: str) -> bool:
        """从 recent 索引中移除该 workspace（不删除 manifest 与 scenes.db）；幂等。"""
        ...

    # ── Scene ──────────────────────────────────────────────────────

    def create_scene(self, request: CreateSceneRequest) -> SceneInfo:
        """创建场景。"""
        ...

    def get_scene(self, workspace_path: str, scene_id: str) -> SceneInfo:
        """按 scene_id 获取场景；不存在抛 KeyError。"""
        ...

    def list_scenes_by_project(
        self,
        workspace_path: str,
        project_id: str,
    ) -> list[SceneInfo]:
        """列出指定 workspace 下属于该项目的全部场景。"""
        ...

    def update_scene(
        self,
        workspace_path: str,
        scene_id: str,
        request: UpdateSceneRequest,
    ) -> SceneInfo:
        """更新场景。"""
        ...

    def set_bound_root_conversation_id(
        self,
        workspace_path: str,
        scene_id: str,
        conversation_id: str,
    ) -> SceneInfo:
        """在 scene 下首次创建 root agent instance 后回填 conversation_id。

        由 attach 编排层调用；同一 scene 不能重复绑定到不同 conversation_id。

        Raises:
            KeyError: scene 不存在。
            ValueError: 与已绑定的 conversation_id 冲突。
        """
        ...

    def touch_scene_active(self, workspace_path: str, scene_id: str) -> SceneInfo:
        """显式刷新场景 ``last_active_at``。"""
        ...

    def delete_scene(self, workspace_path: str, scene_id: str) -> bool:
        """删除场景；不存在返回 False。"""
        ...

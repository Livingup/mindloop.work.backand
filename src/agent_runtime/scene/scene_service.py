# -*- coding: utf-8 -*-
"""Scene 模块领域服务。"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from agent_runtime.scene.project import Project
from agent_runtime.scene.scene import (
    SCENE_STATUS_ACTIVE,
    SCENE_STATUSES,
    Scene,
)
from agent_runtime.scene.scene_repository import SceneRepository

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    """生成业务前缀 + UUID 的稳定 ID。"""
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def _derive_project_name_from_path(workspace_path: str) -> str:
    """从 workspace 绝对路径提取目录 basename 作为默认项目名。"""
    # 兼容 Windows 与 POSIX 风格分隔符。
    normalized = workspace_path.replace("\\", "/").rstrip("/")
    if not normalized:
        return "project"
    parts = normalized.split("/")
    name = parts[-1] if parts else ""
    return name or "project"


class SceneService:
    """Scene 模块领域服务（无状态）。

    职责：
    1. 协调仓储完成 Project / Scene 的 CRUD 业务用例。
    2. 在创建/打开项目时生成业务前缀的稳定 ID（proj-... / scene-...）。
    3. 在打开项目、活动场景时刷新时间戳。
    4. 跨聚合根的业务规则编排（如 archived 场景禁止再激活之外的更新）。

    对外只通过仓储接口与领域实体交互，不依赖 ConfigRegistry 或具体 SQLite 实现。
    """

    def __init__(self, repository: SceneRepository) -> None:
        self._repo = repository

    # ── Project 用例 ────────────────────────────────────────────────

    def open_or_create_project(
        self,
        workspace_path: str,
        *,
        name: str | None = None,
    ) -> Project:
        """根据工作区路径打开或创建项目。

        如果同一 workspace_path 已有项目则直接返回（并刷新 last_opened_at）；
        否则按规则生成新项目。

        Args:
            workspace_path: 项目目录绝对路径。
            name: 可选项目名；为空时取 path 的 basename。

        Returns:
            打开或新建的项目。
        """
        existing = self._repo.get_project_by_workspace_path(workspace_path)
        if existing is not None:
            updated = existing.touch_opened()
            self._repo.save_project(updated)
            return updated
        project = Project(
            project_id=_new_id("proj"),
            name=name or _derive_project_name_from_path(workspace_path),
            workspace_path=workspace_path,
        )
        return self._repo.create_project(project)

    def create_project(
        self,
        workspace_path: str,
        *,
        name: str | None = None,
    ) -> Project:
        """显式创建一个新项目；workspace_path 已存在时抛 ValueError。"""
        existing = self._repo.get_project_by_workspace_path(workspace_path)
        if existing is not None:
            raise ValueError(
                f"创建项目失败：workspace_path 已被项目 {existing.project_id} 占用: {workspace_path}"
            )
        project = Project(
            project_id=_new_id("proj"),
            name=name or _derive_project_name_from_path(workspace_path),
            workspace_path=workspace_path,
        )
        return self._repo.create_project(project)

    def get_project(self, project_id: str) -> Project:
        """按 project_id 获取项目；不存在抛 KeyError。"""
        project = self._repo.get_project(project_id)
        if project is None:
            raise KeyError(f"项目不存在: {project_id}")
        return project

    def list_projects(self) -> list[Project]:
        """列出全部项目，按 last_opened_at 倒序。"""
        return self._repo.list_projects()

    def touch_project_opened(self, project_id: str) -> Project:
        """更新指定项目的 last_opened_at；项目不存在抛 KeyError。"""
        project = self.get_project(project_id)
        updated = project.touch_opened()
        return self._repo.save_project(updated)

    def rename_project(self, project_id: str, name: str) -> Project:
        """修改项目可读名。"""
        project = self.get_project(project_id)
        updated = project.rename(name)
        return self._repo.save_project(updated)

    def update_project_metadata(
        self,
        project_id: str,
        metadata: dict[str, Any],
    ) -> Project:
        """全量替换项目 metadata。"""
        project = self.get_project(project_id)
        from dataclasses import replace as dc_replace

        updated = dc_replace(project, metadata=dict(metadata))
        return self._repo.save_project(updated)

    def delete_project(self, project_id: str) -> bool:
        """删除项目；关联 scenes 通过仓储级联删除。"""
        return self._repo.delete_project(project_id)

    # ── Scene 用例 ──────────────────────────────────────────────────

    def create_scene(
        self,
        *,
        project_id: str,
        name: str,
        bound_agent_definition_id: str,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Scene:
        """在指定项目下创建场景。

        bound_agent_definition_id 永久绑定主 Agent 定义，创建后不可修改。
        LLM 策略不与 scene 绑定，由编排层（attach 用例）在创建首个 instance 时决定。

        Raises:
            KeyError: project_id 不存在。
            ValueError: bound_agent_definition_id 为空。
        """
        # 显式校验 project 存在并刷新打开时间，便于"创建场景时项目自动浮起"。
        self.get_project(project_id)
        scene = Scene(
            scene_id=_new_id("scene"),
            project_id=project_id,
            name=name,
            description=description,
            status=SCENE_STATUS_ACTIVE,
            bound_agent_definition_id=bound_agent_definition_id,
            metadata=dict(metadata) if metadata else {},
        )
        created = self._repo.create_scene(scene)
        # 创建场景视为项目活跃事件，刷新项目 last_opened_at。
        self.touch_project_opened(project_id)
        return created

    def get_scene(self, scene_id: str) -> Scene:
        """按 scene_id 获取场景；不存在抛 KeyError。"""
        scene = self._repo.get_scene(scene_id)
        if scene is None:
            raise KeyError(f"场景不存在: {scene_id}")
        return scene

    def list_scenes_by_project(self, project_id: str) -> list[Scene]:
        """列出指定项目下的全部场景。"""
        # 校验 project 存在性，便于上层区分"项目不存在"和"项目下没有场景"。
        self.get_project(project_id)
        return self._repo.list_scenes_by_project(project_id)

    def touch_scene_active(self, scene_id: str) -> Scene:
        """更新场景 last_active_at 时间戳。"""
        scene = self.get_scene(scene_id)
        updated = scene.touch_active()
        return self._repo.save_scene(updated)

    def update_scene(
        self,
        scene_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Scene:
        """更新场景的可变字段。

        bound_agent_definition_id 永久绑定不可变；如需更换 Agent 请新建 scene。
        bound_root_conversation_id 通过专用方法 ``set_bound_root_conversation_id`` 回填，不在此处更新。
        status 可以传 'active' / 'archived' 触发归档/激活；其他字段值传入即生效。
        所有参数为 None 表示不变。
        """
        scene = self.get_scene(scene_id)
        if status is not None:
            if status not in SCENE_STATUSES:
                raise ValueError(f"未知 scene 状态: {status}")
            if status != scene.status:
                scene = scene.archive() if status == "archived" else scene.activate()
        scene = scene.update_metadata(
            name=name,
            description=description,
            metadata=metadata,
        )
        return self._repo.save_scene(scene)

    def set_bound_root_conversation_id(self, scene_id: str, conversation_id: str) -> Scene:
        """回填场景持有的 conversation_id。

        由 attach 编排层在 scene 下首次创建 root agent instance 后调用；同一 scene 不能重复绑定到不同的
        conversation_id（领域层会拒绝），保证"scene 与对话上下文一对一"业务规则。

        Raises:
            KeyError: scene 不存在。
            ValueError: conversation_id 与既有 bound_root_conversation_id 冲突。
        """
        scene = self.get_scene(scene_id)
        updated = scene.with_bound_root_conversation_id(conversation_id)
        if updated is scene:
            return scene
        return self._repo.save_scene(updated)

    def delete_scene(self, scene_id: str) -> bool:
        """删除场景。"""
        return self._repo.delete_scene(scene_id)

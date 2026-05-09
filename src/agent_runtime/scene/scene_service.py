# -*- coding: utf-8 -*-
"""Scene 模块领域服务。

本服务编排三个仓储依赖：

- :class:`agent_runtime.scene.project_manifest_repository.ProjectManifestRepository`
  读写 ``<workspace>/.spread-ai/project.json`` — 项目永久身份凭证。
- :class:`agent_runtime.scene.recent_workspaces.RecentWorkspacesIndex`
  读写 ``~/.spread-ai/recent-workspaces.json`` — 最近打开的 workspace 全局索引。
- :class:`agent_runtime.scene.scene_repository.SceneRepository`
  读写 ``<workspace>/.spread-ai/scenes.db`` — 场景数据。

设计原则：
- 所有需要定位项目的方法都以 ``workspace_path`` 为入参，不再支持"按 project_id 查项目"。
- 项目数据随 workspace 目录走；recent-workspaces 是纯展示辅助索引，丢失不影响业务。
- 创建/更新场景时校验 ``manifest.project_id == scene.project_id``，避免误写到错误数据库。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from agent_runtime.scene.project import Project
from agent_runtime.scene.project_manifest_repository import ProjectManifestRepository
from agent_runtime.scene.recent_workspaces import (
    RecentWorkspaceEntry,
    RecentWorkspacesIndex,
)
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
    normalized = workspace_path.replace("\\", "/").rstrip("/")
    if not normalized:
        return "project"
    parts = normalized.split("/")
    name = parts[-1] if parts else ""
    return name or "project"


def _is_absolute_path(path: str) -> bool:
    """判断给定字符串是否为绝对路径（兼容 Windows 与 POSIX）。"""
    if not path:
        return False
    if path.startswith("/"):
        return True
    if path.startswith("\\\\") or path.startswith("//"):
        return True
    if len(path) >= 3 and path[1] == ":" and path[2] in ("\\", "/"):
        return True
    return False


def _require_absolute_workspace(workspace_path: str) -> None:
    """校验 workspace_path 必须是非空的绝对路径。"""
    if not isinstance(workspace_path, str) or not workspace_path.strip():
        raise ValueError("workspace_path 不能为空。")
    if not _is_absolute_path(workspace_path):
        raise ValueError(f"workspace_path 必须是绝对路径: {workspace_path}")


class SceneService:
    """Scene 模块领域服务（无状态）。

    职责：
    1. 协调三层仓储完成 Project（manifest）、最近工作区索引、Scene 数据库的 CRUD。
    2. 创建项目/场景时生成业务前缀稳定 ID（``proj-...`` / ``scene-...``）。
    3. 打开项目、活跃场景时刷新时间戳，并把变更同步到 recent-workspaces 索引。
    4. 跨聚合一致性校验：场景的 project_id 必须与 manifest 一致。

    对外只通过领域接口与实体交互，不依赖具体存储实现。
    """

    def __init__(
        self,
        manifest_repo: ProjectManifestRepository,
        recent_index: RecentWorkspacesIndex,
        scene_repo: SceneRepository,
    ) -> None:
        self._manifest_repo = manifest_repo
        self._recent_index = recent_index
        self._scene_repo = scene_repo

    # ── Project 用例（按 workspace_path 路由）────────────────────────

    def open_or_create_project(
        self,
        workspace_path: str,
        *,
        name: str | None = None,
    ) -> Project:
        """打开或创建项目（幂等）。

        - 如果 ``<workspace>/.spread-ai/project.json`` 已存在 → 读出 + touch_opened + 写回；
        - 否则生成新 project_id 并写入 manifest。
        - 无论哪条路径都会确保 scenes.db 已初始化、recent-workspaces 索引已更新。
        """
        _require_absolute_workspace(workspace_path)
        existing = self._manifest_repo.read(workspace_path)
        if existing is not None:
            project = existing.touch_opened()
        else:
            project = Project(
                project_id=_new_id("proj"),
                name=name or _derive_project_name_from_path(workspace_path),
            )
            logger.info(
                "新建项目 manifest project_id=%s workspace=%s",
                project.project_id, workspace_path,
            )
        self._manifest_repo.write(workspace_path, project)
        self._scene_repo.ensure_initialized(workspace_path)
        self._sync_recent_index(workspace_path, project)
        return project

    def get_project_by_workspace_path(self, workspace_path: str) -> Project:
        """读取指定 workspace 的项目 manifest；不存在抛 KeyError。"""
        _require_absolute_workspace(workspace_path)
        project = self._manifest_repo.read(workspace_path)
        if project is None:
            raise KeyError(f"项目 manifest 不存在: {workspace_path}")
        return project

    def list_recent_workspaces(self) -> list[RecentWorkspaceEntry]:
        """列出最近打开过的 workspace（按最近使用倒序）。"""
        return self._recent_index.list()

    def touch_project_opened(self, workspace_path: str) -> Project:
        """刷新项目 last_opened_at；同步到 manifest 与 recent 索引。"""
        project = self.get_project_by_workspace_path(workspace_path).touch_opened()
        self._manifest_repo.write(workspace_path, project)
        self._sync_recent_index(workspace_path, project)
        return project

    def rename_project(self, workspace_path: str, new_name: str) -> Project:
        """重命名项目。"""
        project = self.get_project_by_workspace_path(workspace_path).rename(new_name)
        self._manifest_repo.write(workspace_path, project)
        self._sync_recent_index(workspace_path, project)
        return project

    def update_project_metadata(
        self,
        workspace_path: str,
        metadata: dict[str, Any],
    ) -> Project:
        """全量替换项目 metadata。"""
        project = self.get_project_by_workspace_path(workspace_path).with_metadata(metadata)
        self._manifest_repo.write(workspace_path, project)
        return project

    def forget_workspace(self, workspace_path: str) -> bool:
        """从 recent 索引中移除该 workspace（不删除 manifest 与 scenes.db）。

        前端"从最近列表中移除"按钮可调本方法；项目本身的数据仍保留在目录下。
        """
        return self._recent_index.remove(workspace_path)

    # ── Scene 用例（按 workspace_path 路由）─────────────────────────

    def create_scene(
        self,
        workspace_path: str,
        *,
        project_id: str,
        name: str,
        bound_agent_definition_id: str,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Scene:
        """在指定 workspace 下创建场景。

        ``bound_agent_definition_id`` 永久绑定主 Agent 定义，创建后不可修改。
        LLM 策略不与 scene 绑定，由 attach 编排层在创建首个 instance 时决定。

        Raises:
            KeyError: 项目 manifest 不存在或与传入 ``project_id`` 不一致。
            ValueError: 必填字段为空。
        """
        self._require_project_match(workspace_path, project_id)
        scene = Scene(
            scene_id=_new_id("scene"),
            project_id=project_id,
            name=name,
            description=description,
            status=SCENE_STATUS_ACTIVE,
            bound_agent_definition_id=bound_agent_definition_id,
            metadata=dict(metadata) if metadata else {},
        )
        created = self._scene_repo.create_scene(workspace_path, scene)
        # 创建场景视为项目活跃事件，刷新项目 last_opened_at。
        self.touch_project_opened(workspace_path)
        return created

    def get_scene(self, workspace_path: str, scene_id: str) -> Scene:
        """按 scene_id 获取场景；不存在抛 KeyError。"""
        _require_absolute_workspace(workspace_path)
        scene = self._scene_repo.get_scene(workspace_path, scene_id)
        if scene is None:
            raise KeyError(f"场景不存在: {scene_id}")
        return scene

    def list_scenes_by_project(
        self,
        workspace_path: str,
        project_id: str,
    ) -> list[Scene]:
        """列出指定 workspace 下属于该项目的全部场景。

        会校验 manifest.project_id 与传入参数一致；不一致抛 KeyError 防止误读。
        """
        self._require_project_match(workspace_path, project_id)
        return self._scene_repo.list_scenes_by_project(workspace_path, project_id)

    def touch_scene_active(self, workspace_path: str, scene_id: str) -> Scene:
        """更新场景 last_active_at 时间戳。"""
        scene = self.get_scene(workspace_path, scene_id).touch_active()
        return self._scene_repo.save_scene(workspace_path, scene)

    def update_scene(
        self,
        workspace_path: str,
        scene_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Scene:
        """更新场景的可变字段。

        ``bound_agent_definition_id`` 永久绑定不可变；如需更换 Agent 请新建 scene。
        ``bound_root_conversation_id`` 通过 ``set_bound_root_conversation_id`` 回填，不在此处更新。
        ``status`` 可传 ``'active' / 'archived'`` 触发激活/归档；其他字段为 None 表示不变。
        """
        scene = self.get_scene(workspace_path, scene_id)
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
        return self._scene_repo.save_scene(workspace_path, scene)

    def set_bound_root_conversation_id(
        self,
        workspace_path: str,
        scene_id: str,
        conversation_id: str,
    ) -> Scene:
        """回填场景持有的 conversation_id。

        由 attach 编排层在 scene 下首次创建 root agent instance 后调用；同一 scene 不能重复
        绑定到不同的 conversation_id（领域层会拒绝），保证"scene 与对话上下文一对一"。

        Raises:
            KeyError: scene 不存在。
            ValueError: conversation_id 与既有 bound_root_conversation_id 冲突。
        """
        scene = self.get_scene(workspace_path, scene_id)
        updated = scene.with_bound_root_conversation_id(conversation_id)
        if updated is scene:
            return scene
        return self._scene_repo.save_scene(workspace_path, updated)

    def delete_scene(self, workspace_path: str, scene_id: str) -> bool:
        """删除场景。"""
        _require_absolute_workspace(workspace_path)
        return self._scene_repo.delete_scene(workspace_path, scene_id)

    # ── 内部辅助 ────────────────────────────────────────────────────

    def _require_project_match(self, workspace_path: str, project_id: str) -> Project:
        """校验 workspace_path 下的 manifest.project_id 与传入一致；不一致抛 KeyError。"""
        project = self.get_project_by_workspace_path(workspace_path)
        if project.project_id != project_id:
            raise KeyError(
                f"workspace 与项目不匹配: workspace={workspace_path} "
                f"manifest.project_id={project.project_id} expected={project_id}"
            )
        return project

    def _sync_recent_index(self, workspace_path: str, project: Project) -> None:
        """把当前 project 信息同步写入 recent-workspaces 索引。"""
        self._recent_index.put(
            RecentWorkspaceEntry(
                workspace_path=workspace_path,
                project_id=project.project_id,
                project_name=project.name,
                last_opened_at=project.last_opened_at,
            )
        )

# -*- coding: utf-8 -*-
"""Scene 模块配置。

Scene 详情数据持久化到全局统一 SQLite 数据库（所有 workspace 共享），
项目身份（manifest）仍然按 workspace 本地化存储于 ``<workspace>/.spread-ai/project.json``。
本配置承载两类全局路径：
- ``scenes_db_path``  — 全局 Scene 数据库文件，所有项目下的 scene 均落盘在此；
- ``recent_workspaces_path`` — 最近工作区索引文件，前端"最近项目"列表展示用，
  丢失或损坏不影响业务（项目身份与全局 scene DB 数据完整保留）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SceneConfig:
    """Scene 服务配置。"""

    # 全局 Scene SQLite 数据库文件路径（默认 ~/.spread-ai/scene/scene.db）。
    # SqliteSceneRepository 在此初始化唯一一份 scene 数据库，所有 workspace 共享；
    # 每条 scene 记录通过 ``project_id`` 列归属到具体项目。
    scenes_db_path: str = "~/.spread-ai/scene/scene.db"

    # 全局最近工作区索引文件路径（默认 ~/.spread-ai/recent-workspaces.json）。
    # FileRecentWorkspacesIndex 写入该文件，记录所有曾被本机用户打开过的 workspace 元数据。
    recent_workspaces_path: str = "~/.spread-ai/recent-workspaces.json"

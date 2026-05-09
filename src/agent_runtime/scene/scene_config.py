# -*- coding: utf-8 -*-
"""Scene 模块配置。

Scene 数据按 workspace 本地化存储，已不再有"全局 scene 数据目录"的概念，
本配置只保留"全局最近工作区索引"文件路径——索引仅承担前端"最近项目"列表的展示用途，
丢失或损坏不影响业务（项目身份与数据保存在 ``<workspace>/.spread-ai/`` 内）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SceneConfig:
    """Scene 服务配置。"""

    # 全局最近工作区索引文件路径（默认 ~/.spread-ai/recent-workspaces.json）。
    # FileRecentWorkspacesIndex 写入该文件，记录所有曾被本机用户打开过的 workspace 元数据。
    recent_workspaces_path: str = "~/.spread-ai/recent-workspaces.json"

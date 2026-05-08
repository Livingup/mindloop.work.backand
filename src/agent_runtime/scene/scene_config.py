# -*- coding: utf-8 -*-
"""Scene 模块配置。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SceneConfig:
    """Scene 服务配置。"""

    # Scene 数据目录（SQLite 数据库 scene.db 落盘位置）。默认 ~/.spread-ai/scene。
    data_dir: str = "~/.spread-ai/scene"

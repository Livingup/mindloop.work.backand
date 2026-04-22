# -*- coding: utf-8 -*-
"""Skill 模块配置。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SkillConfig:
    """Skill 服务配置。"""

    # Skill 仓库根目录（包含各 skill 子目录）。
    skills_dir: str

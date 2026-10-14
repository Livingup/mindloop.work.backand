# -*- coding: utf-8 -*-
"""Skill 模块配置。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SkillConfig:
    """Skill 服务配置。"""

    # Skill 仓库根目录（包含各 skill 子目录）。
    skills_dir: str

    # Skill 分组映射文件路径（应用层扩展，非 Skill 标准协议字段）。
    skill_groups_file: str = "~/.spread-ai/skill-groups.yaml"

    # Skill 用户设置数据库目录。
    settings_dir: str = "~/.spread-ai/skill"

    # 是否在读取 Skill 元数据前自动检查仓库目录变化并刷新缓存。
    auto_refresh_enabled: bool = True

    # 自动刷新快照检查最小间隔，避免高频查询每次都遍历文件系统。
    snapshot_check_interval_seconds: float = 1.0

# -*- coding: utf-8 -*-
"""Skill 北向网关契约 — Protocol + DTO。

定义 skill 模块对外暴露的接口形状和数据模型。
外部调用方（Agent 定义模块、CLI adapter、REST adapter）只依赖此契约。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# ══════════════════════════════════════════
# DTO — 北向网关数据传输对象
# ══════════════════════════════════════════


@dataclass(frozen=True)
class SkillInfo:
    """Skill 信息 DTO — 完整元数据 + 目录路径 + 应用层分组。"""

    skill_id: str
    name: str
    description: str
    directory_path: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: str | None = None
    group: str = "default"
    """应用层分组标签（非 Skill 标准协议字段）。"""


# ══════════════════════════════════════════
# Gateway Protocol — 北向网关契约
# ══════════════════════════════════════════


class SkillGateway(Protocol):
    """Skill 模块北向网关契约。

    所有外部调用方通过此 Protocol 使用 Skill 模块能力。
    """

    def list_skills(self) -> list[SkillInfo]:
        """列出所有已发现的 Skill（完整元数据 + 目录路径 + 应用层分组）。"""
        ...

    def refresh(self) -> None:
        """触发重新扫描 Skill 仓库目录。"""
        ...

    def get_settings(self):
        """读取 Skill 用户设置。"""
        ...

    def save_settings(self, *, skills_dir: str):
        """保存 Skill 用户设置。"""
        ...

    def settings_db_path(self):
        """读取 Skill 用户设置数据库路径。"""
        ...

    def set_skill_group(self, skill_id: str, group: str) -> None:
        """设置指定 Skill 的应用层分组。

        Args:
            skill_id: Skill 唯一标识。
            group: 分组名称，传入 'default' 恢复默认分组。
        """
        ...

    def get_skill_content(self, skill_id: str) -> str:
        """读取指定 Skill 的 SKILL.md 正文内容（去除 frontmatter）。

        Args:
            skill_id: Skill 唯一标识。

        Returns:
            SKILL.md 的 Markdown 正文部分。

        Raises:
            KeyError: Skill 不存在。
            FileNotFoundError: SKILL.md 文件不存在。
        """
        ...

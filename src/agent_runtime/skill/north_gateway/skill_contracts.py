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
    """Skill 信息 DTO — 完整元数据 + 目录路径。"""

    skill_id: str
    name: str
    description: str
    directory_path: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: str | None = None


# ══════════════════════════════════════════
# Gateway Protocol — 北向网关契约
# ══════════════════════════════════════════


class SkillGateway(Protocol):
    """Skill 模块北向网关契约。

    所有外部调用方通过此 Protocol 使用 Skill 模块能力。
    """

    def list_skills(self) -> list[SkillInfo]:
        """列出所有已发现的 Skill（完整元数据 + 目录路径）。"""
        ...

    def refresh(self) -> None:
        """触发重新扫描 Skill 仓库目录。"""
        ...

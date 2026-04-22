# -*- coding: utf-8 -*-
"""Skill REST 接入适配。

挂在 /api/skills 路由下。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from agent_runtime.skill.north_gateway.skill_contracts import SkillGateway


# ── 响应模型 ──────────────────────────────────────────────────


class SkillInfoResponse(BaseModel):
    """Skill 信息响应。"""

    skill_id: str
    name: str
    description: str
    directory_path: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = {}
    allowed_tools: str | None = None


class RefreshResponse(BaseModel):
    """刷新响应。"""

    message: str
    count: int


# ── 路由工厂 ──────────────────────────────────────────────────


def create_skill_router(gateway: SkillGateway) -> APIRouter:
    """创建 Skill REST 路由。

    Args:
        gateway: Skill 北向网关实例。

    Returns:
        FastAPI APIRouter。
    """
    router = APIRouter()

    @router.get("", response_model=list[SkillInfoResponse])
    def list_skills() -> list[dict[str, Any]]:
        """列出所有已发现的 Skill。"""
        skills = gateway.list_skills()
        return [
            {
                "skill_id": s.skill_id,
                "name": s.name,
                "description": s.description,
                "directory_path": s.directory_path,
                "license": s.license,
                "compatibility": s.compatibility,
                "metadata": s.metadata,
                "allowed_tools": s.allowed_tools,
            }
            for s in skills
        ]

    @router.post("/refresh", response_model=RefreshResponse)
    def refresh_skills() -> dict[str, Any]:
        """触发重新扫描 Skill 仓库目录。"""
        gateway.refresh()
        skills = gateway.list_skills()
        return {"message": "Skill 仓库已重新扫描", "count": len(skills)}

    return router

# -*- coding: utf-8 -*-
"""Skill REST 接入适配。

挂在 /api/skills 路由下。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
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
    group: str = "default"


class RefreshResponse(BaseModel):
    """刷新响应。"""

    message: str
    count: int


class SetGroupBody(BaseModel):
    """设置分组请求体。"""

    group: str


class SkillSettingsResponse(BaseModel):
    skills_dir: str
    db_path: str
    updated_at: str | None = None


class SkillSettingsUpdateBody(BaseModel):
    skills_dir: str


class StatusResponse(BaseModel):
    """通用状态响应。"""

    status: str
    message: str


class SkillContentResponse(BaseModel):
    """Skill 内容响应。"""

    skill_id: str
    content: str


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
                "group": s.group,
            }
            for s in skills
        ]

    @router.get("/settings", response_model=SkillSettingsResponse)
    def get_settings() -> SkillSettingsResponse:
        settings = gateway.get_settings()
        return SkillSettingsResponse(
            skills_dir=settings.skills_dir,
            db_path=str(gateway.settings_db_path()),
            updated_at=settings.updated_at,
        )

    @router.put("/settings", response_model=SkillSettingsResponse)
    def update_settings(body: SkillSettingsUpdateBody) -> SkillSettingsResponse:
        try:
            settings = gateway.save_settings(skills_dir=body.skills_dir)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return SkillSettingsResponse(
            skills_dir=settings.skills_dir,
            db_path=str(gateway.settings_db_path()),
            updated_at=settings.updated_at,
        )

    @router.patch("/{skill_id}/group", response_model=StatusResponse)
    def set_skill_group(skill_id: str, body: SetGroupBody) -> dict[str, Any]:
        """设置指定 Skill 的应用层分组。"""
        gateway.set_skill_group(skill_id, body.group)
        return {"status": "success", "message": f"已设置 Skill '{skill_id}' 分组为 '{body.group}'"}

    @router.get("/{skill_id}/content", response_model=SkillContentResponse)
    def get_skill_content(skill_id: str) -> dict[str, Any]:
        """读取指定 Skill 的 SKILL.md 正文内容（去除 frontmatter）。"""
        try:
            content = gateway.get_skill_content(skill_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"skill_id": skill_id, "content": content}

    @router.post("/refresh", response_model=RefreshResponse)
    def refresh_skills() -> dict[str, Any]:
        """触发重新扫描 Skill 仓库目录。"""
        gateway.refresh()
        skills = gateway.list_skills()
        return {"message": "Skill 仓库已重新扫描", "count": len(skills)}

    return router

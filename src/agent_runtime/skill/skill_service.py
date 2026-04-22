# -*- coding: utf-8 -*-
"""Skill 领域服务。"""
from __future__ import annotations

import logging
from typing import Any

from agent_runtime.skill.skill_repository import SkillRepository

logger = logging.getLogger(__name__)


class SkillService:
    """Skill 领域服务（无状态）。

    协调仓储完成业务用例：
    1. 列出所有 Skill（返回完整元数据 + 目录路径）
    2. 刷新 Skill 索引（触发重新扫描）
    """

    def __init__(self, repo: SkillRepository) -> None:
        self._repo = repo

    def list_skills(self) -> list[dict[str, Any]]:
        """列出所有 Skill 的完整信息。

        Returns:
            每个 Skill 的元数据字典列表，包含目录路径。
        """
        skills = self._repo.list_all()
        return [self._to_dict(s) for s in skills]

    def refresh(self) -> None:
        """触发重新扫描 Skill 仓库目录。"""
        logger.info("触发 Skill 仓库重新扫描")
        self._repo.scan()
        logger.info("Skill 仓库扫描完成，共发现 %d 个 Skill", len(self._repo.list_all()))

    @staticmethod
    def _to_dict(skill: Any) -> dict[str, Any]:
        """将 SkillDefinition 转为字典。"""
        result: dict[str, Any] = {
            "skill_id": skill.skill_id,
            "name": skill.name,
            "description": skill.description,
            "directory_path": skill.directory_path,
        }
        if skill.license:
            result["license"] = skill.license
        if skill.compatibility:
            result["compatibility"] = skill.compatibility
        if skill.metadata:
            result["metadata"] = skill.metadata
        if skill.allowed_tools:
            result["allowed_tools"] = skill.allowed_tools
        return result

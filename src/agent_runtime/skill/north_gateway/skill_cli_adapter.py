# -*- coding: utf-8 -*-
"""Skill CLI 接入适配。

挂在 spread-ai skill 子命令组下。
输出格式为 JSON，方便 Agent / 外部 AI 解析。
"""
from __future__ import annotations

import json
from typing import Any

import click

from agent_runtime.skill.north_gateway.skill_contracts import SkillGateway


def _serialize_skill(item: Any) -> dict[str, Any]:
    """将 SkillInfo 序列化为字典。"""
    result: dict[str, Any] = {
        "skill_id": item.skill_id,
        "name": item.name,
        "description": item.description,
        "directory_path": item.directory_path,
    }
    if item.license:
        result["license"] = item.license
    if item.compatibility:
        result["compatibility"] = item.compatibility
    if item.metadata:
        result["metadata"] = item.metadata
    if item.allowed_tools:
        result["allowed_tools"] = item.allowed_tools
    return result


def create_skill_cli_group(gateway: SkillGateway) -> click.Group:
    """创建 Skill CLI 命令组。

    Args:
        gateway: Skill 北向网关实例。

    Returns:
        click.Group 命令组，挂载到 spread-ai skill 下。
    """

    @click.group("skill", help="Skill 发现与管理")
    def skill_group() -> None:
        pass

    @skill_group.command("list", help="列出所有已发现的 Skill")
    def list_skills() -> None:
        """列出所有 Skill（完整元数据 + 目录路径）。"""
        skills = gateway.list_skills()
        items = [_serialize_skill(s) for s in skills]
        click.echo(json.dumps(items, ensure_ascii=False, indent=2))

    @skill_group.command("refresh", help="重新扫描 Skill 仓库目录")
    def refresh_skills() -> None:
        """触发重新扫描 Skill 仓库目录。"""
        gateway.refresh()
        skills = gateway.list_skills()
        click.echo(
            json.dumps(
                {"message": "Skill 仓库已重新扫描", "count": len(skills)},
                ensure_ascii=False,
                indent=2,
            )
        )

    return skill_group

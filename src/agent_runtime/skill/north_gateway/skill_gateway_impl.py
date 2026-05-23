# -*- coding: utf-8 -*-
"""Skill 北向网关实现。"""
from __future__ import annotations

from collections.abc import Callable

from agent_runtime.skill.infrastructure.skill_group_store import SkillGroupStore
from agent_runtime.skill.north_gateway.skill_contracts import SkillInfo
from agent_runtime.skill.settings_repository import SkillSettingsRepository
from agent_runtime.skill.skill_service import SkillService


class SkillGatewayImpl:
    """Skill 北向网关实现。

    将领域服务的返回值转换为北向网关 DTO，
    并合并应用层分组信息（来自 SkillGroupStore）。
    """

    def __init__(
        self,
        service: SkillService,
        group_store: SkillGroupStore,
        *,
        settings_repository: SkillSettingsRepository | None = None,
        default_skills_dir_accessor: Callable[[], str] | None = None,
    ) -> None:
        self._service = service
        self._group_store = group_store
        self._settings_repository = settings_repository
        self._default_skills_dir_accessor = default_skills_dir_accessor

    def list_skills(self) -> list[SkillInfo]:
        """列出所有已发现的 Skill（合并应用层分组）。"""
        items = self._service.list_skills()
        groups = self._group_store.get_all()
        return [
            SkillInfo(
                skill_id=item["skill_id"],
                name=item["name"],
                description=item["description"],
                directory_path=item["directory_path"],
                license=item.get("license"),
                compatibility=item.get("compatibility"),
                metadata=item.get("metadata", {}),
                allowed_tools=item.get("allowed_tools"),
                group=groups.get(item["skill_id"], SkillGroupStore.DEFAULT_GROUP),
            )
            for item in items
        ]

    def refresh(self) -> None:
        """触发重新扫描 Skill 仓库目录。"""
        self._service.refresh()

    def get_settings(self):
        if self._settings_repository is None or self._default_skills_dir_accessor is None:
            raise RuntimeError("Skill 设置仓储未初始化。")
        return self._settings_repository.get_settings(
            default_skills_dir=self._default_skills_dir_accessor(),
        )

    def save_settings(self, *, skills_dir: str):
        if self._settings_repository is None:
            raise RuntimeError("Skill 设置仓储未初始化。")
        settings = self._settings_repository.save_settings(skills_dir=skills_dir)
        self.refresh()
        return settings

    def settings_db_path(self):
        if self._settings_repository is None:
            raise RuntimeError("Skill 设置仓储未初始化。")
        return self._settings_repository.db_path()

    def set_skill_group(self, skill_id: str, group: str) -> None:
        """设置指定 Skill 的应用层分组。"""
        self._group_store.set_group(skill_id, group)

    def get_skill_content(self, skill_id: str) -> str:
        """读取指定 Skill 的 SKILL.md 正文（去除 frontmatter）。"""
        items = self._service.list_skills()
        target = next((s for s in items if s["skill_id"] == skill_id), None)
        if target is None:
            raise KeyError(f"Skill '{skill_id}' 不存在")

        from pathlib import Path

        skill_md = Path(target["directory_path"]) / "SKILL.md"
        if not skill_md.exists():
            raise FileNotFoundError(f"SKILL.md 不存在: {skill_md}")

        content = skill_md.read_text(encoding="utf-8")
        return _strip_frontmatter(content)


def _strip_frontmatter(content: str) -> str:
    """去除 Markdown 文件开头的 YAML frontmatter，返回正文部分。"""
    stripped = content.lstrip()
    if not stripped.startswith("---"):
        return content

    after_first = stripped[3:]
    if after_first.startswith("\n"):
        after_first = after_first[1:]
    elif after_first.startswith("\r\n"):
        after_first = after_first[2:]
    else:
        return content

    end_idx = after_first.find("\n---")
    if end_idx == -1:
        return content

    # 跳过 '---' 行及其后的换行
    body = after_first[end_idx + 4:]
    if body.startswith("\n"):
        body = body[1:]
    elif body.startswith("\r\n"):
        body = body[2:]
    return body

# -*- coding: utf-8 -*-
"""Skill 北向网关实现。"""
from __future__ import annotations

from collections.abc import Callable

from agent_runtime.skill.builtin_skill_installer import (
    BuiltinSkillInstaller,
    BuiltinSkillInstallResult,
)
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
        builtin_skill_installer: BuiltinSkillInstaller | None = None,
    ) -> None:
        self._service = service
        self._group_store = group_store
        self._settings_repository = settings_repository
        self._default_skills_dir_accessor = default_skills_dir_accessor
        self._builtin_skill_installer = builtin_skill_installer

    def list_skills(self) -> list[SkillInfo]:
        """列出所有已发现的 Skill（合并应用层分组）。"""
        self.ensure_builtin_skills()
        groups = self._group_store.get_all()
        return [self._to_skill_info(item, groups) for item in self._service.list_skills()]

    def get_skill(self, skill_id: str) -> SkillInfo:
        """根据 skill_id 获取 Skill 元数据详情。"""
        self.ensure_builtin_skills()
        groups = self._group_store.get_all()
        return self._to_skill_info(self._service.get_skill(skill_id), groups)

    def refresh(self) -> None:
        """内部/诊断用途：触发重新扫描 Skill 仓库目录。"""
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
        self.ensure_builtin_skills(settings.skills_dir)
        self.refresh()
        return settings

    def settings_db_path(self):
        if self._settings_repository is None:
            raise RuntimeError("Skill 设置仓储未初始化。")
        return self._settings_repository.db_path()

    def ensure_builtin_skills(self, skills_dir: str | None = None) -> list[BuiltinSkillInstallResult]:
        """幂等安装/升级 Runtime 随包内置 Skill，并在变更后刷新仓库缓存。"""
        if self._builtin_skill_installer is None:
            return []
        target_dir = skills_dir
        if target_dir is None:
            if self._settings_repository is None or self._default_skills_dir_accessor is None:
                return []
            target_dir = self._settings_repository.get_settings(
                default_skills_dir=self._default_skills_dir_accessor(),
            ).skills_dir
        results = self._builtin_skill_installer.install_all(target_dir)
        if any(result.changed for result in results):
            self.refresh()
        return results

    def set_skill_group(self, skill_id: str, group: str) -> None:
        """设置指定 Skill 的应用层分组。"""
        self._group_store.set_group(skill_id, group)

    def get_skill_content(self, skill_id: str) -> str:
        """读取指定 Skill 的 SKILL.md 正文（去除 frontmatter）。"""
        target = self._service.get_skill(skill_id)

        from pathlib import Path

        skill_md = Path(target["directory_path"]) / "SKILL.md"
        if not skill_md.exists():
            raise FileNotFoundError(f"SKILL.md 不存在: {skill_md}")

        content = skill_md.read_text(encoding="utf-8")
        return _strip_frontmatter(content)

    @staticmethod
    def _to_skill_info(item: dict[str, object], groups: dict[str, str]) -> SkillInfo:
        skill_id = str(item["skill_id"])
        return SkillInfo(
            skill_id=skill_id,
            name=str(item["name"]),
            description=str(item["description"]),
            directory_path=str(item["directory_path"]),
            license=item.get("license") if isinstance(item.get("license"), str) else None,
            compatibility=item.get("compatibility") if isinstance(item.get("compatibility"), str) else None,
            metadata=item.get("metadata", {}) if isinstance(item.get("metadata", {}), dict) else {},
            allowed_tools=item.get("allowed_tools") if isinstance(item.get("allowed_tools"), str) else None,
            group=groups.get(skill_id, SkillGroupStore.DEFAULT_GROUP),
        )


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

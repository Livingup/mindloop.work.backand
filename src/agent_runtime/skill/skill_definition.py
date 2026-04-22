# -*- coding: utf-8 -*-
"""Skill 领域实体（聚合根）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# name 允许格式：a-z、0-9、-，1~64 字符，不能以 - 开头或结尾，不能连续 --。
_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9\-]{0,62}[a-z0-9])?$")
_CONSECUTIVE_HYPHENS = re.compile(r"--")


@dataclass
class SkillDefinition:
    """Skill 定义实体。

    对应文件系统上的一个 skill 目录，元数据来自 SKILL.md 的 YAML frontmatter。
    skill_id 即 name，也是目录名。
    """

    # Skill 唯一标识（= name = 目录名）。
    skill_id: str

    # Skill 名称（必须与 skill_id 和目录名一致）。
    name: str

    # Skill 描述（做什么、何时使用）。
    description: str

    # Skill 目录的绝对路径。
    directory_path: str

    # 许可证（可选）。
    license: str | None = None

    # 环境/依赖要求（可选）。
    compatibility: str | None = None

    # 额外元数据（可选，字符串到字符串映射）。
    metadata: dict[str, str] = field(default_factory=dict)

    # 允许的工具列表（可选，空格分隔的字符串，实验性字段）。
    allowed_tools: str | None = None

    # ── 业务规则 ──

    def validate(self) -> None:
        """校验 Skill 定义的完整性和格式合规性。

        校验规则：
        - name 必须符合规范（a-z/0-9/-，1~64 字符，不能以 - 开头结尾，不能连续 --）
        - name 必须与 skill_id 一致
        - description 不能为空

        Raises:
            ValueError: 校验失败时抛出，包含具体错误描述。
        """
        if not _NAME_PATTERN.match(self.name) or _CONSECUTIVE_HYPHENS.search(self.name):
            raise ValueError(
                f"skill name '{self.name}' 格式无效，"
                "要求：a-z/0-9/-，1~64 字符，不能以 - 开头或结尾，不能包含连续 --"
            )

        if self.name != self.skill_id:
            raise ValueError(
                f"skill name '{self.name}' 与 skill_id '{self.skill_id}' 不一致"
            )

        if not self.description or not self.description.strip():
            raise ValueError("skill description 不能为空")

        if self.metadata:
            for k, v in self.metadata.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise ValueError(
                        f"metadata 必须是字符串到字符串映射，键 '{k}' 或值 '{v}' 类型无效"
                    )

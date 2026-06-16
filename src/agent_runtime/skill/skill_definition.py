# -*- coding: utf-8 -*-
"""Skill 领域实体（聚合根）。"""
from __future__ import annotations

from dataclasses import dataclass, field


# Skill ID 只保留必要安全边界：非空、无空白、无路径分隔符、无控制字符。
_PATH_SEPARATORS = ("/", "\\")


@dataclass
class SkillDefinition:
    """Skill 定义实体。

    对应文件系统上的一个 skill 目录，元数据来自 SKILL.md 的 YAML frontmatter。
    skill_id 即 frontmatter name；物理目录名只作为承载路径，不再要求与 name 完全一致。
    """

    # Skill 唯一标识（= frontmatter name）。
    skill_id: str

    # Skill 名称（必须与 skill_id 一致，可使用大小写；不要求与物理目录名一致）。
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
        - name 必须是稳定 Skill ID，且必须与 skill_id 一致
        - name 只做必要安全校验：非空、无前后空白、无空白字符、无路径分隔符、无控制字符
        - description 不能为空

        Raises:
            ValueError: 校验失败时抛出，包含具体错误描述。
        """
        if not isinstance(self.name, str):
            raise ValueError("skill name 必须是字符串")

        if not isinstance(self.skill_id, str):
            raise ValueError("skill_id 必须是字符串")

        if self.name != self.skill_id:
            raise ValueError(
                f"skill name '{self.name}' 与 skill_id '{self.skill_id}' 不一致"
            )

        if not self.name or not self.name.strip():
            raise ValueError("skill name 不能为空")

        if self.name != self.name.strip() or any(ch.isspace() for ch in self.name):
            raise ValueError("skill name 不能包含空白字符或前后空白")

        if any(separator in self.name for separator in _PATH_SEPARATORS):
            raise ValueError("skill name 不能包含路径分隔符 / 或 \\")

        if any(ord(ch) < 32 or ord(ch) == 127 for ch in self.name):
            raise ValueError("skill name 不能包含控制字符")

        if not self.description or not self.description.strip():
            raise ValueError("skill description 不能为空")

        if self.metadata:
            for k, v in self.metadata.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise ValueError(
                        f"metadata 必须是字符串到字符串映射，键 '{k}' 或值 '{v}' 类型无效"
                    )

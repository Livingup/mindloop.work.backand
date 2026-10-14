# -*- coding: utf-8 -*-
"""Skill 领域实体（聚合根）。"""
from __future__ import annotations

from dataclasses import dataclass, field


# Skill name / skill_id 采用“文件目录名可接受字符”规则：
# 允许大小写、空格和 Unicode，但禁止路径分隔符、Windows 文件名保留字符、
# 控制字符、仅由空白组成的名称、"." / ".."，以及 Windows 保留设备名。
_WINDOWS_INVALID_FILE_NAME_CHARS = set('<>:"/\\|?*')
_WINDOWS_RESERVED_FILE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass
class SkillDefinition:
    """Skill 定义实体。

    对应文件系统上的一个 skill 目录，元数据来自 SKILL.md 的 YAML frontmatter。
    skill_id 即 frontmatter name；物理目录名只作为承载路径，不再要求与 name 完全一致。
    """

    # Skill 唯一标识（= frontmatter name）。
    skill_id: str

    # Skill 名称（必须与 skill_id 一致，校验规则与文件目录名一致；不要求与物理目录名一致）。
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
        - name 的可接受字符规则与文件目录名一致：允许大小写、空格和 Unicode，
          但不能是空白、"." / ".."，不能包含路径分隔符、Windows 非法文件名字符、
          控制字符，不能以空格或点号结尾，也不能使用 Windows 保留设备名
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
            raise ValueError("skill name 不能为空白字符串")

        if self.name in {".", ".."}:
            raise ValueError("skill name 不能是 '.' 或 '..'")

        if any(ch in _WINDOWS_INVALID_FILE_NAME_CHARS for ch in self.name):
            raise ValueError(
                "skill name 不能包含 Windows 非法文件名字符 <>:\"/\\|?* 或路径分隔符"
            )

        if any(ord(ch) < 32 or ord(ch) == 127 for ch in self.name):
            raise ValueError("skill name 不能包含控制字符")

        if self.name.endswith((" ", ".")):
            raise ValueError("skill name 不能以空格或点号结尾")

        if self.name.upper() in _WINDOWS_RESERVED_FILE_NAMES:
            raise ValueError(f"skill name 不能使用 Windows 保留设备名: {self.name}")

        if not self.description or not self.description.strip():
            raise ValueError("skill description 不能为空")

        if self.metadata:
            for k, v in self.metadata.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise ValueError(
                        f"metadata 必须是字符串到字符串映射，键 '{k}' 或值 '{v}' 类型无效"
                    )

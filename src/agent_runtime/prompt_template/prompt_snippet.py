"""提示词片段领域实体。"""
from __future__ import annotations

import re
from dataclasses import dataclass

# name 最大长度。
_NAME_MAX_LENGTH = 50

# category 合法字符：字母、数字、下划线、连字符。
_CATEGORY_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")

# category 最大长度。
_CATEGORY_MAX_LENGTH = 50


@dataclass(frozen=True, slots=True)
class PromptSnippet:
    """提示词可复用片段实体。

    由唯一描述名标识，Agent 定义中通过 ref 引用 name 来拼接系统提示词。
    可通过 category 归入子目录分类组织。

    业务规则（在实体内部校验）：
    - name：非空，长度不超过 50，不含路径分隔符。
    - category：默认 "default"，仅允许字母、数字、下划线、连字符，长度不超过 50，
      不允许路径穿越（如 '..'）。
    """

    # 片段唯一描述名（业务标识，如"业务架构设计规则"）。
    name: str

    # 提示词文本内容。
    text: str

    # 分类目录（如"coding"、"architecture"），默认为 "default"。
    category: str = "default"

    def __post_init__(self) -> None:
        """实体创建时校验业务规则。"""
        # ── name 校验 ─────────────────────────────────────────────
        if not self.name or not self.name.strip():
            raise ValueError("片段描述名不能为空。")
        if len(self.name) > _NAME_MAX_LENGTH:
            raise ValueError(
                f"片段描述名长度不能超过 {_NAME_MAX_LENGTH}，"
                f"当前长度: {len(self.name)}。",
            )
        if "/" in self.name or "\\" in self.name:
            raise ValueError("片段描述名不能包含路径分隔符（/ 或 \\）。")

        # ── category 校验 ────────────────────────────────────────
        if not self.category or not self.category.strip():
            raise ValueError("分类目录不能为空。")
        if ".." in self.category:
            raise ValueError("分类目录不允许包含 '..'。")
        if len(self.category) > _CATEGORY_MAX_LENGTH:
            raise ValueError(
                f"分类目录长度不能超过 {_CATEGORY_MAX_LENGTH}，"
                f"当前长度: {len(self.category)}。",
            )
        if not _CATEGORY_PATTERN.match(self.category):
            raise ValueError(
                "分类目录仅允许字母、数字、下划线和连字符。"
                f"当前值: '{self.category}'。",
            )

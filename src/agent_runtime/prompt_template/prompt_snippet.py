"""提示词片段聚合根。

聚合根封装业务规则校验和业务行为方法，外部通过聚合根的行为方法操作业务状态，
不直接构造新实例来表达业务变更。

采用 frozen 不可变设计：业务行为方法返回新的聚合根实例（函数式不可变模式），
新实例同样经过 __post_init__ 业务规则校验，保证不变量始终成立。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# name 最大长度。
_NAME_MAX_LENGTH = 50

# category 合法字符：中文、字母、数字、下划线、连字符。
_CATEGORY_PATTERN = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9_\-]+$")

# category 最大长度。
_CATEGORY_MAX_LENGTH = 50


@dataclass(frozen=True, slots=True)
class PromptSnippet:
    """提示词可复用片段聚合根。

    由唯一描述名标识，Agent 定义中通过 ref 引用 name 来拼接系统提示词。
    可通过 category 归入子目录分类组织。

    业务规则（不变量，在实体创建时校验）：
    - name：非空，长度不超过 50，不含路径分隔符。
    - category：默认 "default"，仅允许中文、字母、数字、下划线、连字符，长度不超过 50，
      不允许路径穿越（如 '..'）。

    业务行为方法：
    - with_updates()：应用部分更新，返回新的聚合根实例（不可变模式）。
    """

    # 片段唯一描述名（业务标识，如"业务架构设计规则"）。
    name: str

    # 提示词文本内容。
    text: str

    # 分类目录（如"coding"、"architecture"），默认为 "default"。
    category: str = "default"

    def __post_init__(self) -> None:
        """聚合根创建时校验业务不变量。"""
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
                "分类目录仅允许中文、字母、数字、下划线和连字符。"
                f"当前值: '{self.category}'。",
            )

    # ── 业务行为方法 ─────────────────────────────────────────────

    def with_updates(
        self,
        *,
        new_name: str | None = None,
        new_text: str | None = None,
        new_category: str | None = None,
    ) -> PromptSnippet:
        """应用部分更新，返回新的聚合根实例。

        仅传入非 None 的字段会被更新，未传入的字段保持当前值。
        返回的新实例会经过 __post_init__ 校验，保证业务不变量。

        Args:
            new_name: 新的描述名，None 表示不变。
            new_text: 新的文本内容，None 表示不变。
            new_category: 新的分类目录，None 表示不变。

        Returns:
            更新后的新聚合根实例。
        """
        return PromptSnippet(
            name=new_name if new_name is not None else self.name,
            text=new_text if new_text is not None else self.text,
            category=new_category if new_category is not None else self.category,
        )


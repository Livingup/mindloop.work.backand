"""提示词片段聚合根。

聚合根封装业务规则校验和业务行为方法，外部通过聚合根的行为方法操作业务状态，
不直接构造新实例来表达业务变更。

采用 frozen 不可变设计：业务行为方法返回新的聚合根实例（函数式不可变模式），
新实例同样经过 __post_init__ 业务规则校验，保证不变量始终成立。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

# name 最大长度。
_NAME_MAX_LENGTH = 50

# category 合法字符：中文、字母、数字、下划线、连字符。
_CATEGORY_PATTERN = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9_\-]+$")

# category 最大长度。
_CATEGORY_MAX_LENGTH = 50


class PromptSnippetValidationError(ValueError):
    """提示词片段参数校验失败（结构性错误，对应 HTTP 400）。"""
    pass


class PromptSnippetConflictError(ValueError):
    """提示词片段状态或匹配冲突（命中数不为 1 或并发版本冲突，对应 HTTP 409）。"""
    pass


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
    - apply_text_edits()：应用连续行精确替换，返回仅正文变化的新聚合根实例。
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

    def apply_text_edits(
        self,
        edits: Sequence[Any],
    ) -> PromptSnippet:
        """按顺序对正文应用连续行精确替换，返回仅正文变化的新聚合根。

        业务规则：
        - edits 列表不能为空，否则抛出 PromptSnippetValidationError。
        - 每个 edit 的 search_lines 必须非空，且必须在当前正文中恰好唯一命中一次。
        - 若 search_lines 命中 0 次或超过 1 次，抛出 PromptSnippetConflictError。
        - replace_lines 可以为空列表（表示删除匹配的行区间），也可以包含更多或更少的行。
        - 保持原正文的换行符风格（\\r\\n 或 \\n）和末尾换行符状态。
        - 仅更新 text，name 和 category 保持不变。
        """
        if not edits:
            raise PromptSnippetValidationError("正文编辑列表不能为空。")

        lines = self.text.splitlines()
        newline = "\r\n" if "\r\n" in self.text else "\r" if "\r" in self.text else "\n"
        has_final_newline = self.text.endswith(("\r\n", "\n", "\r"))

        for edit in edits:
            if hasattr(edit, "search_lines") and hasattr(edit, "replace_lines"):
                search_lines = edit.search_lines
                replace_lines = edit.replace_lines
            elif isinstance(edit, (tuple, list)) and len(edit) >= 2:
                search_lines, replace_lines = edit[0], edit[1]
            else:
                raise PromptSnippetValidationError("编辑项必须包含 search_lines 和 replace_lines。")

            if not search_lines:
                raise PromptSnippetValidationError("search_lines 必须至少包含一行。")

            search_list = list(search_lines)
            replace_list = list(replace_lines) if replace_lines is not None else []

            for line in search_list:
                if "\r" in line or "\n" in line:
                    raise PromptSnippetValidationError("search_lines 中的每一行不能包含换行符。")
            for line in replace_list:
                if "\r" in line or "\n" in line:
                    raise PromptSnippetValidationError("replace_lines 中的每一行不能包含换行符。")

            target_len = len(search_list)
            matches = [
                index
                for index in range(len(lines) - target_len + 1)
                if lines[index : index + target_len] == search_list
            ]
            if len(matches) != 1:
                raise PromptSnippetConflictError(
                    f"正文编辑 search_lines 必须恰好命中一次；实际命中 {len(matches)} 次。"
                )
            start = matches[0]
            lines[start : start + target_len] = replace_list

        if not lines:
            updated_text = ""
        else:
            updated_text = newline.join(lines)
            if has_final_newline:
                updated_text += newline

        return PromptSnippet(name=self.name, text=updated_text, category=self.category)



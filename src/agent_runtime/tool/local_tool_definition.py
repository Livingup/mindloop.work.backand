"""本地工具领域实体。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

# 工具名称最大长度。
_TOOL_NAME_MAX_LENGTH = 80

# 工具名称允许的字符正则。
_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")


@dataclass(frozen=True, slots=True)
class LocalToolDefinition:
    """本地工具定义实体（聚合根）。

    描述一个本地工具的静态元数据和可执行引用。
    Agent 定义中通过 tool_name 引用本地工具。

    业务规则（在实体内部校验）：
    - tool_name：非空，长度不超过 80，仅允许字母、数字、下划线、连字符。
    - description：非空（LLM 看到的工具描述）。
    - fn：必须是可调用对象。
    - group：非空，默认 "default"（工具分组，如 "文件"、"网络"）。
    - public：是否公开，默认 True（列出所有工具时是否可见）。
    - wiki：人类可读的工具介绍文档，可为 None。
    """

    # 工具唯一名称（如 "search_files"），Agent 定义中通过此名称引用。
    tool_name: str

    # 工具描述，传给 LLM 的 function description（简洁、功能导向）。
    description: str

    # 实际执行函数引用。
    fn: Callable[..., Any]

    # 工具分组（如 "文件"、"网络"、"数据库"），默认 "default"。
    group: str = "default"

    # 是否公开（列出所有工具时是否可见），默认 True。
    public: bool = True

    # 人类可读的工具介绍文档（用于生成系统工具说明文档），可为 None。
    wiki: str | None = None

    def __post_init__(self) -> None:
        """实体创建时校验业务规则。"""
        # ── tool_name 校验 ──────────────────────────────────────────
        if not self.tool_name or not self.tool_name.strip():
            raise ValueError("工具名称不能为空。")
        if len(self.tool_name) > _TOOL_NAME_MAX_LENGTH:
            raise ValueError(
                f"工具名称长度不能超过 {_TOOL_NAME_MAX_LENGTH}，"
                f"当前长度: {len(self.tool_name)}。",
            )
        if not _TOOL_NAME_PATTERN.match(self.tool_name):
            raise ValueError(
                "工具名称仅允许字母、数字、下划线和连字符。"
                f"当前值: '{self.tool_name}'。",
            )

        # ── description 校验 ────────────────────────────────────────
        if not self.description or not self.description.strip():
            raise ValueError("工具描述不能为空。")

        # ── fn 校验 ─────────────────────────────────────────────────
        if not callable(self.fn):
            raise ValueError("fn 必须是可调用对象。")

        # ── group 校验 ──────────────────────────────────────────────
        if not self.group or not self.group.strip():
            raise ValueError("工具分组不能为空。")

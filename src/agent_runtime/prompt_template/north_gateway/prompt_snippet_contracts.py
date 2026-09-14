"""提示词片段增删改查业务北向契约。

该模块定义提示词模板服务对外暴露的请求模型、结果模型和服务接口。
所有调用方（程序内部、REST API、CLI）均依赖此契约。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from agent_runtime.prompt_template.prompt_snippet import PromptSnippet



@dataclass(frozen=True, slots=True)
class PromptSnippetTextEdit:
    """提示词正文中的一个连续行替换编辑。"""

    search_lines: Sequence[str]
    replace_lines: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class PromptSnippetEditRequest:
    """提示词正文局部编辑请求。"""

    # 目标片段唯一描述名。
    name: str

    # 连续行编辑项列表。
    edits: Sequence[PromptSnippetTextEdit]

    # 可选预期正文 SHA256，用于乐观并发控制。
    expected_text_sha256: str | None = None


# 别名兼容
PromptSnippetEditContentRequest = PromptSnippetEditRequest



@dataclass(frozen=True, slots=True)
class PromptSnippetCreateRequest:
    """提示词片段创建请求。

    调用方提交唯一描述名和提示词文本，由服务创建 YAML 文件并注册到索引。
    """

    # 片段唯一描述名。
    name: str

    # 提示词文本内容。
    text: str

    # 分类目录（如"coding"），默认为 "default"。
    category: str = "default"


@dataclass(frozen=True, slots=True)
class PromptSnippetUpdateRequest:
    """提示词片段更新请求。

    支持更新描述名或文本内容，未提供的字段保持不变。
    """

    # 当前片段唯一描述名（用于定位目标片段）。
    current_name: str

    # 新的唯一描述名（为 None 时不修改）。
    new_name: str | None = None

    # 新的提示词文本内容（为 None 时不修改）。
    new_text: str | None = None

    # 新的分类目录（为 None 时不修改）。
    new_category: str | None = None


class PromptSnippetNorthGateway(Protocol):
    """提示词片段增删改查业务北向网关接口。"""

    def get_snippet(self, name: str) -> PromptSnippet:
        """根据唯一描述名获取片段。未找到时抛出 KeyError。"""
        ...

    def list_snippets(self) -> list[PromptSnippet]:
        """列出所有已注册的提示词片段。"""
        ...

    def create_snippet(self, request: PromptSnippetCreateRequest) -> PromptSnippet:
        """创建提示词片段。name 已存在时抛出 ValueError。"""
        ...

    def update_snippet(self, request: PromptSnippetUpdateRequest) -> PromptSnippet:
        """更新提示词片段。目标片段不存在时抛出 KeyError。"""
        ...

    def edit_snippet_content(self, request: PromptSnippetEditRequest) -> PromptSnippet:
        """按连续行唯一匹配局部编辑提示词正文。目标片段不存在时抛出 KeyError。"""
        ...

    def delete_snippet(self, name: str) -> None:
        """删除提示词片段。不存在时静默跳过。"""
        ...

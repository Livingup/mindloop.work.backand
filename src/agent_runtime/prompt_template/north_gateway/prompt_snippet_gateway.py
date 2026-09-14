"""提示词片段增删改查北向网关实现。

网关是对外暴露的服务入口，内部委托给 PromptSnippetService 执行业务逻辑。
"""
from __future__ import annotations

from agent_runtime.prompt_template.prompt_snippet import PromptSnippet
from agent_runtime.prompt_template.north_gateway.prompt_snippet_contracts import (
    PromptSnippetCreateRequest,
    PromptSnippetEditRequest,
    PromptSnippetUpdateRequest,
)
from agent_runtime.prompt_template.prompt_snippet_service import PromptSnippetService


class PromptSnippetGateway:
    """提示词片段增删改查北向网关。

    实现 PromptSnippetNorthGateway 协议，委托内部 service 执行业务逻辑。
    调用方（REST、CLI、程序内部）统一通过本网关访问提示词片段能力。
    """

    def __init__(self, service: PromptSnippetService) -> None:
        self._service = service

    def get_snippet(self, name: str) -> PromptSnippet:
        """根据唯一描述名获取片段。未找到时抛出 KeyError。"""
        return self._service.get_snippet(name)

    def list_snippets(self) -> list[PromptSnippet]:
        """列出所有已注册的提示词片段。"""
        return self._service.list_snippets()

    def create_snippet(self, request: PromptSnippetCreateRequest) -> PromptSnippet:
        """创建提示词片段。name 已存在时抛出 ValueError。"""
        return self._service.create_snippet(request)

    def update_snippet(self, request: PromptSnippetUpdateRequest) -> PromptSnippet:
        """更新提示词片段。目标片段不存在时抛出 KeyError。"""
        return self._service.update_snippet(request)

    def edit_snippet_content(self, request: PromptSnippetEditRequest) -> PromptSnippet:
        """按连续行唯一匹配局部编辑提示词正文。"""
        return self._service.edit_snippet_content(request)

    def delete_snippet(self, name: str) -> None:
        """删除提示词片段。不存在时静默跳过。"""
        self._service.delete_snippet(name)


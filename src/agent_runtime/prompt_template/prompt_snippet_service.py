"""提示词片段领域服务。

纯业务逻辑层，依赖仓储接口，不直接操作文件系统。
"""
from __future__ import annotations

import logging

from agent_runtime.prompt_template.prompt_snippet import PromptSnippet
from agent_runtime.prompt_template.north_gateway.prompt_snippet_contracts import (
    PromptSnippetCreateRequest,
    PromptSnippetUpdateRequest,
)
from agent_runtime.prompt_template.prompt_snippet_repository import (
    PromptSnippetRepository,
)

logger = logging.getLogger(__name__)


class PromptSnippetService:
    """提示词片段领域服务。

    负责提示词片段的增删改查业务规则校验，持久化委托给仓储。
    外部调用方不直接使用本类，而是通过北向网关访问。
    """

    def __init__(self, repo: PromptSnippetRepository) -> None:
        self._repo = repo

    # ── 片段增删改查 ───────────────────────────────────────────────

    def get_snippet(self, name: str) -> PromptSnippet:
        """根据唯一描述名获取片段。未找到时抛出 KeyError。"""
        snippet = self._repo.find_by_name(name)
        if snippet is None:
            raise KeyError(f"提示词片段不存在: {name}")
        return snippet

    def list_snippets(self) -> list[PromptSnippet]:
        """列出所有已注册的提示词片段。"""
        return self._repo.find_all()

    def create_snippet(self, request: PromptSnippetCreateRequest) -> PromptSnippet:
        """创建提示词片段。name 已存在时抛出 ValueError。"""
        if self._repo.exists(request.name):
            raise ValueError(f"提示词片段已存在: {request.name}")
        snippet = PromptSnippet(
            name=request.name, text=request.text, category=request.category,
        )
        self._repo.save(snippet)
        logger.info("创建提示词片段: %s", request.name)
        return snippet

    def update_snippet(self, request: PromptSnippetUpdateRequest) -> PromptSnippet:
        """更新提示词片段。目标片段不存在时抛出 KeyError。"""
        current = self._repo.find_by_name(request.current_name)
        if current is None:
            raise KeyError(f"提示词片段不存在: {request.current_name}")

        new_name = request.new_name if request.new_name is not None else current.name
        new_text = request.new_text if request.new_text is not None else current.text
        new_category = (
            request.new_category if request.new_category is not None
            else current.category
        )

        # 如果修改了 name，校验新 name 不与其他片段冲突。
        if new_name != current.name and self._repo.exists(new_name):
            raise ValueError(f"提示词片段已存在: {new_name}")

        # 如果 name 变更，需要更新仓储索引映射。
        if new_name != current.name:
            self._repo.rename(current.name, new_name)

        updated = PromptSnippet(name=new_name, text=new_text, category=new_category)
        self._repo.save(updated)
        logger.info("更新提示词片段: %s -> %s", request.current_name, new_name)
        return updated

    def delete_snippet(self, name: str) -> None:
        """删除提示词片段。不存在时静默跳过。"""
        if self._repo.delete(name):
            logger.info("删除提示词片段: %s", name)

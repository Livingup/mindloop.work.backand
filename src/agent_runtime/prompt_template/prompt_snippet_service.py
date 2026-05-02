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
        """更新提示词片段。

        编排流程（模式 C）：
        1. 从仓储获取当前聚合根
        2. 调用聚合根的 with_updates() 业务行为方法，由聚合根自身完成字段合并和校验
        3. 如果 name 变更，校验唯一性约束并更新仓储索引
        4. 调用仓储 save() 落地更新后的聚合根

        目标片段不存在时抛出 KeyError。
        """
        # 1. 从仓储获取聚合根。
        current = self._repo.find_by_name(request.current_name)
        if current is None:
            raise KeyError(f"提示词片段不存在: {request.current_name}")

        # 2. 通过聚合根行为方法应用更新（聚合根内部完成字段合并和不变量校验）。
        updated = current.with_updates(
            new_name=request.new_name,
            new_text=request.new_text,
            new_category=request.new_category,
        )

        # 3. name 变更时的跨聚合根约束（唯一性检查属于领域服务职责）。
        if updated.name != current.name:
            if self._repo.exists(updated.name):
                raise ValueError(f"提示词片段已存在: {updated.name}")
            self._repo.rename(current.name, updated.name)

        # 4. 仓储落地聚合根。
        self._repo.save(updated)
        logger.info("更新提示词片段: %s -> %s", request.current_name, updated.name)
        return updated

    def delete_snippet(self, name: str) -> None:
        """删除提示词片段。不存在时静默跳过。"""
        if self._repo.delete(name):
            logger.info("删除提示词片段: %s", name)

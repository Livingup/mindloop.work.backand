"""提示词模板服务依赖装配。"""
from __future__ import annotations

from agent_runtime.prompt_template.north_gateway.prompt_snippet_gateway import (
    PromptSnippetGateway,
)
from agent_runtime.prompt_template.prompt_snippet_service import PromptSnippetService
from agent_runtime.prompt_template.prompt_template_config import PromptTemplateConfig
from agent_runtime.prompt_template.infrastructure.yaml_snippet_repository import (
    YamlFileSnippetRepository,
)


class PromptTemplateFactory:
    """提示词模板服务工厂。"""

    @staticmethod
    def create(config: PromptTemplateConfig) -> PromptSnippetGateway:
        """根据配置创建提示词模板北向网关实例。

        装配链路：YAML 文件仓储 → 领域服务 → 北向网关。
        """
        repo = YamlFileSnippetRepository(config.snippets_dir)
        service = PromptSnippetService(repo)
        return PromptSnippetGateway(service)

"""提示词模板服务依赖装配。"""
from __future__ import annotations

from agent_runtime.prompt_template.north_gateway.prompt_snippet_gateway import (
    PromptSnippetGateway,
)
from agent_runtime.prompt_template.prompt_snippet_service import PromptSnippetService
from agent_runtime.prompt_template.infrastructure.yaml_snippet_repository import (
    YamlFileSnippetRepository,
)


class PromptTemplateFactory:
    """提示词模板服务工厂。"""

    @staticmethod
    def create() -> PromptSnippetGateway:
        """创建提示词模板北向网关实例。

        配置通过访问器从 ConfigRegistry 实时读取，不存储配置副本。
        装配链路：YAML 文件仓储 → 领域服务 → 北向网关。
        """
        from agent_runtime.config import ConfigRegistry

        if ConfigRegistry.instance().cloud_user_config_database.is_postgresql_enabled:
            from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore
            from agent_runtime.prompt_template.infrastructure.postgres_prompt_snippet_repository import (
                PostgresPromptSnippetRepository,
            )

            store = PostgresUserConfigStore(
                lambda: ConfigRegistry.instance().cloud_user_config_database,
            )
            repo = PostgresPromptSnippetRepository(lambda: store)
        else:
            repo = YamlFileSnippetRepository(
                snippets_dir_accessor=lambda: ConfigRegistry.instance().prompt_template.snippets_dir,
            )
        service = PromptSnippetService(repo)
        return PromptSnippetGateway(service)

from __future__ import annotations

from agent_runtime.codex_oauth.infrastructure.sqlite_codex_oauth_repository import (
    SqliteCodexOAuthRepository,
)
from agent_runtime.codex_oauth.service import CodexOAuthService
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmModelRegistryGateway,
)


class CodexOAuthFactory:
    @staticmethod
    def create_service(
        llm_registry_gateway: LlmModelRegistryGateway | None = None,
    ) -> CodexOAuthService:
        from agent_runtime.config import ConfigRegistry

        repository = SqliteCodexOAuthRepository(
            data_dir_accessor=lambda: ConfigRegistry.instance().codex_oauth.data_dir,
        )
        return CodexOAuthService(
            repository=repository,
            config_accessor=lambda: ConfigRegistry.instance().codex_oauth,
            llm_registry_gateway=llm_registry_gateway,
        )

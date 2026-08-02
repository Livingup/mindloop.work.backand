from __future__ import annotations

from threading import RLock

from agent_runtime.codex_oauth.service import CodexOAuthService
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmModelRegistryGateway,
 )


class CodexOAuthFactory:
    """Process-local owner registry for the single louyi-codex application service."""

    _lock = RLock()
    _service: CodexOAuthService | None = None

    @classmethod
    def create_service(
        cls,
        llm_registry_gateway: LlmModelRegistryGateway | None = None,
    ) -> CodexOAuthService:
        from agent_runtime.config import ConfigRegistry

        with cls._lock:
            if cls._service is None:
                cls._service = CodexOAuthService(
                    config_accessor=lambda: ConfigRegistry.instance().codex_oauth,
                    llm_registry_gateway=llm_registry_gateway,
                )
            else:
                cls._service.set_llm_registry_gateway(llm_registry_gateway)
            return cls._service

    @classmethod
    def reset_for_runtime_mode_change(cls) -> None:
        """Keep the SDK owner stable; only registry gateways are replaced in place."""
        return

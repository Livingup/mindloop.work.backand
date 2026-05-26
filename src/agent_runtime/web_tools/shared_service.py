from __future__ import annotations

from threading import Lock

from agent_runtime.web_tools.service import WebToolsService

_web_tools_service: WebToolsService | None = None
_web_tools_service_lock = Lock()


def get_web_tools_service() -> WebToolsService:
    global _web_tools_service
    if _web_tools_service is not None:
        return _web_tools_service
    with _web_tools_service_lock:
        if _web_tools_service is None:
            from agent_runtime.config import ConfigRegistry
            from agent_runtime.llm_client.llm_client_factory import LlmClientFactory
            from agent_runtime.llm_model_registry.llm_model_registry_factory import LlmModelRegistryFactory
            from agent_runtime.web_tools.settings_repository import WebToolsSettingsRepository

            _web_tools_service = WebToolsService(
                settings_repository=WebToolsSettingsRepository(
                    data_dir_accessor=lambda: ConfigRegistry.instance().llm_model_registry.registry_dir,
                ),
                llm_registry_gateway=LlmModelRegistryFactory.create(),
                llm_request_service=LlmClientFactory.create(),
            )
        return _web_tools_service

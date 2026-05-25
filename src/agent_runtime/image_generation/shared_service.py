from __future__ import annotations

from threading import Lock

from agent_runtime.image_generation.service import ImageGenerationService

_image_generation_service: ImageGenerationService | None = None
_image_generation_service_lock = Lock()


def get_image_generation_service() -> ImageGenerationService:
    global _image_generation_service
    if _image_generation_service is not None:
        return _image_generation_service
    with _image_generation_service_lock:
        if _image_generation_service is None:
            from agent_runtime.config import ConfigRegistry
            from agent_runtime.file_resource.shared_service import get_file_resource_service
            from agent_runtime.image_generation.settings_repository import ImageGenerationSettingsRepository
            from agent_runtime.llm_model_registry.llm_model_registry_factory import LlmModelRegistryFactory

            _image_generation_service = ImageGenerationService(
                settings_repository=ImageGenerationSettingsRepository(
                    data_dir_accessor=lambda: ConfigRegistry.instance().llm_model_registry.registry_dir,
                ),
                llm_registry_gateway=LlmModelRegistryFactory.create_image_generation_registry(),
                file_resource_service=get_file_resource_service(),
            )
        return _image_generation_service

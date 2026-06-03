# -*- coding: utf-8 -*-
"""LLM 服务商与模型注册服务依赖装配。"""
from __future__ import annotations

from agent_runtime.llm_model_registry.infrastructure.sqlite_llm_model_registry_repository import (
    SqliteLlmModelRegistryRepository,
)
from agent_runtime.llm_model_registry.builtin_models import (
    builtin_image_generation_model_metadata,
    builtin_llm_model_metadata,
)
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    LlmModelRegistryService,
)
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_gateway_impl import (
    LlmModelRegistryGatewayImpl,
)


class LlmModelRegistryFactory:
    """LLM 服务商与模型注册服务工厂。

    工厂是唯一知道具体仓储实现和 ConfigRegistry 的装配位置。
    领域层只依赖 Callable 配置访问器和仓储接口，不感知全局配置注册中心。
    """

    @staticmethod
    def _create_repository(
        *,
        db_filename: str | None = None,
        namespace_prefix: str = "",
    ):
        from agent_runtime.config import ConfigRegistry

        if ConfigRegistry.instance().cloud_user_config_database.is_postgresql_enabled:
            from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore
            from agent_runtime.llm_model_registry.infrastructure.postgres_llm_model_registry_repository import (
                PostgresLlmModelRegistryRepository,
            )

            store = PostgresUserConfigStore(
                lambda: ConfigRegistry.instance().cloud_user_config_database,
            )
            return PostgresLlmModelRegistryRepository(
                lambda: store,
                namespace_prefix=namespace_prefix,
            )

        kwargs = {}
        if db_filename is not None:
            kwargs["db_filename"] = db_filename
        return SqliteLlmModelRegistryRepository(
            registry_dir_accessor=(
                lambda: ConfigRegistry.instance().llm_model_registry.registry_dir
            ),
            **kwargs,
        )

    @staticmethod
    def create() -> LlmModelRegistryGatewayImpl:
        """创建 LLM 注册模块北向网关实例。

        配置通过访问器从 ConfigRegistry 实时读取，不存储配置副本。
        装配链路：SQLite 仓储 → 领域服务 → 北向网关。
        """
        repository = LlmModelRegistryFactory._create_repository()
        service = LlmModelRegistryService(repository)
        for metadata in builtin_llm_model_metadata():
            service.ensure_builtin_model_metadata(metadata)
        return LlmModelRegistryGatewayImpl(service)

    @staticmethod
    def create_image_generation_registry():
        from agent_runtime.image_generation.llm_registry_gateway import (
            GPT_IMAGE_AGENT_MODEL_ID,
            ImageGenerationLlmRegistryGateway,
        )
        from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_RESPONSES

        llm_registry_gateway = LlmModelRegistryFactory.create()

        def external_provider_model_resolver(provider_id: str, binding_id: str):
            try:
                resolved = llm_registry_gateway.resolve_provider_model(provider_id, binding_id)
            except Exception:
                return None
            if resolved.model_id == GPT_IMAGE_AGENT_MODEL_ID and resolved.api_format == API_FORMAT_OPENAI_RESPONSES:
                return resolved
            return None

        repository = LlmModelRegistryFactory._create_repository(
            db_filename="image_generation_llm_registry.sqlite3",
            namespace_prefix="image_generation_",
        )
        service = LlmModelRegistryService(
            repository,
            external_provider_model_resolver=external_provider_model_resolver,
        )
        for metadata in builtin_image_generation_model_metadata():
            service.ensure_builtin_model_metadata(metadata)
        return ImageGenerationLlmRegistryGateway(
            image_registry_gateway=LlmModelRegistryGatewayImpl(service),
            llm_registry_gateway=llm_registry_gateway,
        )

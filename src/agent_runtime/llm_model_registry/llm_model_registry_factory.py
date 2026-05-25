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

    工厂是唯一知道具体 SQLite 仓储实现和 ConfigRegistry 的装配位置。
    领域层只依赖 Callable 目录访问器和仓储接口，不感知全局配置注册中心。
    """

    @staticmethod
    def create() -> LlmModelRegistryGatewayImpl:
        """创建 LLM 注册模块北向网关实例。

        配置通过访问器从 ConfigRegistry 实时读取，不存储配置副本。
        装配链路：SQLite 仓储 → 领域服务 → 北向网关。
        """
        from agent_runtime.config import ConfigRegistry

        repository = SqliteLlmModelRegistryRepository(
            registry_dir_accessor=(
                lambda: ConfigRegistry.instance().llm_model_registry.registry_dir
            ),
        )
        service = LlmModelRegistryService(repository)
        for metadata in builtin_llm_model_metadata():
            service.ensure_builtin_model_metadata(metadata)
        return LlmModelRegistryGatewayImpl(service)

    @staticmethod
    def create_image_generation_registry() -> LlmModelRegistryGatewayImpl:
        from agent_runtime.config import ConfigRegistry

        repository = SqliteLlmModelRegistryRepository(
            registry_dir_accessor=(
                lambda: ConfigRegistry.instance().llm_model_registry.registry_dir
            ),
            db_filename="image_generation_llm_registry.sqlite3",
        )
        service = LlmModelRegistryService(repository)
        for metadata in builtin_image_generation_model_metadata():
            service.ensure_builtin_model_metadata(metadata)
        return LlmModelRegistryGatewayImpl(service)

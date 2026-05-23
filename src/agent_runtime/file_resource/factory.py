"""统一文件资源服务工厂。

该模块用于快速创建可运行的统一文件资源服务实例，
把核心业务编排与依赖装配关系显式落到代码中。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_runtime.file_resource.config import FileResourceServiceConfig
from agent_runtime.file_resource.infrastructure import SqliteFileResourceArchiveRepository
from agent_runtime.file_resource.local_storage import LocalResourceStorage
from agent_runtime.file_resource.metadata_parser import FileResourceMetadataParser
from agent_runtime.file_resource.models import InMemoryFileResourceArchiveRepository
from agent_runtime.file_resource.settings_repository import ObjectStorageSettingsRepository
from agent_runtime.file_resource.service import FileResourceService
from agent_runtime.file_resource.south_gateway.object_storage.contracts import (
    ObjectStorageGateway,
)
from agent_runtime.file_resource.south_gateway.object_storage.in_memory import (
    InMemoryObjectStorageGateway,
)
from agent_runtime.file_resource.south_gateway.object_storage.runtime_configured import (
    RuntimeConfiguredObjectStorageGateway,
)


@dataclass(frozen=True, slots=True)
class FileResourceDevConfig:
    """统一文件资源服务开发环境配置。

    用于在没有真实数据库和 OSS 服务时，快速组装一个可运行的服务实例。
    """

    # 本地资源存储根目录。
    local_storage_root_path: str

    # 内存对象存储网关返回的公共访问地址前缀。
    object_storage_public_base_url: str

    # 单次批量处理上限。
    batch_limit: int = 20


class FileResourceServiceFactory:
    """统一文件资源服务工厂。"""

    @staticmethod
    def create_dev_service(
        config: FileResourceDevConfig,
    ) -> FileResourceService:
        """创建开发环境可运行的统一文件资源服务实例。

        使用内存档案仓储 + 本地文件存储 + 内存对象存储网关。
        """

        Path(config.local_storage_root_path).mkdir(parents=True, exist_ok=True)
        return FileResourceService(
            archive_repository=InMemoryFileResourceArchiveRepository(),
            local_storage=LocalResourceStorage(
                local_storage_root_path=config.local_storage_root_path,
            ),
            object_storage_gateway=InMemoryObjectStorageGateway(
                object_storage_public_base_url=config.object_storage_public_base_url,
            ),
            metadata_parser=FileResourceMetadataParser(),
            batch_limit=config.batch_limit,
        )

    @staticmethod
    def create_from_yaml_file(yaml_file_path: str) -> FileResourceService:
        """从 YAML 配置文件创建统一文件资源服务实例。

        会先加载 ConfigRegistry，再从全局单例读取配置。
        """
        from agent_runtime.config import ConfigRegistry

        ConfigRegistry.load(yaml_file_path)
        svc_config = ConfigRegistry.instance().file_resource_service
        return FileResourceServiceFactory.create_from_config(svc_config)

    @staticmethod
    def create_from_config(
        svc_config: FileResourceServiceConfig,
    ) -> FileResourceService:
        """从结构化配置创建统一文件资源服务实例。"""
        return FileResourceService(
            archive_repository=FileResourceServiceFactory._create_archive_repository(
                svc_config,
            ),
            local_storage=FileResourceServiceFactory._create_local_storage(svc_config),
            object_storage_gateway=FileResourceServiceFactory._create_object_storage_gateway(
                svc_config,
            ),
            metadata_parser=FileResourceMetadataParser(),
            batch_limit=svc_config.file_resource_batch_limit,
        )

    @staticmethod
    def _create_archive_repository(
        svc_config: FileResourceServiceConfig,
    ) -> SqliteFileResourceArchiveRepository:
        archive_data_dir = (
            Path(svc_config.local_resource_storage_root_path)
            / "archive"
        )
        return SqliteFileResourceArchiveRepository(archive_data_dir)

    @staticmethod
    def _create_local_storage(
        svc_config: FileResourceServiceConfig,
    ) -> LocalResourceStorage:
        """根据配置创建本地资源存储服务。"""

        Path(svc_config.local_resource_storage_root_path).mkdir(
            parents=True,
            exist_ok=True,
        )
        return LocalResourceStorage(
            local_storage_root_path=svc_config.local_resource_storage_root_path,
        )

    @staticmethod
    def _create_object_storage_gateway(
        svc_config: FileResourceServiceConfig,
    ) -> ObjectStorageGateway:
        settings_repository = ObjectStorageSettingsRepository(
            data_dir_accessor=lambda: svc_config.local_resource_storage_root_path,
        )
        return RuntimeConfiguredObjectStorageGateway(settings_repository)

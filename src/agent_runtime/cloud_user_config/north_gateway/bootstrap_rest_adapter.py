# -*- coding: utf-8 -*-
"""Runtime 初始化引导 REST 入口。"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_runtime.cloud_user_config.bootstrap_settings import (
    CloudUserConfigBootstrapSettingsRepository,
)
from agent_runtime.cloud_user_config.config import (
    CloudUserConfigDatabaseConfig,
    CloudUserConfigDatabasePoolConfig,
)
from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore
from agent_runtime.config import ConfigRegistry
from agent_runtime.file_resource.settings_repository import (
    DEFAULT_OBJECT_STORAGE_BUCKET_NAME,
    DEFAULT_OBJECT_STORAGE_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_OBJECT_STORAGE_ENDPOINT,
    DEFAULT_OBJECT_STORAGE_GATEWAY_PROVIDER,
    DEFAULT_OBJECT_STORAGE_OBJECT_KEY_PREFIX,
    DEFAULT_OBJECT_STORAGE_PUBLIC_BASE_URL,
    DEFAULT_OBJECT_STORAGE_REGION,
    create_object_storage_settings_repository,
)

logger = logging.getLogger(__name__)


class CloudUserConfigDatabaseBootstrapBody(BaseModel):
    enabled: bool = True
    backend: str = "postgresql"
    host: str
    port: int
    database: str
    username: str
    password: str
    sslmode: str = "require"
    connect_timeout_seconds: int = 10


class ObjectStorageBootstrapBody(BaseModel):
    gateway_provider: str = DEFAULT_OBJECT_STORAGE_GATEWAY_PROVIDER
    object_storage_endpoint: str = DEFAULT_OBJECT_STORAGE_ENDPOINT
    object_storage_region: str = DEFAULT_OBJECT_STORAGE_REGION
    object_storage_bucket_name: str = DEFAULT_OBJECT_STORAGE_BUCKET_NAME
    object_storage_public_base_url: str = DEFAULT_OBJECT_STORAGE_PUBLIC_BASE_URL
    object_storage_object_key_prefix: str = DEFAULT_OBJECT_STORAGE_OBJECT_KEY_PREFIX
    object_storage_access_key_id: str | None = None
    object_storage_access_key_secret: str | None = None
    connect_timeout_seconds: int = DEFAULT_OBJECT_STORAGE_CONNECT_TIMEOUT_SECONDS


class RuntimeBootstrapConfigureBody(BaseModel):
    cloud_user_config_database: CloudUserConfigDatabaseBootstrapBody | None = None
    object_storage_settings: ObjectStorageBootstrapBody | None = None


class RuntimeBootstrapCloudDatabaseStatus(BaseModel):
    configured: bool
    active_at_start: bool
    updated_at: str | None = None
    error_message: str | None = None


class RuntimeBootstrapObjectStorageStatus(BaseModel):
    configured: bool
    updated_at: str | None = None
    error_message: str | None = None


class RuntimeBootstrapStatusResponse(BaseModel):
    ready: bool
    bootstrap_required: bool
    restart_required: bool
    missing_required_items: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    cloud_user_config_database: RuntimeBootstrapCloudDatabaseStatus
    object_storage: RuntimeBootstrapObjectStorageStatus


def create_runtime_bootstrap_router(
    *,
    cloud_user_config_database_active_at_start: bool,
) -> APIRouter:
    router = APIRouter()

    @router.get("/status", response_model=RuntimeBootstrapStatusResponse)
    def get_runtime_bootstrap_status() -> RuntimeBootstrapStatusResponse:
        return _runtime_bootstrap_status(cloud_user_config_database_active_at_start)

    @router.post("/configure", response_model=RuntimeBootstrapStatusResponse)
    def configure_runtime_bootstrap(
        body: RuntimeBootstrapConfigureBody,
    ) -> RuntimeBootstrapStatusResponse:
        if body.cloud_user_config_database is not None:
            candidate = _database_config_from_body(body.cloud_user_config_database)
            if not candidate.is_postgresql_enabled:
                raise HTTPException(status_code=400, detail="云端用户配置 PostgreSQL 连接参数不完整。")
            try:
                PostgresUserConfigStore(lambda: candidate).ensure_schema()
            except Exception as exc:  # noqa: BLE001 - 需要把连接失败反馈给初始化 UI
                logger.warning("云端用户配置 PostgreSQL 连接校验失败: %s", exc, exc_info=True)
                raise HTTPException(status_code=400, detail=f"云端 PostgreSQL 连接失败：{exc}") from exc

            _cloud_bootstrap_repository().save_config(candidate)
        elif not _cloud_database_configured():
            raise HTTPException(status_code=400, detail="请先完成云端用户配置 PostgreSQL 初始化。")

        if body.object_storage_settings is not None:
            _save_object_storage_settings(body.object_storage_settings)

        return _runtime_bootstrap_status(cloud_user_config_database_active_at_start)

    return router


def _runtime_bootstrap_status(
    cloud_user_config_database_active_at_start: bool,
) -> RuntimeBootstrapStatusResponse:
    cloud_config = ConfigRegistry.instance().cloud_user_config_database
    cloud_configured = _cloud_database_configured(cloud_config)
    restart_required = cloud_configured and not cloud_user_config_database_active_at_start
    missing_required_items: list[str] = []
    warnings: list[str] = []
    if not cloud_configured:
        missing_required_items.append("cloud_user_config_database")
    if restart_required:
        warnings.append("云端用户配置库连接凭据已保存；需要重启 Runtime 后共享数据仓储才会切换到 PostgreSQL。")

    object_status = _object_storage_status()
    if cloud_configured and not object_status.configured:
        warnings.append("OSS 对象存储尚未配置；文件上传和对象存储能力会在使用时提示先配置。")

    ready = cloud_configured and not restart_required
    return RuntimeBootstrapStatusResponse(
        ready=ready,
        bootstrap_required=not cloud_configured,
        restart_required=restart_required,
        missing_required_items=missing_required_items,
        warnings=warnings,
        cloud_user_config_database=_cloud_database_status(
            cloud_config,
            cloud_user_config_database_active_at_start,
        ),
        object_storage=object_status,
    )


def _cloud_database_status(
    cloud_config: CloudUserConfigDatabaseConfig,
    active_at_start: bool,
) -> RuntimeBootstrapCloudDatabaseStatus:
    repository = _cloud_bootstrap_repository()
    settings = repository.get_settings()
    return RuntimeBootstrapCloudDatabaseStatus(
        configured=_cloud_database_configured(cloud_config),
        active_at_start=active_at_start,
        updated_at=settings.updated_at if settings is not None else None,
        error_message=None,
    )


def _cloud_database_configured(cloud_config: CloudUserConfigDatabaseConfig | None = None) -> bool:
    if cloud_config is None:
        cloud_config = ConfigRegistry.instance().cloud_user_config_database
    if cloud_config.is_postgresql_enabled:
        return True
    try:
        settings = _cloud_bootstrap_repository().get_settings()
    except Exception as exc:  # noqa: BLE001 - 状态判断需要降级为未配置
        logger.warning("读取云端用户配置 bootstrap 状态失败: %s", exc, exc_info=True)
        return False
    return bool(settings and settings.configured)


def _object_storage_status() -> RuntimeBootstrapObjectStorageStatus:
    repository = create_object_storage_settings_repository(
        lambda: ConfigRegistry.instance().file_resource_service.local_resource_storage_root_path,
    )
    try:
        settings = repository.get_settings()
        return RuntimeBootstrapObjectStorageStatus(
            configured=settings.configured,
            updated_at=settings.updated_at,
        )
    except Exception as exc:  # noqa: BLE001 - 状态接口需要降级呈现配置错误
        logger.warning("读取对象存储设置状态失败: %s", exc, exc_info=True)
        return RuntimeBootstrapObjectStorageStatus(
            configured=False,
            error_message=str(exc),
        )


def _save_object_storage_settings(body: ObjectStorageBootstrapBody) -> None:
    repository = create_object_storage_settings_repository(
        lambda: ConfigRegistry.instance().file_resource_service.local_resource_storage_root_path,
    )
    repository.save_settings(
        gateway_provider=body.gateway_provider,
        object_storage_endpoint=body.object_storage_endpoint,
        object_storage_region=body.object_storage_region,
        object_storage_bucket_name=body.object_storage_bucket_name,
        object_storage_public_base_url=body.object_storage_public_base_url,
        object_storage_object_key_prefix=body.object_storage_object_key_prefix,
        object_storage_access_key_id=body.object_storage_access_key_id,
        object_storage_access_key_secret=body.object_storage_access_key_secret,
        connect_timeout_seconds=body.connect_timeout_seconds,
    )


def _database_config_from_body(
    body: CloudUserConfigDatabaseBootstrapBody,
) -> CloudUserConfigDatabaseConfig:
    base = ConfigRegistry.instance().cloud_user_config_database
    return CloudUserConfigDatabaseConfig(
        enabled=body.enabled,
        backend=body.backend.strip() or "postgresql",
        host=body.host.strip(),
        port=body.port,
        database=body.database.strip(),
        username=body.username.strip(),
        password=body.password.strip(),
        sslmode=body.sslmode.strip() or "require",
        bootstrap_data_dir=base.bootstrap_data_dir,
        pool=CloudUserConfigDatabasePoolConfig(
            min_size=base.pool.min_size,
            max_size=base.pool.max_size,
            connect_timeout_seconds=max(int(body.connect_timeout_seconds), 1),
        ),
    )


def _cloud_bootstrap_repository() -> CloudUserConfigBootstrapSettingsRepository:
    return CloudUserConfigBootstrapSettingsRepository(
        lambda: ConfigRegistry.instance().cloud_user_config_database.bootstrap_data_dir,
    )

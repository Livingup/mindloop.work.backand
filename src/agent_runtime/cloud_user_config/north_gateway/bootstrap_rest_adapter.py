# -*- coding: utf-8 -*-
"""Runtime 初始化引导 REST 入口。"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_runtime.cloud_user_config.bootstrap_settings import (
    CloudUserConfigBootstrapSettings,
    CloudUserConfigBootstrapSettingsRepository,
)
from agent_runtime.cloud_user_config.config import (
    CloudUserConfigDatabaseConfig,
    CloudUserConfigDatabasePoolConfig,
)
from agent_runtime.cloud_user_config.local_state import (
    CloudUserConfigLocalReadiness,
    CloudUserConfigLocalStateRepository,
    OBJECT_STORAGE_DECISION_CONFIGURED,
    OBJECT_STORAGE_DECISION_SKIPPED,
    atomic_commit_database_reconfigure,
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
    PostgresObjectStorageSettingsRepository,
)

logger = logging.getLogger(__name__)

# Settings 重配置 PUT 进程内串行，避免并发验证后的 interleaved durable 提交。
_DATABASE_SETTINGS_RECONFIGURE_LOCK = threading.RLock()


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
    object_storage_skipped: bool = False


class RuntimeBootstrapCloudDatabaseStatus(BaseModel):
    configured: bool
    active_at_start: bool
    shared_config_snapshot_ready: bool = False
    completed: bool = False
    updated_at: str | None = None
    snapshot_updated_at: str | None = None
    error_message: str | None = None


class RuntimeBootstrapObjectStorageStatus(BaseModel):
    configured: bool
    skipped: bool = False
    configured_or_skipped: bool = False
    updated_at: str | None = None
    error_message: str | None = None


class RuntimeBootstrapStatusResponse(BaseModel):
    ready: bool
    bootstrap_required: bool
    restricted: bool = True
    repair_required: bool = False
    restart_required: bool = False
    missing_required_items: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    cloud_user_config_database: RuntimeBootstrapCloudDatabaseStatus
    object_storage: RuntimeBootstrapObjectStorageStatus


class CloudUserConfigDatabaseSettingsUpdateBody(BaseModel):
    """Settings 页重配置 PostgreSQL 连接的请求体。

    password 为空/省略时保留已保存密码；仅在首次配置或主动覆盖时提供。
    """

    enabled: bool = True
    backend: str = "postgresql"
    host: str
    port: int
    database: str
    username: str
    password: str | None = None
    sslmode: str = "require"
    connect_timeout_seconds: int = 10


class CloudUserConfigDatabaseSettingsResponse(BaseModel):
    """Settings 页可安全回显的 PostgreSQL 连接状态（不含密码）。"""

    configured: bool
    enabled: bool = False
    backend: str = "local"
    host: str = ""
    port: int = 5432
    database: str = ""
    username: str = ""
    sslmode: str = "require"
    connect_timeout_seconds: int = 10
    password_configured: bool = False
    updated_at: str | None = None
    bootstrap_db_path: str = ""
    shared_config_snapshot_ready: bool = False
    snapshot_updated_at: str | None = None
    snapshot_revision: int | None = None
    active_at_start: bool = False
    effective_target_matches_saved: bool = False
    restart_required: bool = False
    warnings: list[str] = Field(default_factory=list)


def create_runtime_bootstrap_router(
    *,
    cloud_user_config_database_active_at_start: bool,
    active_database_config_key_at_start: tuple[object, ...] | None = None,
    on_bootstrap_ready: Callable[[], None] | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/status", response_model=RuntimeBootstrapStatusResponse)
    def get_runtime_bootstrap_status() -> RuntimeBootstrapStatusResponse:
        return _runtime_bootstrap_status(
            cloud_user_config_database_active_at_start,
            active_database_config_key_at_start=active_database_config_key_at_start,
        )

    @router.get("/database-settings", response_model=CloudUserConfigDatabaseSettingsResponse)
    def get_cloud_user_config_database_settings() -> CloudUserConfigDatabaseSettingsResponse:
        return _database_settings_response(
            cloud_user_config_database_active_at_start,
            active_database_config_key_at_start=active_database_config_key_at_start,
        )

    @router.put("/database-settings", response_model=CloudUserConfigDatabaseSettingsResponse)
    def update_cloud_user_config_database_settings(
        body: CloudUserConfigDatabaseSettingsUpdateBody,
    ) -> CloudUserConfigDatabaseSettingsResponse:
        return _update_database_settings(
            body,
            cloud_user_config_database_active_at_start=cloud_user_config_database_active_at_start,
            active_database_config_key_at_start=active_database_config_key_at_start,
        )

    @router.post("/configure", response_model=RuntimeBootstrapStatusResponse)
    def configure_runtime_bootstrap(
        body: RuntimeBootstrapConfigureBody,
    ) -> RuntimeBootstrapStatusResponse:
        if body.cloud_user_config_database is not None:
            candidate = _database_config_from_body(body.cloud_user_config_database)
            if not candidate.is_postgresql_enabled:
                raise HTTPException(status_code=400, detail="云端用户配置 PostgreSQL 连接参数不完整。")
            try:
                PostgresUserConfigStore(lambda: candidate).reload_cache_from_database()
            except Exception as exc:  # noqa: BLE001 - 需要把连接失败反馈给初始化 UI
                logger.warning("云端用户配置 PostgreSQL 初始快照拉取失败: %s", exc, exc_info=True)
                raise HTTPException(status_code=400, detail=f"云端 PostgreSQL 连接或初始快照拉取失败：{exc}") from exc

            _cloud_bootstrap_repository().save_config(candidate)
            _local_state_repository().reset_object_storage_decision()
        elif not _cloud_database_configured():
            raise HTTPException(status_code=400, detail="请先完成云端用户配置 PostgreSQL 初始化。")

        if body.object_storage_settings is not None and body.object_storage_skipped:
            raise HTTPException(status_code=400, detail="OSS 配置和跳过 OSS 只能二选一。")
        if body.object_storage_settings is not None:
            _save_object_storage_settings(body.object_storage_settings)
            _local_state_repository().mark_object_storage_configured()
        elif body.object_storage_skipped:
            _local_state_repository().mark_object_storage_skipped()

        status = _runtime_bootstrap_status(
            cloud_user_config_database_active_at_start,
            active_database_config_key_at_start=active_database_config_key_at_start,
        )
        if status.ready and on_bootstrap_ready is not None:
            try:
                on_bootstrap_ready()
            except Exception as exc:  # noqa: BLE001 - 需要让初始化 UI 感知同进程激活失败
                logger.exception("Runtime bootstrap 已完成，但当前进程完整模式激活失败。")
                raise HTTPException(status_code=500, detail=f"Runtime 完整模式激活失败：{exc}") from exc
        return status

    return router


def _update_database_settings(
    body: CloudUserConfigDatabaseSettingsUpdateBody,
    *,
    cloud_user_config_database_active_at_start: bool,
    active_database_config_key_at_start: tuple[object, ...] | None,
) -> CloudUserConfigDatabaseSettingsResponse:
    with _DATABASE_SETTINGS_RECONFIGURE_LOCK:
        return _update_database_settings_locked(
            body,
            cloud_user_config_database_active_at_start=cloud_user_config_database_active_at_start,
            active_database_config_key_at_start=active_database_config_key_at_start,
        )


def _update_database_settings_locked(
    body: CloudUserConfigDatabaseSettingsUpdateBody,
    *,
    cloud_user_config_database_active_at_start: bool,
    active_database_config_key_at_start: tuple[object, ...] | None,
) -> CloudUserConfigDatabaseSettingsResponse:
    repository = _cloud_bootstrap_repository()
    existing = repository.get_settings()
    candidate = _database_config_from_update_body(body, existing=existing)
    if not candidate.is_postgresql_enabled:
        raise HTTPException(status_code=400, detail="用户通用数据 PostgreSQL 连接参数不完整。")

    old_key = (
        _cloud_user_config_database_key(_cloud_database_config_from_settings(existing))
        if existing is not None and existing.configured
        else None
    )
    new_key = _cloud_user_config_database_key(candidate)

    candidate_store = PostgresUserConfigStore(lambda: candidate)
    try:
        # 候选验证必须无进程 cache / 本机 snapshot 副作用；当前进程仍应继续服务旧目标。
        snapshot = candidate_store.read_authoritative_snapshot_without_process_side_effects()
    except Exception as exc:  # noqa: BLE001 - 连接/快照失败时不得覆盖旧凭据
        logger.warning(
            "用户通用数据 PostgreSQL 连接或快照拉取失败 host=%s port=%s database=%s username=%s sslmode=%s: %s",
            candidate.host,
            candidate.port,
            candidate.database,
            candidate.username,
            candidate.sslmode,
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=400,
            detail=f"PostgreSQL 连接或共享配置快照拉取失败：{exc}",
        ) from exc

    oss_decision = _resolve_object_storage_decision_for_reconfigure(
        config_key_changed=old_key != new_key,
        snapshot_items=snapshot.items,
    )
    try:
        atomic_commit_database_reconfigure(
            candidate.bootstrap_data_dir,
            enabled=candidate.enabled,
            backend=candidate.backend,
            host=candidate.host,
            port=candidate.port,
            database=candidate.database,
            username=candidate.username,
            password=candidate.password,
            sslmode=candidate.sslmode,
            pool_min_size=candidate.pool.min_size,
            pool_max_size=candidate.pool.max_size,
            connect_timeout_seconds=candidate.pool.connect_timeout_seconds,
            config_key=new_key,
            revision=snapshot.revision,
            items=snapshot.items,
            object_storage_decision=oss_decision,
        )
    except Exception as exc:  # noqa: BLE001 - durable 原子提交失败时不得留下半写状态
        logger.exception(
            "用户通用数据 PostgreSQL 本机 durable 原子提交失败 host=%s port=%s database=%s",
            candidate.host,
            candidate.port,
            candidate.database,
        )
        raise HTTPException(
            status_code=500,
            detail=f"本机 PostgreSQL 连接配置保存失败，已保持旧配置不变：{exc}",
        ) from exc

    response = _database_settings_response(
        cloud_user_config_database_active_at_start,
        active_database_config_key_at_start=active_database_config_key_at_start,
        snapshot_revision=snapshot.revision,
    )
    logger.info(
        "用户通用数据 PostgreSQL 连接配置已保存 host=%s port=%s database=%s username=%s sslmode=%s revision=%s restart_required=%s",
        candidate.host,
        candidate.port,
        candidate.database,
        candidate.username,
        candidate.sslmode,
        snapshot.revision,
        response.restart_required,
    )
    return response


def _database_settings_response(
    cloud_user_config_database_active_at_start: bool,
    *,
    active_database_config_key_at_start: tuple[object, ...] | None = None,
    snapshot_revision: int | None = None,
) -> CloudUserConfigDatabaseSettingsResponse:
    repository = _cloud_bootstrap_repository()
    settings = repository.get_settings()
    bootstrap_db_path = str(repository.db_path())
    if settings is None or not settings.configured:
        return CloudUserConfigDatabaseSettingsResponse(
            configured=False,
            enabled=False,
            backend="local",
            bootstrap_db_path=bootstrap_db_path,
            active_at_start=cloud_user_config_database_active_at_start,
            effective_target_matches_saved=False,
            restart_required=cloud_user_config_database_active_at_start,
            warnings=["本机尚未保存用户通用数据 PostgreSQL 连接配置。"],
        )

    cloud_config = _cloud_database_config_from_settings(settings)
    readiness = _local_readiness(settings, cloud_config)
    saved_key = _cloud_user_config_database_key(cloud_config)
    effective_target_matches_saved = (
        cloud_user_config_database_active_at_start
        and active_database_config_key_at_start is not None
        and active_database_config_key_at_start == saved_key
    )
    warnings: list[str] = []
    if readiness.error_message:
        warnings.append(f"本机 bootstrap 状态读取失败：{readiness.error_message}")
    if readiness.credentials_saved and not readiness.shared_config_snapshot_ready:
        warnings.append("本机共享配置快照缺失、不匹配或损坏；请重新保存 PostgreSQL 连接以拉取权威快照。")
    if not readiness.object_storage_configured_or_skipped:
        warnings.append("对象存储尚未配置或跳过；可在下方对象存储卡片中处理，不会自动进入首次引导。")

    revision = snapshot_revision
    if revision is None:
        snapshot = _local_state_repository().get_snapshot(expected_config_key=saved_key)
        revision = snapshot.revision if snapshot is not None else None

    restart_required = not effective_target_matches_saved
    if restart_required:
        warnings.append("连接配置已保存到本机。请重启 Runtime 后生效；当前进程仍可能使用旧数据库目标。")

    return CloudUserConfigDatabaseSettingsResponse(
        configured=True,
        enabled=settings.enabled,
        backend=settings.backend,
        host=settings.host,
        port=settings.port,
        database=settings.database,
        username=settings.username,
        sslmode=settings.sslmode,
        connect_timeout_seconds=settings.pool.connect_timeout_seconds,
        password_configured=bool((settings.password or "").strip()),
        updated_at=settings.updated_at,
        bootstrap_db_path=bootstrap_db_path,
        shared_config_snapshot_ready=readiness.shared_config_snapshot_ready,
        snapshot_updated_at=readiness.snapshot_updated_at,
        snapshot_revision=revision,
        active_at_start=cloud_user_config_database_active_at_start,
        effective_target_matches_saved=effective_target_matches_saved,
        restart_required=restart_required,
        warnings=warnings,
    )


def _resolve_object_storage_decision_for_reconfigure(
    *,
    config_key_changed: bool,
    snapshot_items: dict[tuple[str, str], dict[str, object]],
) -> str | None:
    """计算 Settings 重配置应写入的 OSS 决策；None 表示事务中不改写该字段。

    禁止 first-run 式无条件 reset；仅在 config_key 变化且新目标事实需要时写入。
    """

    if not config_key_changed:
        return None

    payload = snapshot_items.get(("file_resource.object_storage_settings", "default"))
    oss_configured = False
    if isinstance(payload, dict):
        oss_configured = all((
            str(payload.get("gateway_provider") or "").strip(),
            str(payload.get("object_storage_endpoint") or "").strip(),
            str(payload.get("object_storage_region") or "").strip(),
            str(payload.get("object_storage_bucket_name") or "").strip(),
            str(payload.get("object_storage_public_base_url") or "").strip(),
            str(payload.get("object_storage_access_key_id") or "").strip(),
            str(payload.get("object_storage_access_key_secret") or "").strip(),
        ))
    if oss_configured:
        return OBJECT_STORAGE_DECISION_CONFIGURED

    current = _local_state_repository().get_state()
    if current.object_storage_decision == OBJECT_STORAGE_DECISION_SKIPPED:
        return OBJECT_STORAGE_DECISION_SKIPPED
    # 已配置或 pending：不在事务中改写，避免无条件 reset 把用户踢回 first-run。
    return None


def _database_config_from_update_body(
    body: CloudUserConfigDatabaseSettingsUpdateBody,
    *,
    existing: CloudUserConfigBootstrapSettings | None,
) -> CloudUserConfigDatabaseConfig:
    base = _cloud_database_base_config()
    requested_password = (body.password or "").strip()
    if requested_password:
        password = requested_password
    elif existing is not None and (existing.password or "").strip():
        password = (existing.password or "").strip()
    else:
        raise HTTPException(status_code=400, detail="请提供 PostgreSQL 密码；首次配置不能留空。")

    backend = body.backend.strip() or "postgresql"
    if backend != "postgresql":
        raise HTTPException(status_code=400, detail="当前仅支持 backend=postgresql。")

    return CloudUserConfigDatabaseConfig(
        enabled=body.enabled,
        backend=backend,
        host=body.host.strip(),
        port=int(body.port),
        database=body.database.strip(),
        username=body.username.strip(),
        password=password,
        sslmode=body.sslmode.strip() or "require",
        bootstrap_data_dir=base.bootstrap_data_dir,
        pool=CloudUserConfigDatabasePoolConfig(
            min_size=base.pool.min_size,
            max_size=base.pool.max_size,
            connect_timeout_seconds=max(int(body.connect_timeout_seconds), 1),
        ),
    )


def _runtime_bootstrap_status(
    cloud_user_config_database_active_at_start: bool,
    *,
    active_database_config_key_at_start: tuple[object, ...] | None = None,
) -> RuntimeBootstrapStatusResponse:
    settings = _cloud_bootstrap_repository().get_settings()
    cloud_config = _cloud_database_config_from_settings(settings) if settings and settings.configured else None
    readiness = _local_readiness(settings, cloud_config)
    missing_required_items: list[str] = []
    warnings: list[str] = []

    if not readiness.credentials_saved:
        missing_required_items.append("cloud_user_config_database")
    if readiness.credentials_saved and not readiness.shared_config_snapshot_ready:
        missing_required_items.append("shared_config_snapshot")
        warnings.append("本机共享配置快照缺失、不匹配或损坏；Runtime 将保持限制模式，需重新拉取 PostgreSQL 权威快照。")
    if not readiness.object_storage_configured_or_skipped:
        missing_required_items.append("object_storage_decision")
    if readiness.error_message:
        warnings.append(f"本机 bootstrap 状态读取失败：{readiness.error_message}")

    object_status = _object_storage_status(
        cloud_config=cloud_config,
        readiness=readiness,
    )
    ready = readiness.completed
    return RuntimeBootstrapStatusResponse(
        ready=ready,
        bootstrap_required=not ready,
        restricted=not ready,
        repair_required=readiness.credentials_saved and not readiness.shared_config_snapshot_ready,
        restart_required=False,
        missing_required_items=missing_required_items,
        warnings=warnings,
        cloud_user_config_database=RuntimeBootstrapCloudDatabaseStatus(
            configured=readiness.credentials_saved,
            active_at_start=cloud_user_config_database_active_at_start,
            shared_config_snapshot_ready=readiness.shared_config_snapshot_ready,
            completed=readiness.completed,
            updated_at=settings.updated_at if settings is not None else None,
            snapshot_updated_at=readiness.snapshot_updated_at,
            error_message=readiness.error_message,
        ),
        object_storage=object_status,
    )


def _cloud_database_configured() -> bool:
    try:
        settings = _cloud_bootstrap_repository().get_settings()
    except Exception as exc:  # noqa: BLE001 - 状态判断需要降级为未配置
        logger.warning("读取云端用户配置 bootstrap 状态失败: %s", exc, exc_info=True)
        return False
    return bool(settings and settings.configured)


def _object_storage_status(
    *,
    cloud_config: CloudUserConfigDatabaseConfig | None,
    readiness: CloudUserConfigLocalReadiness,
) -> RuntimeBootstrapObjectStorageStatus:
    skipped = readiness.object_storage_decision == OBJECT_STORAGE_DECISION_SKIPPED
    if cloud_config is None or not readiness.shared_config_snapshot_ready:
        return RuntimeBootstrapObjectStorageStatus(
            configured=False,
            skipped=skipped,
            configured_or_skipped=readiness.object_storage_configured_or_skipped,
            error_message=readiness.error_message,
        )
    try:
        store = PostgresUserConfigStore(lambda: cloud_config)
        settings = PostgresObjectStorageSettingsRepository(lambda: store).get_settings()
        return RuntimeBootstrapObjectStorageStatus(
            configured=settings.configured,
            skipped=skipped,
            configured_or_skipped=readiness.object_storage_configured_or_skipped,
            updated_at=settings.updated_at,
        )
    except Exception as exc:  # noqa: BLE001 - 状态接口需要降级呈现配置错误
        logger.warning("读取对象存储设置状态失败: %s", exc, exc_info=True)
        return RuntimeBootstrapObjectStorageStatus(
            configured=False,
            skipped=skipped,
            configured_or_skipped=readiness.object_storage_configured_or_skipped,
            error_message=str(exc),
        )


def _save_object_storage_settings(body: ObjectStorageBootstrapBody) -> None:
    cloud_config = _required_saved_cloud_database_config()
    store = PostgresUserConfigStore(lambda: cloud_config)
    repository = PostgresObjectStorageSettingsRepository(lambda: store)
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


def _required_saved_cloud_database_config() -> CloudUserConfigDatabaseConfig:
    settings = _cloud_bootstrap_repository().get_settings()
    if settings is None or not settings.configured:
        raise HTTPException(status_code=400, detail="请先完成云端用户配置 PostgreSQL 初始化。")
    return _cloud_database_config_from_settings(settings)


def _cloud_database_config_from_settings(
    settings: CloudUserConfigBootstrapSettings,
) -> CloudUserConfigDatabaseConfig:
    return settings.to_database_config(_cloud_database_base_config())


def _local_readiness(
    settings: CloudUserConfigBootstrapSettings | None,
    cloud_config: CloudUserConfigDatabaseConfig | None,
) -> CloudUserConfigLocalReadiness:
    return _local_state_repository().get_readiness(
        credentials_saved=bool(settings and settings.configured),
        expected_config_key=_cloud_user_config_database_key(cloud_config) if cloud_config is not None else None,
    )


def _database_config_from_body(
    body: CloudUserConfigDatabaseBootstrapBody,
) -> CloudUserConfigDatabaseConfig:
    base = _cloud_database_base_config()
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


def _cloud_database_base_config() -> CloudUserConfigDatabaseConfig:
    cloud_config = ConfigRegistry.instance().cloud_user_config_database
    if cloud_config.is_postgresql_enabled:
        return CloudUserConfigDatabaseConfig(
            bootstrap_data_dir=cloud_config.bootstrap_data_dir,
            pool=cloud_config.pool,
        )
    return cloud_config


def _cloud_user_config_database_key(config: CloudUserConfigDatabaseConfig) -> tuple[object, ...]:
    return (
        config.host,
        config.port,
        config.database,
        config.username,
        config.sslmode,
    )


def _cloud_bootstrap_repository() -> CloudUserConfigBootstrapSettingsRepository:
    return CloudUserConfigBootstrapSettingsRepository(
        lambda: _cloud_database_base_config().bootstrap_data_dir,
    )


def _local_state_repository() -> CloudUserConfigLocalStateRepository:
    return CloudUserConfigLocalStateRepository(
        lambda: _cloud_database_base_config().bootstrap_data_dir,
    )

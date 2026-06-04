# -*- coding: utf-8 -*-
"""用户共享配置中心增量同步 REST 入口。"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from agent_runtime.cloud_user_config.postgres_store import (
    CloudUserConfigChangeSet,
    PostgresUserConfigStore,
    is_versioned_config_namespace,
    versioned_config_namespaces,
)
from agent_runtime.config import ConfigRegistry

CONFIG_CENTER_POLL_INTERVAL_SECONDS = 5


class ConfigCenterChangeResponse(BaseModel):
    namespace: str
    item_id: str
    revision: int
    deleted: bool
    payload: dict[str, Any] | None = None
    updated_at: str | None = None


class ConfigCenterChangesResponse(BaseModel):
    current_revision: int
    changes: list[ConfigCenterChangeResponse]
    versioned_namespaces: list[str]
    poll_interval_seconds: int = Field(default=CONFIG_CENTER_POLL_INTERVAL_SECONDS, ge=1)


def create_config_center_router(
    *,
    store_accessor: Callable[[], PostgresUserConfigStore] | None = None,
    poll_interval_seconds: int = CONFIG_CENTER_POLL_INTERVAL_SECONDS,
    versioned_namespaces_accessor: Callable[[], tuple[str, ...]] = versioned_config_namespaces,
    namespace_predicate: Callable[[str], bool] = is_versioned_config_namespace,
) -> APIRouter:
    """创建轻量配置中心轮询路由。

    默认直接使用当前 Runtime bootstrap 后的 PostgreSQL cloud_user_config_items；测试可注入
    store_accessor。该路由只暴露用户共享配置 namespace 的 revision 变化，不承载会话、
    任务、文件资源归档等业务/运行数据。
    """

    router = APIRouter()

    def _store() -> PostgresUserConfigStore:
        if store_accessor is not None:
            return store_accessor()
        config = ConfigRegistry.instance().cloud_user_config_database
        if not config.is_postgresql_enabled:
            raise HTTPException(
                status_code=503,
                detail="Runtime 尚未完成云端用户共享配置库初始化，配置中心增量同步不可用。",
            )
        return PostgresUserConfigStore(lambda: ConfigRegistry.instance().cloud_user_config_database)

    @router.get("/changes", response_model=ConfigCenterChangesResponse)
    def get_changes(
        since: int = Query(
            0,
            ge=0,
            description="客户端最后已看到的用户共享配置 revision。",
        ),
    ) -> ConfigCenterChangesResponse:
        changes = _store().list_config_changes_since(since)
        return _changes_response(
            changes,
            poll_interval_seconds=poll_interval_seconds,
            versioned_namespaces_accessor=versioned_namespaces_accessor,
            namespace_predicate=namespace_predicate,
        )

    return router


def _changes_response(
    changes: CloudUserConfigChangeSet,
    *,
    poll_interval_seconds: int,
    versioned_namespaces_accessor: Callable[[], tuple[str, ...]],
    namespace_predicate: Callable[[str], bool],
) -> ConfigCenterChangesResponse:
    response_changes = [
        ConfigCenterChangeResponse(
            namespace=change.namespace,
            item_id=change.item_id,
            revision=change.revision,
            deleted=change.deleted,
            payload=change.payload,
            updated_at=change.updated_at,
        )
        for change in changes.changes
        if namespace_predicate(change.namespace)
    ]
    return ConfigCenterChangesResponse(
        current_revision=changes.current_revision,
        changes=response_changes,
        versioned_namespaces=list(versioned_namespaces_accessor()),
        poll_interval_seconds=max(int(poll_interval_seconds), 1),
    )

# -*- coding: utf-8 -*-
"""云端用户全局配置 PostgreSQL 连接配置。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class CloudUserConfigDatabasePoolConfig:
    """云端用户配置数据库连接池/连接参数。

    当前仓储实现按操作短连接访问 PostgreSQL，min/max 保留为程序元数据，
    便于后续切换为真实连接池时不改变配置契约。
    """

    min_size: int = 1
    max_size: int = 5
    connect_timeout_seconds: int = 10


@dataclass(frozen=True, slots=True)
class CloudUserConfigDatabaseConfig:
    """用户全局可复用配置的云端 PostgreSQL 数据库配置。

    该配置属于程序部署元数据，不属于用户可同步设置；只有 password 使用环境变量占位符。
    """

    enabled: bool = False
    backend: str = "local"
    host: str = "db.mindloop.chat"
    port: int = 25432
    database: str = "agent_runtime"
    username: str = "agent_runtime_client"
    password: str = ""
    sslmode: str = "require"
    pool: CloudUserConfigDatabasePoolConfig = field(
        default_factory=CloudUserConfigDatabasePoolConfig,
    )

    @property
    def is_postgresql_enabled(self) -> bool:
        return self.enabled and self.backend == "postgresql"

    def require_password(self) -> str:
        if not self.password:
            raise ValueError(
                "cloud_user_config_database.password 未配置；请设置 "
                "SPREAD_AI_CLOUD_USER_CONFIG_DB_PASSWORD。"
            )
        return self.password

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
    """用户全局可复用配置的云端 PostgreSQL 数据库连接配置。

    YAML 中只保留默认连接元数据；真正的启动凭据由每台机器本地 bootstrap
    存储覆盖。只有连接参数和 password 都完整时，云端 PostgreSQL 才会成为
    prompt、Agent 定义、LLM Registry、OSS 设置等用户共享数据的权威仓储。
    """

    enabled: bool = False
    backend: str = "local"
    host: str = "db.mindloop.chat"
    port: int = 25432
    database: str = "agent_runtime"
    username: str = "agent_runtime_client"
    password: str = ""
    sslmode: str = "require"
    bootstrap_data_dir: str = "~/.spread-ai/cloud-user-config"
    pool: CloudUserConfigDatabasePoolConfig = field(
        default_factory=CloudUserConfigDatabasePoolConfig,
    )

    @property
    def is_postgresql_enabled(self) -> bool:
        return all((
            self.enabled,
            self.backend == "postgresql",
            self.host.strip(),
            self.port > 0,
            self.database.strip(),
            self.username.strip(),
            self.password.strip(),
        ))

    def require_password(self) -> str:
        if not self.password:
            raise ValueError(
                "cloud_user_config_database.password 未配置；请先完成云端用户配置库初始化引导。"
            )
        return self.password

"""统一文件资源服务业务包。

该包用于承载通用 Agent 运行时环境中的统一文件资源管理基础服务。

架构分层：
- north_gateway/: 北向网关 — 按业务域定义对外服务契约（创建 / 访问 / 删除）
- south_gateway/: 南向网关 — 按业务域定义外部依赖契约（对象存储）
- 内部扁平模块: models / service / metadata_parser / local_storage / config / factory
"""
from agent_runtime.file_resource.factory import (
    FileResourceDevConfig,
    FileResourceServiceFactory,
)
from agent_runtime.file_resource.service import FileResourceService

__all__ = [
    "FileResourceService",
    "FileResourceServiceFactory",
    "FileResourceDevConfig",
]

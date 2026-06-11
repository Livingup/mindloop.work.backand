"""统一文件资源服务北向网关业务包。

该包按业务域组织统一文件资源服务对外提供的全部北向契约：
- resource_create_contracts: 资源创建（从本地文件 / Base64 / 远程 URL 创建）
- resource_access_contracts: 资源访问（按 ID 批量获取）
- resource_delete_contracts: 资源删除（按 ID 批量删除）
"""
from agent_runtime.file_resource.north_gateway.resource_access_contracts import (
    FileResourceAccessNorthGateway,
    FileResourceAccessResult,
)
from agent_runtime.file_resource.north_gateway.resource_create_contracts import (
    Base64CreateRequest,
    FileResourceCreateNorthGateway,
    FileResourceCreateResult,
    LocalFileCreateRequest,
    RemoteUrlCreateRequest,
)
from agent_runtime.file_resource.north_gateway.resource_delete_contracts import (
    FileResourceDeleteNorthGateway,
)

__all__ = [
    "LocalFileCreateRequest",
    "Base64CreateRequest",
    "RemoteUrlCreateRequest",
    "FileResourceCreateResult",
    "FileResourceCreateNorthGateway",
    "FileResourceAccessResult",
    "FileResourceAccessNorthGateway",
    "FileResourceDeleteNorthGateway",
]

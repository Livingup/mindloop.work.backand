"""文件资源解析南向网关业务包。"""
from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import (
    ResolvedResource,
    ResourceMeta,
    ResourceResolver,
    resolve_resource_access,
)

__all__ = [
    "ResolvedResource",
    "ResourceMeta",
    "ResourceResolver",
    "resolve_resource_access",
]

"""统一 LLM 消息服务南向网关业务包。

该包按业务域组织统一 LLM 消息服务依赖的外部能力契约：
- resource_resolver/: 文件资源解析（依赖统一文件资源服务）
"""
from agent_runtime.uni_llm.south_gateway.resource_resolver.contracts import (
    ResourceMeta,
    ResourceResolver,
)

__all__ = [
    "ResourceMeta",
    "ResourceResolver",
]

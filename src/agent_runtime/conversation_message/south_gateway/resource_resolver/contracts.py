"""文件资源解析南向网关接口契约。

该模块定义统一 LLM 消息服务面向统一文件资源服务时的稳定协作契约。
渲染消息时需要将 resourceId 转换为目标平台所需的格式（base64 / URL），
该转换能力由 ResourceResolver 接口抽象，具体实现由南向适配器提供。

任何具体文件资源服务实现都必须在该接口后面完成适配，
避免把文件资源服务的内部概念直接带入消息渲染流程。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ResourceMeta:
    """文件资源元数据。

    该模型用于表达渲染消息时从文件资源服务获取的资源基本属性，
    包括 MIME 类型和资源类别，供各协议适配器构建目标平台消息体。
    """

    # 资源 MIME 类型（如 "image/jpeg"、"application/pdf"）。
    mime_type: str

    # 资源类别（"image" / "pdf"）。
    resource_category: str

    size_in_bytes: int | None = None
    image_width: int | None = None
    image_height: int | None = None
    pdf_page_count: int | None = None


@dataclass(frozen=True, slots=True)
class ResolvedResource:
    resource_id: str
    mime_type: str
    resource_category: str
    object_storage_url: str | None
    base64_content: str | None
    local_storage_path: str | None
    size_in_bytes: int | None = None
    image_width: int | None = None
    image_height: int | None = None
    pdf_page_count: int | None = None


class ResourceResolver(Protocol):
    """文件资源解析南向网关接口。

    该接口定义统一 LLM 消息服务面向统一文件资源服务时的稳定协作契约。
    渲染消息时各协议适配器通过该接口按需获取资源的 base64 内容和元数据。
    """

    async def get_base64(self, resource_id: str) -> str:
        """按资源标识获取 Base64 编码的资源内容。

        渲染消息时，各协议适配器调用此方法将 resourceId 转为
        目标平台所需的 base64 内容（如 Claude 的 inline base64、
        OpenAI 的 data URI）。
        """
        ...

    async def get_public_url(self, resource_id: str) -> str | None:
        ...

    async def get_resource_meta(self, resource_id: str) -> ResourceMeta:
        """按资源标识获取资源元数据。

        渲染消息时，各协议适配器需要知道资源的 MIME 类型
        以构建正确的目标平台消息体。
        """
        ...

    async def resolve_resource(self, resource_id: str) -> ResolvedResource:
        ...


async def resolve_resource_access(
    resolver: ResourceResolver,
    resource_id: str,
) -> ResolvedResource:
    resolve_resource = getattr(resolver, "resolve_resource", None)
    if resolve_resource is not None:
        return await resolve_resource(resource_id)

    meta = await resolver.get_resource_meta(resource_id)
    get_public_url = getattr(resolver, "get_public_url", None)
    public_url = await get_public_url(resource_id) if get_public_url is not None else None
    return ResolvedResource(
        resource_id=resource_id,
        mime_type=meta.mime_type,
        resource_category=meta.resource_category,
        object_storage_url=public_url,
        base64_content=await resolver.get_base64(resource_id),
        local_storage_path=None,
        size_in_bytes=meta.size_in_bytes,
        image_width=meta.image_width,
        image_height=meta.image_height,
        pdf_page_count=meta.pdf_page_count,
    )

"""文件资源解析南向适配器 —— 桥接统一文件资源服务。

该适配器实现 ResourceResolver 接口，内部委托统一文件资源服务
（FileResourceService）完成资源 Base64 内容读取和元数据获取。

这是生产环境和开发环境通用的适配实现：
只要注入的 FileResourceService 实例不同（真实仓储 vs 内存仓储），
即可在不同环境下运行，无需修改本适配器代码。
"""
from __future__ import annotations

from agent_runtime.conversation_message.south_gateway.resource_resolver.contracts import (
    ResolvedResource,
    ResourceMeta,
)


class FileResourceResolverAdapter:
    """基于统一文件资源服务的资源解析适配器。

    该适配器将 ResourceResolver 接口的调用转发给 FileResourceService，
    把文件资源服务的内部模型（FileResourceAccessResult）转换为
    消息渲染所需的 ResourceMeta。
    """

    def __init__(self, file_resource_service: object) -> None:
        # 统一文件资源服务实例。该服务提供资源的本地路径、Base64 内容、
        # 对象存储地址和结构化元数据，是资源解析的数据源。
        self._file_resource_service = file_resource_service

    async def get_base64(self, resource_id: str) -> str:
        """按资源标识获取 Base64 编码的资源内容。

        委托 FileResourceService.get_resources 获取资源访问结果，
        返回其中的 resource_base64_content 字段。
        """
        return (await self.resolve_resource(resource_id)).base64_content or ""

    async def get_public_url(self, resource_id: str) -> str | None:
        return (await self.resolve_resource(resource_id)).object_storage_url

    async def get_resource_meta(self, resource_id: str) -> ResourceMeta:
        """按资源标识获取资源元数据。

        委托 FileResourceService.get_resources 获取资源访问结果，
        将 FileResourceAccessResult 的 MIME 类型和元数据类别
        转换为消息渲染所需的 ResourceMeta。
        """
        resource = await self.resolve_resource(resource_id)
        return ResourceMeta(
            mime_type=resource.mime_type,
            resource_category=resource.resource_category,
            size_in_bytes=resource.size_in_bytes,
            image_width=resource.image_width,
            image_height=resource.image_height,
            pdf_page_count=resource.pdf_page_count,
        )

    async def resolve_resource(self, resource_id: str) -> ResolvedResource:
        results = self._file_resource_service.get_resources([resource_id])
        result = results[0]
        if result is None:
            raise ValueError(f"资源不存在: {resource_id}")
        metadata = result.resource_metadata
        return ResolvedResource(
            resource_id=result.resource_id,
            mime_type=result.resource_mime_type,
            resource_category=metadata.resource_category,
            object_storage_url=result.object_storage_url,
            base64_content=result.resource_base64_content,
            local_storage_path=result.local_storage_path,
            image_width=getattr(metadata, "image_width", None),
            image_height=getattr(metadata, "image_height", None),
            pdf_page_count=getattr(metadata, "pdf_page_count", None),
        )

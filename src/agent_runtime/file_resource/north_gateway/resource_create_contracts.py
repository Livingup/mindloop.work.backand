"""统一文件资源创建业务北向契约。

该模块定义统一文件资源服务"资源创建"业务域对外暴露的请求模型、结果模型和服务接口。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LocalFileCreateRequest:
    """本地文件资源创建请求。

    调用方希望把某个本地文件纳入统一文件资源服务管理时，提交该请求。
    """

    # 调用方提供的本地文件绝对路径。
    local_file_path: str

    # 调用方声明的资源 MIME 类型。
    resource_mime_type: str


@dataclass(frozen=True, slots=True)
class Base64CreateRequest:
    """Base64 资源创建请求。

    调用方直接提交文件 Base64 内容，由统一文件资源服务完成资源建档。
    """

    # 调用方提交的 Base64 编码资源内容。
    resource_base64_content: str

    # 调用方声明的资源 MIME 类型。
    resource_mime_type: str


@dataclass(frozen=True, slots=True)
class RemoteUrlCreateRequest:
    """远程 URL 资源创建请求。

    LLM 工具或外部调用方只能理解 URL，不能直接提供 Runtime 内部 resource_id。
    该请求用于把 http/https URL 接入统一文件资源服务。
    """

    # 调用方提供的 http/https 文件 URL。
    resource_url: str

    # 调用方可选声明的资源 MIME 类型；为空时由响应头或 URL 后缀推断。
    resource_mime_type: str | None = None

    # 可选下载字节上限，用于在接入阶段提前执行业务侧文件大小限制。
    max_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class FileResourceCreateResult:
    """统一文件资源创建结果。

    向调用方返回新建或去重命中的资源标识与访问地址。
    """

    # 统一文件资源唯一标识。
    resource_id: str

    # 资源对象存储访问地址。
    object_storage_url: str

    # 资源本地存储路径。
    local_storage_path: str


class FileResourceCreateNorthGateway(Protocol):
    """统一文件资源创建业务北向网关接口。"""

    def create_resources_from_local_files(
        self,
        create_requests: list[LocalFileCreateRequest],
    ) -> list[FileResourceCreateResult]:
        """从本地文件批量创建统一文件资源。"""

    def create_resources_from_base64_contents(
        self,
        create_requests: list[Base64CreateRequest],
    ) -> list[FileResourceCreateResult]:
        """从 Base64 内容批量创建统一文件资源。"""

    def create_resources_from_remote_urls(
        self,
        create_requests: list[RemoteUrlCreateRequest],
    ) -> list[FileResourceCreateResult]:
        """从远程 http/https URL 批量创建统一文件资源。"""

"""统一文件资源服务域配置模型。

本模块只定义文件资源服务域自身的配置 dataclass。
全局配置加载与注册中心见顶层 agent_runtime.config。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ObjectStorageGatewayConfig:
    """对象存储南向网关配置。"""

    # 对象存储网关提供商标识。
    gateway_provider: str

    # 对象存储服务 endpoint。
    object_storage_endpoint: str

    # 对象存储区域。
    object_storage_region: str

    # 对象存储桶名称。
    object_storage_bucket_name: str

    # 对象存储公共访问地址前缀。
    object_storage_public_base_url: str

    # 对象存储对象键前缀。
    object_storage_object_key_prefix: str

    # 对象存储访问账号 ID。
    object_storage_access_key_id: str

    # 对象存储访问账号密钥。
    object_storage_access_key_secret: str

    # 对象存储连接超时时间，单位秒。默认 60 秒。
    connect_timeout_seconds: int = 60


@dataclass(frozen=True, slots=True)
class FileResourceServiceConfig:
    """统一文件资源服务配置。"""

    # 单次批量处理上限。
    file_resource_batch_limit: int

    # 本地资源热存储根目录。
    local_resource_storage_root_path: str

    # 当前启用的对象存储网关提供商。
    object_storage_gateway_provider: str

    # 对象存储南向网关配置。
    object_storage_gateway: ObjectStorageGatewayConfig

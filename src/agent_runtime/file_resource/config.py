"""统一文件资源服务域配置模型。

本模块只定义文件资源服务域自身的配置 dataclass。
全局配置加载与注册中心见顶层 agent_runtime.config。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FileResourceServiceConfig:
    """统一文件资源服务配置。"""

    # 单次批量处理上限。
    file_resource_batch_limit: int

    # 本地资源热存储根目录。
    local_resource_storage_root_path: str

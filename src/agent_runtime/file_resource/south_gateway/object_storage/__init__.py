"""对象存储南向网关业务包。

该包承载统一文件资源服务面向外部对象存储服务的全部南向协作能力：
- contracts: 对象存储网关接口契约
- aliyun_oss: 阿里云国际 OSS 适配实现
- in_memory: 开发测试用内存模拟实现
"""
from agent_runtime.file_resource.south_gateway.object_storage.contracts import (
    ObjectStorageGateway,
    UploadedObjectStorageResource,
)

__all__ = [
    "ObjectStorageGateway",
    "UploadedObjectStorageResource",
]

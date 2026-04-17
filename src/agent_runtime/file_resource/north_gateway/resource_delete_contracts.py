"""统一文件资源删除业务北向契约。

该模块定义统一文件资源服务"资源删除"业务域对外暴露的服务接口。
"""
from __future__ import annotations

from typing import Protocol


class FileResourceDeleteNorthGateway(Protocol):
    """统一文件资源删除业务北向网关接口。

    删除采用物理删除语义：本地内容、对象存储内容、档案记录全部清除。
    资源不存在时静默跳过。
    """

    def delete_resources(self, resource_ids: list[str]) -> None:
        """按资源标识批量删除统一文件资源。"""

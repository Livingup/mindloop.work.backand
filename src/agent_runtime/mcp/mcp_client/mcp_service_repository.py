# -*- coding: utf-8 -*-
"""MCP 服务配置仓储接口 — 定义在领域层，实现在基础设施层。"""

from __future__ import annotations

from typing import Protocol

from agent_runtime.mcp.mcp_client.mcp_service_definition import McpServiceDefinition


class McpServiceRepository(Protocol):
    """MCP 服务配置仓储接口。

    集合语义，提供 MCP 服务配置定义的持久化存取操作。
    接口定义在领域层，具体实现（如 YAML 文件存储）在基础设施层。
    """

    def find_all(self) -> list[McpServiceDefinition]:
        """列出所有服务配置。

        Returns:
            所有 MCP 服务配置定义列表。
        """
        ...

    def find_by_id(self, service_id: str) -> McpServiceDefinition | None:
        """按 ID 查找服务配置。

        Args:
            service_id: 服务唯一标识。

        Returns:
            找到返回 McpServiceDefinition，否则返回 None。
        """
        ...

    def save(self, definition: McpServiceDefinition) -> None:
        """保存服务配置（新增或更新）。

        Args:
            definition: 要保存的服务配置定义。
        """
        ...

    def delete(self, service_id: str) -> bool:
        """删除服务配置。

        Args:
            service_id: 服务唯一标识。

        Returns:
            是否存在并已成功删除。
        """
        ...

    def contains(self, service_id: str) -> bool:
        """是否存在指定服务配置。

        Args:
            service_id: 服务唯一标识。

        Returns:
            是否存在。
        """
        ...

    def config_dir_mtime(self) -> float:
        """获取配置目录的最大修改时间戳。

        返回配置目录自身及其下所有 YAML 文件中最大的 mtime。
        用于跨进程变更检测：对比前后两次 mtime 即可判断是否有外部修改。

        Returns:
            最大 mtime（Unix 时间戳），目录不存在时返回 0.0。
        """
        ...

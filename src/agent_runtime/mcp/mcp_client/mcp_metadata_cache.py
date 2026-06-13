# -*- coding: utf-8 -*-
"""MCP 服务元数据快照缓存。

该模块承载 MCP 元数据的两级缓存模型：
1. JsonMcpMetadataSnapshotRepository 负责本机磁盘持久化快照；
2. McpMetadataRegistry 负责进程全局内存快照，供 Agent 热路径和 MCP 元工具同步读取。

快照只保存服务能力、服务说明、工具定义和资源列表等元数据，不保存真实工具执行结果或资源内容。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from agent_runtime.mcp.mcp_client.mcp_value_objects import (
    McpResourceInfo,
    McpToolInfo,
    McpToolSummary,
)

logger = logging.getLogger(__name__)

_CACHE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class McpCachedTool:
    """MCP 工具元数据快照。"""

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_tool_info(cls, info: McpToolInfo) -> McpCachedTool:
        return cls(
            name=info.name,
            description=info.description,
            input_schema=dict(info.input_schema or {}),
        )

    @classmethod
    def from_tool_summary(
        cls, summary: McpToolSummary, *, input_schema: dict[str, Any] | None = None
    ) -> McpCachedTool:
        return cls(
            name=summary.name,
            description=summary.description,
            input_schema=dict(input_schema or {}),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> McpCachedTool:
        return cls(
            name=str(data.get("name") or ""),
            description=data.get("description"),
            input_schema=dict(data.get("input_schema") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "input_schema": self.input_schema}
        if self.description is not None:
            data["description"] = self.description
        return data

    def to_tool_summary(self) -> McpToolSummary:
        return McpToolSummary(name=self.name, description=self.description)

    def to_tool_info(self) -> McpToolInfo:
        return McpToolInfo(
            name=self.name,
            description=self.description,
            input_schema=dict(self.input_schema or {}),
        )


@dataclass(frozen=True)
class McpCachedResource:
    """MCP 资源列表元数据快照。"""

    uri: str
    name: str
    description: str | None = None
    mime_type: str | None = None

    @classmethod
    def from_resource_info(cls, info: McpResourceInfo) -> McpCachedResource:
        return cls(
            uri=info.uri,
            name=info.name,
            description=info.description,
            mime_type=info.mime_type,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> McpCachedResource:
        return cls(
            uri=str(data.get("uri") or ""),
            name=str(data.get("name") or ""),
            description=data.get("description"),
            mime_type=data.get("mime_type"),
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"uri": self.uri, "name": self.name}
        if self.description is not None:
            data["description"] = self.description
        if self.mime_type is not None:
            data["mime_type"] = self.mime_type
        return data

    def to_resource_info(self) -> McpResourceInfo:
        return McpResourceInfo(
            uri=self.uri,
            name=self.name,
            description=self.description,
            mime_type=self.mime_type,
        )


@dataclass(frozen=True)
class McpServiceMetadataSnapshot:
    """单个 MCP 服务的当前元数据快照。"""

    service_id: str
    supports_tools: bool = False
    supports_resources: bool = False
    server_name: str = ""
    server_version: str = ""
    instructions: str | None = None
    tools: tuple[McpCachedTool, ...] = field(default_factory=tuple)
    resources: tuple[McpCachedResource, ...] = field(default_factory=tuple)
    refreshed_at: float | None = None
    last_refresh_succeeded: bool = False
    last_refresh_error: str | None = None
    last_refresh_error_at: float | None = None

    @classmethod
    def successful(
        cls,
        *,
        service_id: str,
        supports_tools: bool,
        supports_resources: bool,
        server_name: str,
        server_version: str,
        instructions: str | None,
        tools: Iterable[McpCachedTool],
        resources: Iterable[McpCachedResource],
        refreshed_at: float,
    ) -> McpServiceMetadataSnapshot:
        return cls(
            service_id=service_id,
            supports_tools=supports_tools,
            supports_resources=supports_resources,
            server_name=server_name,
            server_version=server_version,
            instructions=instructions,
            tools=tuple(sorted(tools, key=lambda item: item.name)),
            resources=tuple(sorted(resources, key=lambda item: item.uri)),
            refreshed_at=refreshed_at,
            last_refresh_succeeded=True,
            last_refresh_error=None,
            last_refresh_error_at=None,
        )

    @classmethod
    def failed(
        cls,
        *,
        service_id: str,
        error: str,
        failed_at: float,
        previous: McpServiceMetadataSnapshot | None = None,
    ) -> McpServiceMetadataSnapshot:
        if previous is None:
            return cls(
                service_id=service_id,
                last_refresh_succeeded=False,
                last_refresh_error=error,
                last_refresh_error_at=failed_at,
            )
        return cls(
            service_id=previous.service_id,
            supports_tools=previous.supports_tools,
            supports_resources=previous.supports_resources,
            server_name=previous.server_name,
            server_version=previous.server_version,
            instructions=previous.instructions,
            tools=previous.tools,
            resources=previous.resources,
            refreshed_at=previous.refreshed_at,
            last_refresh_succeeded=False,
            last_refresh_error=error,
            last_refresh_error_at=failed_at,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> McpServiceMetadataSnapshot:
        tools = tuple(
            McpCachedTool.from_dict(item)
            for item in data.get("tools", [])
            if isinstance(item, dict)
        )
        resources = tuple(
            McpCachedResource.from_dict(item)
            for item in data.get("resources", [])
            if isinstance(item, dict)
        )
        return cls(
            service_id=str(data.get("service_id") or ""),
            supports_tools=bool(data.get("supports_tools", False)),
            supports_resources=bool(data.get("supports_resources", False)),
            server_name=str(data.get("server_name") or ""),
            server_version=str(data.get("server_version") or ""),
            instructions=data.get("instructions"),
            tools=tuple(sorted(tools, key=lambda item: item.name)),
            resources=tuple(sorted(resources, key=lambda item: item.uri)),
            refreshed_at=data.get("refreshed_at"),
            last_refresh_succeeded=bool(data.get("last_refresh_succeeded", False)),
            last_refresh_error=data.get("last_refresh_error"),
            last_refresh_error_at=data.get("last_refresh_error_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _CACHE_SCHEMA_VERSION,
            "service_id": self.service_id,
            "supports_tools": self.supports_tools,
            "supports_resources": self.supports_resources,
            "server_name": self.server_name,
            "server_version": self.server_version,
            "instructions": self.instructions,
            "tools": [tool.to_dict() for tool in self.tools],
            "resources": [resource.to_dict() for resource in self.resources],
            "refreshed_at": self.refreshed_at,
            "last_refresh_succeeded": self.last_refresh_succeeded,
            "last_refresh_error": self.last_refresh_error,
            "last_refresh_error_at": self.last_refresh_error_at,
        }

    def to_tool_summaries(self) -> list[McpToolSummary]:
        return [tool.to_tool_summary() for tool in self.tools]

    def to_tool_infos(self, tool_names: Iterable[str]) -> list[McpToolInfo]:
        name_set = set(tool_names)
        return [
            tool.to_tool_info()
            for tool in self.tools
            if not name_set or tool.name in name_set
        ]

    def to_resource_infos(self) -> list[McpResourceInfo]:
        return [resource.to_resource_info() for resource in self.resources]


class JsonMcpMetadataSnapshotRepository:
    """基于本机 JSON 文件的 MCP 元数据快照仓储。"""

    def __init__(self, cache_dir_accessor: Callable[[], str]) -> None:
        self._cache_dir_accessor = cache_dir_accessor

    @property
    def _cache_dir(self) -> Path:
        return Path(self._cache_dir_accessor()).expanduser()

    def load_all(self) -> list[McpServiceMetadataSnapshot]:
        cache_dir = self._cache_dir
        if not cache_dir.exists():
            return []
        snapshots: list[McpServiceMetadataSnapshot] = []
        for json_file in sorted(cache_dir.glob("*.json")):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                snapshot = McpServiceMetadataSnapshot.from_dict(data)
                if snapshot.service_id:
                    snapshots.append(snapshot)
            except Exception as exc:  # noqa: BLE001 - 单个缓存文件损坏不应阻断启动
                logger.warning("MCP 元数据缓存文件读取失败，已跳过: %s — %s", json_file, exc)
        return snapshots

    def save(self, snapshot: McpServiceMetadataSnapshot) -> None:
        cache_dir = self._cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        target = self._path_for_service(snapshot.service_id)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(target)

    def delete(self, service_id: str) -> bool:
        target = self._path_for_service(service_id)
        if target.exists():
            target.unlink()
            return True
        return False

    def _path_for_service(self, service_id: str) -> Path:
        return self._cache_dir / f"{service_id}.json"


class McpMetadataRegistry:
    """进程内 MCP 元数据快照注册表。"""

    def __init__(self) -> None:
        self._snapshots: dict[str, McpServiceMetadataSnapshot] = {}
        self._lock = threading.RLock()

    def hydrate(self, snapshots: Iterable[McpServiceMetadataSnapshot]) -> None:
        """用持久化快照替换当前内存状态。"""
        with self._lock:
            self._snapshots = {snapshot.service_id: snapshot for snapshot in snapshots}

    def service_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._snapshots)

    def list_snapshots(self) -> list[McpServiceMetadataSnapshot]:
        with self._lock:
            return [self._snapshots[key] for key in sorted(self._snapshots)]

    def get_snapshot(self, service_id: str) -> McpServiceMetadataSnapshot | None:
        with self._lock:
            return self._snapshots.get(service_id)

    def upsert(self, snapshot: McpServiceMetadataSnapshot) -> bool:
        """更新内存快照。返回 True 表示状态发生变化。"""
        with self._lock:
            current = self._snapshots.get(snapshot.service_id)
            if current == snapshot:
                return False
            self._snapshots[snapshot.service_id] = snapshot
            return True

    def remove(self, service_id: str) -> bool:
        with self._lock:
            return self._snapshots.pop(service_id, None) is not None

    def get_tool_summaries(self, service_id: str) -> list[McpToolSummary]:
        snapshot = self.get_snapshot(service_id)
        return snapshot.to_tool_summaries() if snapshot is not None else []

    def get_tool_details(self, service_id: str, tool_names: Iterable[str]) -> list[McpToolInfo]:
        snapshot = self.get_snapshot(service_id)
        return snapshot.to_tool_infos(tool_names) if snapshot is not None else []

    def get_resources(self, service_id: str) -> list[McpResourceInfo]:
        snapshot = self.get_snapshot(service_id)
        return snapshot.to_resource_infos() if snapshot is not None else []


_global_metadata_registry: McpMetadataRegistry | None = None
_global_metadata_registry_lock = threading.Lock()


def global_mcp_metadata_registry() -> McpMetadataRegistry:
    """返回进程全局 MCP 元数据内存注册表。"""
    global _global_metadata_registry
    with _global_metadata_registry_lock:
        if _global_metadata_registry is None:
            _global_metadata_registry = McpMetadataRegistry()
        return _global_metadata_registry

# -*- coding: utf-8 -*-
"""MCP streamable-http 服务定义的云端 PostgreSQL 仓储与传输分流仓储。"""
from __future__ import annotations

import json
import zlib
from typing import Any, Callable

from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore
from agent_runtime.mcp.mcp_client.mcp_service_definition import McpServiceDefinition
from agent_runtime.mcp.mcp_client.mcp_service_repository import McpServiceRepository


class PostgresStreamableHttpMcpServiceRepository:
    """streamable-http MCP 服务定义的云端 PostgreSQL 仓储。

    该仓储只接受 transport.type == streamable-http 的服务定义；stdio 服务包含本机命令、路径和环境，
    必须继续留在本机 YAML 仓储中。
    """

    _NAMESPACE = "mcp.streamable_http_service_definition"

    def __init__(self, store_accessor: Callable[[], PostgresUserConfigStore]) -> None:
        self._store_accessor = store_accessor

    @property
    def _store(self) -> PostgresUserConfigStore:
        return self._store_accessor()

    def find_all(self) -> list[McpServiceDefinition]:
        definitions: list[McpServiceDefinition] = []
        for payload in self._store.list_payloads(self._NAMESPACE):
            definition = _definition_from_payload(payload)
            if definition.transport_type == "streamable-http":
                definitions.append(definition)
        return sorted(definitions, key=lambda item: item.service_id)

    def find_by_id(self, service_id: str) -> McpServiceDefinition | None:
        payload = self._store.get_payload(self._NAMESPACE, service_id)
        if payload is None:
            return None
        definition = _definition_from_payload(payload)
        if definition.transport_type != "streamable-http":
            return None
        return definition

    def save(self, definition: McpServiceDefinition) -> None:
        if definition.transport_type != "streamable-http":
            raise ValueError("云端 MCP 仓储只允许保存 streamable-http 服务定义。")
        definition.validate()
        self._store.save_payload(
            self._NAMESPACE,
            definition.service_id,
            _definition_payload(definition),
        )

    def delete(self, service_id: str) -> bool:
        return self._store.delete_payload(self._NAMESPACE, service_id)

    def contains(self, service_id: str) -> bool:
        return self._store.contains_payload(self._NAMESPACE, service_id)

    def config_dir_mtime(self) -> float:
        """返回云端配置快照签名，供现有 reconcile 变更检测复用。

        PostgreSQL item 没有本机目录 mtime；这里用 payload 稳定序列化后的 CRC32 作为变更签名。
        云端不可用时异常应直接暴露，禁止静默回退到本地配置。
        """
        payloads = self._store.list_payloads(self._NAMESPACE)
        if not payloads:
            return 0.0
        raw = json.dumps(payloads, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return float(zlib.crc32(raw.encode("utf-8")))


class TransportRoutingMcpServiceRepository:
    """按传输类型拆分 MCP 服务定义读写的组合仓储。

    - stdio：始终由本机 YAML 仓储保存；
    - streamable-http：始终由云端 PostgreSQL 仓储保存。

    该组合不是云端优先/本地 fallback，而是基于业务边界的二选一分流。
    """

    def __init__(
        self,
        *,
        local_repository: McpServiceRepository,
        shared_repository: McpServiceRepository,
    ) -> None:
        self._local_repository = local_repository
        self._shared_repository = shared_repository

    def find_all(self) -> list[McpServiceDefinition]:
        definitions: dict[str, McpServiceDefinition] = {}
        for definition in self._local_repository.find_all():
            if definition.transport_type == "stdio":
                definitions[definition.service_id] = definition
        for definition in self._shared_repository.find_all():
            if definition.transport_type == "streamable-http" and definition.service_id not in definitions:
                definitions[definition.service_id] = definition
        return [definitions[key] for key in sorted(definitions)]

    def find_by_id(self, service_id: str) -> McpServiceDefinition | None:
        local_definition = self._local_repository.find_by_id(service_id)
        if local_definition is not None and local_definition.transport_type == "stdio":
            return local_definition
        shared_definition = self._shared_repository.find_by_id(service_id)
        if shared_definition is not None and shared_definition.transport_type == "streamable-http":
            return shared_definition
        return None

    def save(self, definition: McpServiceDefinition) -> None:
        definition.validate()
        if definition.transport_type == "stdio":
            self._local_repository.save(definition)
            self._shared_repository.delete(definition.service_id)
            return
        if definition.transport_type == "streamable-http":
            self._shared_repository.save(definition)
            self._local_repository.delete(definition.service_id)
            return
        raise ValueError(f"不支持的 MCP transport: {definition.transport_type}")

    def delete(self, service_id: str) -> bool:
        deleted = False
        definition = self.find_by_id(service_id)
        if definition is None:
            return False
        if definition.transport_type == "stdio":
            deleted = self._local_repository.delete(service_id)
        elif definition.transport_type == "streamable-http":
            deleted = self._shared_repository.delete(service_id)
        return deleted

    def contains(self, service_id: str) -> bool:
        return self.find_by_id(service_id) is not None

    def config_dir_mtime(self) -> float:
        return self._local_repository.config_dir_mtime() + self._shared_repository.config_dir_mtime()


def _definition_payload(definition: McpServiceDefinition) -> dict[str, Any]:
    payload = definition.to_dict()
    payload["service_id"] = definition.service_id
    return payload


def _definition_from_payload(payload: dict[str, Any]) -> McpServiceDefinition:
    service_id = str(payload.get("service_id") or "")
    return McpServiceDefinition.from_dict(service_id, payload)

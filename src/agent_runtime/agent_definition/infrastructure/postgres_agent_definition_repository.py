# -*- coding: utf-8 -*-
"""Agent 定义 PostgreSQL 仓储实现。"""
from __future__ import annotations

from typing import Callable

from agent_runtime.agent_definition.agent_definition import AgentDefinition
from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore

_NAMESPACE = "agent_definition"


class PostgresAgentDefinitionRepository:
    """基于云端 PostgreSQL 的 Agent 定义仓储。

    不做本地缓存；每次读写都直接访问 PostgreSQL，机器 A/B 直连同一数据库即可共用定义。
    """

    def __init__(self, store_accessor: Callable[[], PostgresUserConfigStore]) -> None:
        self._store_accessor = store_accessor

    @property
    def _store(self) -> PostgresUserConfigStore:
        return self._store_accessor()

    def find_all(self) -> list[AgentDefinition]:
        definitions: list[AgentDefinition] = []
        for payload in self._store.list_payloads(_NAMESPACE):
            agent_id = str(payload.get("agent_id", ""))
            definitions.append(AgentDefinition.from_dict(agent_id, payload))
        return definitions

    def find_by_id(self, agent_id: str) -> AgentDefinition | None:
        payload = self._store.get_payload(_NAMESPACE, agent_id)
        if payload is None:
            return None
        return AgentDefinition.from_dict(agent_id, payload)

    def save(self, definition: AgentDefinition) -> None:
        self._store.save_payload(
            _NAMESPACE,
            definition.agent_id,
            definition.to_dict(),
        )

    def delete(self, agent_id: str) -> bool:
        return self._store.delete_payload(_NAMESPACE, agent_id)

    def contains(self, agent_id: str) -> bool:
        return self._store.contains_payload(_NAMESPACE, agent_id)

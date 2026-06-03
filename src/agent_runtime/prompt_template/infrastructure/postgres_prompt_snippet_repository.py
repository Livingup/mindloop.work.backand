# -*- coding: utf-8 -*-
"""提示词片段 PostgreSQL 仓储实现。"""
from __future__ import annotations

from typing import Callable

from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore
from agent_runtime.prompt_template.prompt_snippet import PromptSnippet

_NAMESPACE = "prompt_snippet"


class PostgresPromptSnippetRepository:
    """基于云端 PostgreSQL 的提示词片段仓储。

    不做本地缓存；每次读写都直接访问 PostgreSQL，保证多台 release 程序共用同一份片段数据。
    """

    def __init__(self, store_accessor: Callable[[], PostgresUserConfigStore]) -> None:
        self._store_accessor = store_accessor

    @property
    def _store(self) -> PostgresUserConfigStore:
        return self._store_accessor()

    def find_by_name(self, name: str) -> PromptSnippet | None:
        payload = self._store.get_payload(_NAMESPACE, name)
        if payload is None:
            return None
        return _payload_to_snippet(payload)

    def find_all(self) -> list[PromptSnippet]:
        return [_payload_to_snippet(payload) for payload in self._store.list_payloads(_NAMESPACE)]

    def save(self, snippet: PromptSnippet) -> None:
        self._store.save_payload(_NAMESPACE, snippet.name, _snippet_to_payload(snippet))

    def delete(self, name: str) -> bool:
        return self._store.delete_payload(_NAMESPACE, name)

    def exists(self, name: str) -> bool:
        return self._store.contains_payload(_NAMESPACE, name)

    def rename(self, old_name: str, new_name: str) -> None:
        payload = self._store.get_payload(_NAMESPACE, old_name)
        if payload is None:
            return
        payload["name"] = new_name
        self._store.save_payload(_NAMESPACE, new_name, payload)
        self._store.delete_payload(_NAMESPACE, old_name)

    def reload(self) -> None:
        """PostgreSQL 仓储不做本地缓存，reload 为兼容仓储协议的空操作。"""
        return None


def _snippet_to_payload(snippet: PromptSnippet) -> dict[str, object]:
    return {
        "name": snippet.name,
        "text": snippet.text,
        "category": snippet.category,
    }


def _payload_to_snippet(payload: dict[str, object]) -> PromptSnippet:
    return PromptSnippet(
        name=str(payload.get("name", "")),
        text=str(payload.get("text", "")),
        category=str(payload.get("category", "default")),
    )

# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_runtime.cloud_user_config.postgres_store import (
    CloudUserConfigCacheSnapshot,
    PostgresUserConfigStore,
    CONFIG_CENTER_POLL_INTERVAL_SECONDS,
)


@dataclass(frozen=True)
class _FakePoolConfig:
    connect_timeout_seconds: int = 1


@dataclass(frozen=True)
class _FakeDatabaseConfig:
    host: str = "localhost"
    port: int = 5432
    database: str = "agent"
    username: str = "agent"
    sslmode: str = "prefer"
    pool: _FakePoolConfig = _FakePoolConfig()

    def require_password(self) -> str:
        return "password"


class _FakeCachedConfigStore(PostgresUserConfigStore):
    def __init__(self) -> None:
        super().__init__(lambda: _FakeDatabaseConfig())
        self.database_revision = 1
        self.database_items: dict[tuple[str, str], dict[str, Any]] = {
            ("prompt_snippet", "welcome"): {"name": "welcome", "text": "v1"},
        }
        self.snapshot_reads = 0
        self.revision_reads = 0

    def ensure_schema(self) -> None:
        return None

    def _read_cache_snapshot_from_database(self) -> CloudUserConfigCacheSnapshot:
        self.snapshot_reads += 1
        return CloudUserConfigCacheSnapshot(
            revision=self.database_revision,
            items={key: dict(value) for key, value in self.database_items.items()},
        )

    def _read_database_revision(self) -> int:
        self.revision_reads += 1
        return self.database_revision


def test_backend_cache_loads_postgres_config_snapshot_and_serves_reads_from_memory() -> None:
    _FakeCachedConfigStore.reset_process_cache_for_tests()
    store = _FakeCachedConfigStore()

    assert store.list_payloads("prompt_snippet") == [{"name": "welcome", "text": "v1"}]
    assert store.get_payload("prompt_snippet", "welcome") == {"name": "welcome", "text": "v1"}
    assert store.contains_payload("prompt_snippet", "welcome") is True

    store.database_items[("prompt_snippet", "welcome")] = {"name": "welcome", "text": "v2"}
    assert store.get_payload("prompt_snippet", "welcome") == {"name": "welcome", "text": "v1"}
    assert store.snapshot_reads == 1


def test_backend_poll_refreshes_memory_cache_only_when_database_revision_changes() -> None:
    _FakeCachedConfigStore.reset_process_cache_for_tests()
    store = _FakeCachedConfigStore()

    store.ensure_cache_loaded()
    unchanged = store.refresh_cache_if_revision_changed()
    assert unchanged.revision == 1
    assert store.get_payload("prompt_snippet", "welcome") == {"name": "welcome", "text": "v1"}
    assert store.snapshot_reads == 1
    assert store.revision_reads == 1

    store.database_revision = 2
    store.database_items[("prompt_snippet", "welcome")] = {"name": "welcome", "text": "v2"}
    refreshed = store.refresh_cache_if_revision_changed()

    assert refreshed.revision == 2
    assert store.get_payload("prompt_snippet", "welcome") == {"name": "welcome", "text": "v2"}
    assert store.snapshot_reads == 2
    assert store.revision_reads == 2


def test_default_backend_config_center_poll_interval_is_five_seconds() -> None:
    assert CONFIG_CENTER_POLL_INTERVAL_SECONDS == 5

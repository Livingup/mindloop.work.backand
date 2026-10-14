# -*- coding: utf-8 -*-
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runtime.cloud_user_config.local_state import CloudUserConfigLocalStateRepository

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
    bootstrap_data_dir: str = ""
    pool: _FakePoolConfig = _FakePoolConfig()

    def require_password(self) -> str:
        return "password"


class _FakeSchemaConnection:
    def __init__(self, *, fail_on_schema: bool = False) -> None:
        self.fail_on_schema = fail_on_schema
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []
        self.commits = 0
        self.closed = False

    def execute(self, sql: str, params: tuple[object, ...] | None = None):
        self.statements.append((sql, params))
        if self.fail_on_schema and "CREATE TABLE" in sql:
            raise RuntimeError("schema init failed")
        if "SELECT revision" in sql:
            return self

        return self

    def fetchone(self) -> dict[str, int]:
        return {"revision": 1}

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


class _FakeSchemaStore(PostgresUserConfigStore):
    def __init__(self, config: _FakeDatabaseConfig, *, fail_on_schema: bool = False) -> None:
        self._config = config
        self.fail_on_schema = fail_on_schema
        self.connections: list[_FakeSchemaConnection] = []
        super().__init__(lambda: self._config)

    @contextmanager
    def _connect(self):
        conn = _FakeSchemaConnection(fail_on_schema=self.fail_on_schema)
        self.connections.append(conn)
        try:
            yield conn
        finally:
            conn.close()


class _FakeCachedConfigStore(PostgresUserConfigStore):
    def __init__(self, bootstrap_data_dir: str) -> None:
        self._config = _FakeDatabaseConfig(bootstrap_data_dir=bootstrap_data_dir)
        super().__init__(
            lambda: self._config,
            local_state_repository=CloudUserConfigLocalStateRepository(lambda: bootstrap_data_dir),
        )
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


def test_postgres_schema_initialization_is_cached_per_process_config_key() -> None:
    _FakeSchemaStore.reset_process_cache_for_tests()
    config = _FakeDatabaseConfig()
    first_store = _FakeSchemaStore(config)
    second_store = _FakeSchemaStore(config)

    first_store.get_current_config_revision()
    second_store.get_current_config_revision()

    assert len(first_store.connections) == 2
    first_schema_statements = [statement for statement, _ in first_store.connections[0].statements]
    assert first_schema_statements[0] == "SELECT pg_advisory_xact_lock(%s)"
    assert any("CREATE OR REPLACE FUNCTION cloud_user_config_touch_revision" in statement for statement in first_schema_statements)
    assert first_store.connections[0].commits == 1

    assert len(second_store.connections) == 1
    second_statements = [statement for statement, _ in second_store.connections[0].statements]
    assert second_statements == [
        """
            SELECT revision
            FROM cloud_user_config_revision_state
            WHERE id = TRUE
            """
    ]


def test_postgres_schema_initialization_failure_is_not_cached() -> None:
    _FakeSchemaStore.reset_process_cache_for_tests()
    config = _FakeDatabaseConfig()
    failing_store = _FakeSchemaStore(config, fail_on_schema=True)

    try:
        failing_store.ensure_schema()
    except RuntimeError as exc:
        assert str(exc) == "schema init failed"
    else:
        raise AssertionError("schema initialization failure should be propagated")

    retry_store = _FakeSchemaStore(config)
    retry_store.ensure_schema()

    assert len(retry_store.connections) == 1
    retry_statements = [statement for statement, _ in retry_store.connections[0].statements]
    assert retry_statements[0] == "SELECT pg_advisory_xact_lock(%s)"
    assert any("CREATE TABLE IF NOT EXISTS cloud_user_config_items" in statement for statement in retry_statements)


def test_postgres_schema_initialization_reruns_for_new_database_config_key() -> None:
    _FakeSchemaStore.reset_process_cache_for_tests()
    first_store = _FakeSchemaStore(_FakeDatabaseConfig(database="agent"))
    second_store = _FakeSchemaStore(_FakeDatabaseConfig(database="agent_other"))

    first_store.ensure_schema()
    second_store.ensure_schema()

    assert len(first_store.connections) == 1
    assert len(second_store.connections) == 1
    assert [statement for statement, _ in first_store.connections[0].statements][0] == "SELECT pg_advisory_xact_lock(%s)"
    assert [statement for statement, _ in second_store.connections[0].statements][0] == "SELECT pg_advisory_xact_lock(%s)"


def test_backend_cache_loads_postgres_config_snapshot_and_serves_reads_from_memory(tmp_path: Path) -> None:
    _FakeCachedConfigStore.reset_process_cache_for_tests()
    store = _FakeCachedConfigStore(str(tmp_path / "cloud-user-config"))
    store.reload_cache_from_database()
    _FakeCachedConfigStore.reset_process_cache_for_tests()

    assert store.list_payloads("prompt_snippet") == [{"name": "welcome", "text": "v1"}]
    assert store.get_payload("prompt_snippet", "welcome") == {"name": "welcome", "text": "v1"}
    assert store.contains_payload("prompt_snippet", "welcome") is True

    store.database_items[("prompt_snippet", "welcome")] = {"name": "welcome", "text": "v2"}
    assert store.get_payload("prompt_snippet", "welcome") == {"name": "welcome", "text": "v1"}
    assert store.snapshot_reads == 1


def test_backend_poll_refreshes_memory_cache_only_when_database_revision_changes(tmp_path: Path) -> None:
    _FakeCachedConfigStore.reset_process_cache_for_tests()
    store = _FakeCachedConfigStore(str(tmp_path / "cloud-user-config"))
    store.reload_cache_from_database()
    _FakeCachedConfigStore.reset_process_cache_for_tests()

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

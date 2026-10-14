# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.cloud_user_config.bootstrap_settings import (
    CloudUserConfigBootstrapSettingsRepository,
)
from agent_runtime.cloud_user_config.config import (
    CloudUserConfigDatabaseConfig,
    CloudUserConfigDatabasePoolConfig,
)
from agent_runtime.cloud_user_config.local_state import (
    CloudUserConfigLocalStateRepository,
    atomic_commit_database_reconfigure,
)
from agent_runtime.cloud_user_config.north_gateway.bootstrap_rest_adapter import (
    create_runtime_bootstrap_router,
)
from agent_runtime.cloud_user_config.postgres_store import (
    CloudUserConfigCacheSnapshot,
    PostgresUserConfigStore,
)
from agent_runtime.config import ConfigRegistry


class _RecordingSnapshotStore:
    process_cache_mutations = 0

    def __init__(
        self,
        config_accessor,
        *,
        fail: bool = False,
        revision: int = 11,
        oss_configured: bool = True,
    ) -> None:
        self._config_accessor = config_accessor
        self.fail = fail
        self.revision = revision
        self.oss_configured = oss_configured
        self.read_calls = 0
        self.reload_calls = 0

    def read_authoritative_snapshot_without_process_side_effects(self) -> CloudUserConfigCacheSnapshot:
        self.read_calls += 1
        if self.fail:
            raise RuntimeError("connection refused")
        config = self._config_accessor()
        return CloudUserConfigCacheSnapshot(
            revision=self.revision,
            items={
                ("file_resource.object_storage_settings", "default"): {
                    "gateway_provider": "aliyun_oss",
                    "object_storage_endpoint": "https://oss-cn-hongkong.aliyuncs.com",
                    "object_storage_region": "cn-hongkong",
                    "object_storage_bucket_name": "agent-runtime-resources",
                    "object_storage_public_base_url": "https://example.com",
                    "object_storage_object_key_prefix": "agent-runtime/file-resource",
                    "object_storage_access_key_id": "ak" if self.oss_configured else "",
                    "object_storage_access_key_secret": "sk" if self.oss_configured else "",
                    "connect_timeout_seconds": 120,
                }
            },
            config_key=(
                config.host,
                config.port,
                config.database,
                config.username,
                config.sslmode,
            ),
        )

    def reload_cache_from_database(self) -> CloudUserConfigCacheSnapshot:
        self.reload_calls += 1
        _RecordingSnapshotStore.process_cache_mutations += 1
        snapshot = self.read_authoritative_snapshot_without_process_side_effects()
        PostgresUserConfigStore._cache_revision = snapshot.revision
        PostgresUserConfigStore._cache_items = {key: dict(value) for key, value in snapshot.items.items()}
        PostgresUserConfigStore._cache_config_key = snapshot.config_key
        return snapshot


def _write_runtime_config(config_path: Path, data_root: Path) -> None:
    config_path.write_text(
        "\n".join(
            [
                "agent_runtime_service:",
                "  service_instance_name: \"test-runtime\"",
                "  service_environment: \"test\"",
                "  service_host: \"127.0.0.1\"",
                "  service_port: 8765",
                "file_resource_service:",
                "  file_resource_batch_limit: 100",
                f"  local_resource_storage_root_path: \"{(data_root / 'file-resources').as_posix()}\"",
                "conversation_repository:",
                f"  data_dir: \"{(data_root / 'conversation').as_posix()}\"",
                "prompt_template:",
                f"  snippets_dir: \"{(data_root / 'prompt-snippets').as_posix()}\"",
                "skill_service:",
                f"  skills_dir: \"{(data_root / 'skills').as_posix()}\"",
                "agent_definition:",
                f"  definitions_dir: \"{(data_root / 'agents').as_posix()}\"",
                "agent_runtime:",
                "  repository_type: \"sqlite\"",
                f"  data_dir: \"{(data_root / 'agent-runtime').as_posix()}\"",
                f"  trace_dir: \"{(data_root / 'agent-traces').as_posix()}\"",
                "llm_model_registry:",
                f"  registry_dir: \"{(data_root / 'llm-model-registry').as_posix()}\"",
                "mcp_client:",
                f"  config_dir: \"{(data_root / 'mcp-servers').as_posix()}\"",
                "cloud_user_config_database:",
                "  enabled: false",
                "  backend: \"local\"",
                "  host: \"\"",
                "  port: 5432",
                "  database: \"\"",
                "  username: \"\"",
                "  password: \"\"",
                f"  bootstrap_data_dir: \"{(data_root / 'cloud-user-config').as_posix()}\"",
                "  sslmode: \"require\"",
                "  pool:",
                "    min_size: 1",
                "    max_size: 5",
                "    connect_timeout_seconds: 10",
            ]
        ),
        encoding="utf-8",
    )


def _seed_existing_bootstrap(bootstrap_data_dir: Path) -> CloudUserConfigDatabaseConfig:
    config = CloudUserConfigDatabaseConfig(
        enabled=True,
        backend="postgresql",
        host="db.mindloop.chat",
        port=25432,
        database="agent_runtime",
        username="agent_runtime_client",
        password="old-secret",
        sslmode="require",
        bootstrap_data_dir=str(bootstrap_data_dir),
        pool=CloudUserConfigDatabasePoolConfig(
            min_size=1,
            max_size=5,
            connect_timeout_seconds=10,
        ),
    )
    CloudUserConfigBootstrapSettingsRepository(lambda: str(bootstrap_data_dir)).save_config(config)
    local_state = CloudUserConfigLocalStateRepository(lambda: str(bootstrap_data_dir))
    local_state.save_snapshot(
        config_key=(
            config.host,
            config.port,
            config.database,
            config.username,
            config.sslmode,
        ),
        revision=3,
        items={("legacy.namespace", "legacy-item"): {"keep": True}},
    )
    local_state.mark_object_storage_configured()
    return config


@pytest.fixture
def database_settings_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_root = tmp_path / "data"
    config_path = tmp_path / "agent_runtime.yaml"
    _write_runtime_config(config_path, data_root)
    bootstrap_dir = data_root / "cloud-user-config"
    existing = _seed_existing_bootstrap(bootstrap_dir)

    ConfigRegistry.load(str(config_path), watch=False)
    PostgresUserConfigStore.reset_process_cache_for_tests()
    _RecordingSnapshotStore.process_cache_mutations = 0

    old_key = (
        existing.host,
        existing.port,
        existing.database,
        existing.username,
        existing.sslmode,
    )
    PostgresUserConfigStore._cache_revision = 3
    PostgresUserConfigStore._cache_items = {("legacy.namespace", "legacy-item"): {"keep": True}}
    PostgresUserConfigStore._cache_config_key = old_key

    created_stores: list[_RecordingSnapshotStore] = []

    def _fake_store_factory(config_accessor, *args: Any, **kwargs: Any):
        store = _RecordingSnapshotStore(config_accessor, fail=False, revision=11, oss_configured=True)
        created_stores.append(store)
        return store

    monkeypatch.setattr(
        "agent_runtime.cloud_user_config.north_gateway.bootstrap_rest_adapter.PostgresUserConfigStore",
        _fake_store_factory,
    )

    app = FastAPI()
    app.include_router(
        create_runtime_bootstrap_router(
            cloud_user_config_database_active_at_start=True,
            active_database_config_key_at_start=old_key,
        ),
        prefix="/api/runtime-bootstrap",
    )
    client = TestClient(app)
    try:
        yield client, bootstrap_dir, created_stores, existing, old_key
    finally:
        ConfigRegistry.reset()
        PostgresUserConfigStore.reset_process_cache_for_tests()


def test_get_database_settings_returns_redacted_fields(database_settings_client) -> None:
    client, _bootstrap_dir, _stores, existing, _old_key = database_settings_client
    response = client.get("/api/runtime-bootstrap/database-settings")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["host"] == existing.host
    assert body["password_configured"] is True
    assert "password" not in body
    assert body["restart_required"] is False


def test_put_database_settings_atomic_success_keeps_process_cache_and_matches_snapshot_key(
    database_settings_client,
) -> None:
    client, bootstrap_dir, created_stores, _existing, old_key = database_settings_client

    response = client.put(
        "/api/runtime-bootstrap/database-settings",
        json={
            "enabled": True,
            "backend": "postgresql",
            "host": "127.0.0.1",
            "port": 5432,
            "database": "agent_runtime",
            "username": "agent_runtime_client",
            "password": "",
            "sslmode": "disable",
            "connect_timeout_seconds": 8,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["host"] == "127.0.0.1"
    assert body["snapshot_revision"] == 11
    assert body["restart_required"] is True

    store = created_stores[-1]
    assert store.read_calls == 1
    assert store.reload_calls == 0
    assert _RecordingSnapshotStore.process_cache_mutations == 0
    assert PostgresUserConfigStore._cache_config_key == old_key
    assert PostgresUserConfigStore._cache_revision == 3

    new_key = ("127.0.0.1", 5432, "agent_runtime", "agent_runtime_client", "disable")
    saved = CloudUserConfigBootstrapSettingsRepository(lambda: str(bootstrap_dir)).get_settings()
    assert saved is not None
    assert saved.host == "127.0.0.1"
    assert saved.password == "old-secret"
    assert saved.sslmode == "disable"

    snapshot = CloudUserConfigLocalStateRepository(lambda: str(bootstrap_dir)).get_snapshot(
        expected_config_key=new_key,
    )
    assert snapshot is not None
    assert snapshot.revision == 11
    assert snapshot.config_key == new_key
    assert CloudUserConfigLocalStateRepository(lambda: str(bootstrap_dir)).get_state().object_storage_decision == (
        "configured"
    )


def test_put_database_settings_connection_failure_keeps_old_durable_state(
    database_settings_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, bootstrap_dir, _stores, existing, old_key = database_settings_client

    def _failing_factory(config_accessor, *args: Any, **kwargs: Any):
        return _RecordingSnapshotStore(config_accessor, fail=True)

    monkeypatch.setattr(
        "agent_runtime.cloud_user_config.north_gateway.bootstrap_rest_adapter.PostgresUserConfigStore",
        _failing_factory,
    )

    response = client.put(
        "/api/runtime-bootstrap/database-settings",
        json={
            "enabled": True,
            "backend": "postgresql",
            "host": "127.0.0.1",
            "port": 5432,
            "database": "agent_runtime",
            "username": "agent_runtime_client",
            "password": "new-secret",
            "sslmode": "disable",
            "connect_timeout_seconds": 10,
        },
    )
    assert response.status_code == 400

    saved = CloudUserConfigBootstrapSettingsRepository(lambda: str(bootstrap_dir)).get_settings()
    assert saved is not None
    assert saved.host == existing.host
    assert saved.password == "old-secret"
    snapshot = CloudUserConfigLocalStateRepository(lambda: str(bootstrap_dir)).get_snapshot(
        expected_config_key=old_key,
    )
    assert snapshot is not None
    assert snapshot.revision == 3
    assert snapshot.items == {("legacy.namespace", "legacy-item"): {"keep": True}}
    assert CloudUserConfigLocalStateRepository(lambda: str(bootstrap_dir)).get_state().object_storage_decision == (
        "configured"
    )


def test_atomic_commit_rolls_back_when_snapshot_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap_dir = tmp_path / "cloud-user-config"
    existing = _seed_existing_bootstrap(bootstrap_dir)
    old_key = (
        existing.host,
        existing.port,
        existing.database,
        existing.username,
        existing.sslmode,
    )

    def _fail_snapshot_write(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("injected snapshot write failure")

    monkeypatch.setattr(
        "agent_runtime.cloud_user_config.local_state._tx_write_shared_snapshot",
        _fail_snapshot_write,
    )

    with pytest.raises(sqlite3.OperationalError, match="injected snapshot write failure"):
        atomic_commit_database_reconfigure(
            bootstrap_dir,
            enabled=True,
            backend="postgresql",
            host="127.0.0.1",
            port=5432,
            database="agent_runtime",
            username="agent_runtime_client",
            password="new-secret",
            sslmode="disable",
            pool_min_size=1,
            pool_max_size=5,
            connect_timeout_seconds=8,
            config_key=("127.0.0.1", 5432, "agent_runtime", "agent_runtime_client", "disable"),
            revision=99,
            items={("x", "y"): {"v": 1}},
            object_storage_decision="configured",
        )

    saved = CloudUserConfigBootstrapSettingsRepository(lambda: str(bootstrap_dir)).get_settings()
    assert saved is not None
    assert saved.host == existing.host
    assert saved.password == "old-secret"
    snapshot = CloudUserConfigLocalStateRepository(lambda: str(bootstrap_dir)).get_snapshot(
        expected_config_key=old_key,
    )
    assert snapshot is not None
    assert snapshot.revision == 3
    assert snapshot.items == {("legacy.namespace", "legacy-item"): {"keep": True}}
    assert CloudUserConfigLocalStateRepository(lambda: str(bootstrap_dir)).get_state().object_storage_decision == (
        "configured"
    )


def test_put_database_settings_atomic_commit_failure_keeps_old_state(
    database_settings_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, bootstrap_dir, _stores, existing, old_key = database_settings_client

    def _fail_atomic(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected atomic durable failure")

    monkeypatch.setattr(
        "agent_runtime.cloud_user_config.north_gateway.bootstrap_rest_adapter.atomic_commit_database_reconfigure",
        _fail_atomic,
    )

    response = client.put(
        "/api/runtime-bootstrap/database-settings",
        json={
            "enabled": True,
            "backend": "postgresql",
            "host": "127.0.0.1",
            "port": 5432,
            "database": "agent_runtime",
            "username": "agent_runtime_client",
            "password": "",
            "sslmode": "disable",
            "connect_timeout_seconds": 8,
        },
    )
    assert response.status_code == 500

    saved = CloudUserConfigBootstrapSettingsRepository(lambda: str(bootstrap_dir)).get_settings()
    assert saved is not None
    assert saved.host == existing.host
    assert saved.password == "old-secret"
    snapshot = CloudUserConfigLocalStateRepository(lambda: str(bootstrap_dir)).get_snapshot(
        expected_config_key=old_key,
    )
    assert snapshot is not None
    assert snapshot.revision == 3


def test_atomic_commit_rejects_mismatched_config_key_without_writing(tmp_path: Path) -> None:
    bootstrap_dir = tmp_path / "cloud-user-config"
    existing = _seed_existing_bootstrap(bootstrap_dir)
    old_key = (
        existing.host,
        existing.port,
        existing.database,
        existing.username,
        existing.sslmode,
    )

    with pytest.raises(ValueError, match="bootstrap credentials 与 shared snapshot config_key 不一致"):
        atomic_commit_database_reconfigure(
            bootstrap_dir,
            enabled=True,
            backend="postgresql",
            host="127.0.0.1",
            port=5432,
            database="agent_runtime",
            username="agent_runtime_client",
            password="new-secret",
            sslmode="disable",
            pool_min_size=1,
            pool_max_size=5,
            connect_timeout_seconds=8,
            config_key=("10.9.9.9", 9999, "other_db", "other_user", "require"),
            revision=99,
            items={("x", "y"): {"v": 1}},
            object_storage_decision="configured",
        )

    saved = CloudUserConfigBootstrapSettingsRepository(lambda: str(bootstrap_dir)).get_settings()
    assert saved is not None
    assert saved.host == existing.host
    assert saved.password == "old-secret"
    snapshot = CloudUserConfigLocalStateRepository(lambda: str(bootstrap_dir)).get_snapshot(
        expected_config_key=old_key,
    )
    assert snapshot is not None
    assert snapshot.revision == 3
    assert snapshot.items == {("legacy.namespace", "legacy-item"): {"keep": True}}
    assert CloudUserConfigLocalStateRepository(lambda: str(bootstrap_dir)).get_state().object_storage_decision == (
        "configured"
    )


def test_put_database_settings_serialized_puts_end_with_consistent_key(
    database_settings_client,
) -> None:
    client, bootstrap_dir, _stores, _existing, _old_key = database_settings_client
    results: list[int] = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()

    def _worker(host: str, port: int) -> None:
        try:
            response = client.put(
                "/api/runtime-bootstrap/database-settings",
                json={
                    "enabled": True,
                    "backend": "postgresql",
                    "host": host,
                    "port": port,
                    "database": "agent_runtime",
                    "username": "agent_runtime_client",
                    "password": "",
                    "sslmode": "disable",
                    "connect_timeout_seconds": 8,
                },
            )
            with results_lock:
                results.append(response.status_code)
        except BaseException as exc:  # noqa: BLE001 - 并发测试收集异常
            with results_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=_worker, args=("10.0.0.1", 5432)),
        threading.Thread(target=_worker, args=("10.0.0.2", 5433)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert not errors
    assert len(results) == 2
    assert all(status == 200 for status in results)

    saved = CloudUserConfigBootstrapSettingsRepository(lambda: str(bootstrap_dir)).get_settings()
    assert saved is not None
    saved_key = (saved.host, saved.port, saved.database, saved.username, saved.sslmode)
    snapshot = CloudUserConfigLocalStateRepository(lambda: str(bootstrap_dir)).get_snapshot(
        expected_config_key=saved_key,
    )
    assert snapshot is not None
    assert snapshot.config_key == saved_key
    assert saved_key in {
        ("10.0.0.1", 5432, "agent_runtime", "agent_runtime_client", "disable"),
        ("10.0.0.2", 5433, "agent_runtime", "agent_runtime_client", "disable"),
    }

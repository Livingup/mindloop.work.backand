# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_runtime.cloud_user_config.bootstrap_settings import CloudUserConfigBootstrapSettingsRepository
from agent_runtime.cloud_user_config.config import (
    CloudUserConfigDatabaseConfig,
    CloudUserConfigDatabasePoolConfig,
)
from agent_runtime.cloud_user_config.local_state import CloudUserConfigLocalStateRepository
from agent_runtime.config import ConfigRegistry, _cloud_user_config_database_key
from agent_runtime.server import create_app


def test_runtime_bootstrap_guard_promotes_same_process_after_durable_readiness(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    config_path = tmp_path / "agent_runtime.yaml"
    _write_runtime_config(config_path, data_root)

    try:
        ConfigRegistry.load(str(config_path), watch=False)
        app = create_app()
        with TestClient(app) as client:
            unauthenticated_blocked = client.get("/api/prompt-snippets")
            assert unauthenticated_blocked.status_code == 401
            assert unauthenticated_blocked.json()["bootstrap_required"] is True

            auth_response = client.post(
                "/api/auth/bootstrap",
                json={
                    "username": "admin",
                    "password": "StrongerPassw0rd!",
                    "device_id": "pytest",
                    "client_kind": "pytest",
                },
            )
            assert auth_response.status_code == 200
            token = auth_response.json()["access_token"]
            auth_headers = {"Authorization": f"Bearer {token}"}

            blocked = client.get("/api/prompt-snippets", headers=auth_headers)
            assert blocked.status_code == 503
            assert blocked.json()["bootstrap_required"] is True

            _persist_durable_bootstrap_state(data_root / "cloud-user-config")

            health = client.get("/api/health")
            assert health.status_code == 200

            promoted = client.get("/api/prompt-snippets", headers=auth_headers)
            assert promoted.status_code != 503
            assert promoted.status_code < 500
    finally:
        ConfigRegistry.reset()


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
                "lacp:",
                "  enabled: false",
                f"  data_dir: \"{(data_root / 'lacp').as_posix()}\"",
                "context_compression:",
                f"  archive_data_dir: \"{(data_root / 'context-compression').as_posix()}\"",
                "cloud_user_config_database:",
                "  enabled: true",
                "  backend: \"postgresql\"",
                "  host: \"db.mindloop.chat\"",
                "  port: 25432",
                "  database: \"agent_runtime\"",
                "  username: \"agent_runtime_client\"",
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


def _persist_durable_bootstrap_state(bootstrap_data_dir: Path) -> None:
    cloud_config = CloudUserConfigDatabaseConfig(
        enabled=True,
        backend="postgresql",
        host="db.mindloop.chat",
        port=25432,
        database="agent_runtime",
        username="agent_runtime_client",
        password="postgres-secret",
        sslmode="require",
        bootstrap_data_dir=str(bootstrap_data_dir),
        pool=CloudUserConfigDatabasePoolConfig(
            min_size=1,
            max_size=5,
            connect_timeout_seconds=10,
        ),
    )
    CloudUserConfigBootstrapSettingsRepository(
        lambda: str(bootstrap_data_dir),
    ).save_config(cloud_config)
    local_state = CloudUserConfigLocalStateRepository(lambda: str(bootstrap_data_dir))
    local_state.save_snapshot(
        config_key=_cloud_user_config_database_key(cloud_config),
        revision=1,
        items={},
    )
    local_state.mark_object_storage_skipped()

# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agent_runtime.cloud_user_config import migration as migration_module
from agent_runtime.cloud_user_config.bootstrap_settings import CloudUserConfigBootstrapSettingsRepository
from agent_runtime.cloud_user_config.config import (
    CloudUserConfigDatabaseConfig,
    CloudUserConfigDatabasePoolConfig,
)
from agent_runtime.config import ConfigRegistry
from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_RESPONSES
from agent_runtime.llm_model_registry.infrastructure.sqlite_llm_model_registry_repository import (
    SqliteLlmModelRegistryRepository,
)
from agent_runtime.llm_model_registry.llm_execution_policy import (
    LlmExecutionPolicy,
    LlmExecutionPolicyCandidate,
)
from agent_runtime.llm_model_registry.llm_model_metadata import LlmModelMetadata
from agent_runtime.llm_model_registry.llm_provider_definition import (
    LlmApiKey,
    LlmProviderDefinition,
    LlmProviderModelBinding,
)


class _FakePostgresUserConfigStore:
    """迁移测试用内存 JSONB store，模拟 PostgreSQL namespace + item_id 语义。"""

    items: dict[tuple[str, str], dict[str, Any]] = {}
    last_instance: _FakePostgresUserConfigStore | None = None

    def __init__(self, config_accessor):
        self._config_accessor = config_accessor
        type(self).last_instance = self

    def list_payloads(self, namespace: str) -> list[dict[str, Any]]:
        return [
            dict(payload)
            for (item_namespace, _), payload in sorted(type(self).items.items())
            if item_namespace == namespace
        ]

    def get_payload(self, namespace: str, item_id: str) -> dict[str, Any] | None:
        payload = type(self).items.get((namespace, item_id))
        return dict(payload) if payload is not None else None

    def save_payload(self, namespace: str, item_id: str, payload: dict[str, Any]) -> None:
        type(self).items[(namespace, item_id)] = dict(payload)

    def delete_payload(self, namespace: str, item_id: str) -> bool:
        return type(self).items.pop((namespace, item_id), None) is not None

    def contains_payload(self, namespace: str, item_id: str) -> bool:
        return (namespace, item_id) in type(self).items

    def ensure_schema(self) -> None:
        # 触发配置访问，确保测试覆盖机器本地 bootstrap 凭据覆盖 YAML 元数据后的密码校验路径。
        self._config_accessor().require_password()


def test_migrate_local_user_global_config_to_postgresql_copies_approved_domains(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _FakePostgresUserConfigStore.items = {}
    _FakePostgresUserConfigStore.last_instance = None
    monkeypatch.setattr(
        migration_module,
        "PostgresUserConfigStore",
        _FakePostgresUserConfigStore,
    )
    data_root = tmp_path / "data"
    _write_runtime_config(tmp_path / "agent_runtime.yaml", data_root)
    CloudUserConfigBootstrapSettingsRepository(
        lambda: str(data_root / "cloud-user-config")
    ).save_config(
        CloudUserConfigDatabaseConfig(
            enabled=True,
            backend="postgresql",
            host="db.mindloop.chat",
            port=25432,
            database="agent_runtime",
            username="agent_runtime_client",
            password="postgres-secret",
            sslmode="require",
            bootstrap_data_dir=str(data_root / "cloud-user-config"),
            pool=CloudUserConfigDatabasePoolConfig(
                min_size=1,
                max_size=5,
                connect_timeout_seconds=10,
            ),
        )
    )
    _write_prompt_snippet(data_root)
    _write_agent_definition(data_root)
    _write_llm_registry(data_root, db_filename=None, item_prefix="shared")
    _write_llm_registry(
        data_root,
        db_filename="image_generation_llm_registry.sqlite3",
        item_prefix="image",
    )

    try:
        ConfigRegistry.load(str(tmp_path / "agent_runtime.yaml"), watch=False)
        summary = migration_module.migrate_local_user_global_config_to_postgresql()
        second_summary = migration_module.migrate_local_user_global_config_to_postgresql()
    finally:
        ConfigRegistry.reset()

    assert summary.failed_total == 0
    assert summary.imported_total == 8
    assert summary.skipped_total == 0
    assert second_summary.failed_total == 0
    assert second_summary.imported_total == 0
    assert second_summary.skipped_total == 8

    store = _FakePostgresUserConfigStore.last_instance
    assert store is not None
    items = _FakePostgresUserConfigStore.items

    assert items[("prompt_snippet", "shared-prompt")]["text"] == "Prompt body"
    assert items[("agent_definition", "shared-agent")]["name"] == "Shared Agent"

    provider_payload = items[("llm_provider_definition", "shared-provider")]
    assert provider_payload["api_keys"][0]["key"] == "shared-provider-secret"
    assert provider_payload["models"][0]["id"] == "shared-binding"

    image_provider_payload = items[(
        "image_generation_llm_provider_definition",
        "image-provider",
    )]
    assert image_provider_payload["api_keys"][0]["key"] == "image-provider-secret"


def _write_runtime_config(config_path: Path, data_root: Path) -> None:
    config_path.write_text(
        "\n".join(
            [
                "prompt_template:",
                f"  snippets_dir: \"{(data_root / 'prompt-snippets').as_posix()}\"",
                "agent_definition:",
                f"  definitions_dir: \"{(data_root / 'agents').as_posix()}\"",
                "llm_model_registry:",
                f"  registry_dir: \"{(data_root / 'llm-model-registry').as_posix()}\"",
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


def _write_prompt_snippet(data_root: Path) -> None:
    prompt_dir = data_root / "prompt-snippets" / "default"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / "shared.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "shared-prompt",
                "text": "Prompt body",
                "category": "default",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_agent_definition(data_root: Path) -> None:
    agents_dir = data_root / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "shared-agent.yaml").write_text(
        yaml.safe_dump(
            {
                "agent_id": "shared-agent",
                "name": "Shared Agent",
                "group": "default",
                "system_prompt": [{"text": "system prompt"}],
                "tool_ids": [],
                "mcp_tools": [],
                "skill_ids": [],
                "available_sub_agent_ids": [],
                "builtin_background_agent": False,
                "disabled": False,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_llm_registry(
    data_root: Path,
    *,
    db_filename: str | None,
    item_prefix: str,
) -> None:
    kwargs = {}
    if db_filename is not None:
        kwargs["db_filename"] = db_filename
    repository = SqliteLlmModelRegistryRepository(
        lambda: str(data_root / "llm-model-registry"),
        **kwargs,
    )
    try:
        repository.save_model_metadata(
            LlmModelMetadata(
                model_id=f"{item_prefix}-model",
                context_window=128000,
                max_output_tokens=8192,
                supports_tool_calling=True,
                supports_structured_output=False,
            )
        )
        repository.save_provider(
            LlmProviderDefinition(
                provider_id=f"{item_prefix}-provider",
                api_base_url="https://llm.example.test/v1",
                api_keys=(
                    LlmApiKey(
                        key=f"{item_prefix}-provider-secret",
                        name="primary",
                    ),
                ),
                models=(
                    LlmProviderModelBinding(
                        id=f"{item_prefix}-binding",
                        model_id=f"{item_prefix}-model",
                        model_alias=f"{item_prefix}-model-alias",
                        api_format=API_FORMAT_OPENAI_RESPONSES,
                    ),
                ),
            )
        )
        repository.save_execution_policy(
            LlmExecutionPolicy(
                policy_group_id=f"{item_prefix}-policy",
                candidates=(
                    LlmExecutionPolicyCandidate(
                        id=f"{item_prefix}-candidate",
                        provider_id=f"{item_prefix}-provider",
                        model_binding_id=f"{item_prefix}-binding",
                    ),
                ),
            )
        )
    finally:
        repository.close()

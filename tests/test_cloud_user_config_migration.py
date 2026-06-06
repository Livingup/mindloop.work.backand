# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.cloud_user_config import migration as migration_module
from agent_runtime.cloud_user_config import postgres_store as postgres_store_module
from agent_runtime.cloud_user_config.north_gateway import (
    bootstrap_rest_adapter as bootstrap_module,
)
from agent_runtime.cloud_user_config.bootstrap_settings import CloudUserConfigBootstrapSettingsRepository
from agent_runtime.cloud_user_config.local_state import CloudUserConfigLocalStateRepository
from agent_runtime.cloud_user_config.config import (
    CloudUserConfigDatabaseConfig,
    CloudUserConfigDatabasePoolConfig,
)
from agent_runtime.config import ConfigRegistry
from agent_runtime.context_compression.prompt_repository import (
    ContextCompressionWorkerPromptRepository,
    create_context_compression_worker_prompt_repository,
)
from agent_runtime.context_compression.settings_repository import (
    ContextCompressionSettingsRepository,
    create_context_compression_settings_repository,
)
from agent_runtime.image_generation.settings_repository import (
    ImageGenerationSettingsRepository,
    create_image_generation_settings_repository,
)
from agent_runtime.lacp.settings_repository import (
    LacpSettingsRepository,
    create_lacp_settings_repository,
)
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
from agent_runtime.mcp.mcp_client.infrastructure.postgres_mcp_service_repository import (
    PostgresStreamableHttpMcpServiceRepository,
    TransportRoutingMcpServiceRepository,
)
from agent_runtime.mcp.mcp_client.infrastructure.yaml_mcp_service_repository import YamlMcpServiceRepository
from agent_runtime.mcp.mcp_client.mcp_service_definition import McpServiceDefinition
from agent_runtime.mcp.mcp_client.mcp_transport_config import (
    StdioTransportConfig,
    StreamableHttpTransportConfig,
)
from agent_runtime.web_tools.models import WebToolPolicyCandidate
from agent_runtime.web_tools.settings_repository import (
    WebToolsSettingsRepository,
    create_web_tools_settings_repository,
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

    def reload_cache_from_database(self):
        config = self._config_accessor()
        CloudUserConfigLocalStateRepository(
            lambda: config.bootstrap_data_dir,
        ).save_snapshot(
            config_key=(
                config.host,
                config.port,
                config.database,
                config.username,
                config.sslmode,
            ),
            revision=1,
            items={key: dict(value) for key, value in type(self).items.items()},
        )


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
    _write_image_generation_settings(data_root)
    _write_web_tools_settings(data_root)
    _write_context_compression_settings(data_root)
    _write_context_compression_worker_prompts(data_root)
    _write_mcp_services(data_root)
    _write_lacp_settings(data_root)

    try:
        ConfigRegistry.load(str(tmp_path / "agent_runtime.yaml"), watch=False)
        summary = migration_module.migrate_local_user_global_config_to_postgresql()
        second_summary = migration_module.migrate_local_user_global_config_to_postgresql()
    finally:
        ConfigRegistry.reset()

    assert summary.failed_total == 0
    assert summary.imported_total == 14
    assert summary.skipped_total == 0
    assert second_summary.failed_total == 0
    assert second_summary.imported_total == 0
    assert second_summary.skipped_total == 14

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

    image_settings_payload = items[("image_generation.runtime_settings", "default")]
    assert image_settings_payload["llm_policy_group_id"] == "image-policy"

    web_tools_payload = items[("web_tools.runtime_settings", "default")]
    assert web_tools_payload["candidates"][0]["provider_id"] == "shared-provider"
    assert web_tools_payload["candidates"][0]["model_binding_id"] == "shared-binding"

    context_settings_payload = items[("context_compression.runtime_settings", "default")]
    assert context_settings_payload["llm_policy_group_id"] == "context-policy"
    assert context_settings_payload["input_mode"] == "native_multimodal_history"
    assert context_settings_payload["updated_at"] is not None

    custom_prompt_payload = items[(
        "context_compression.worker_prompt",
        "native_multimodal_history:ProgressPlanWorker",
    )]
    assert custom_prompt_payload["prompt_text"] == "Custom progress prompt"
    assert custom_prompt_payload["default_prompt_text"] == "Default progress prompt"
    assert custom_prompt_payload["is_custom"] is True
    assert (
        "context_compression.worker_prompt",
        "transcript_snapshot:RequirementConstraintWorker",
    ) not in items

    mcp_payload = items[("mcp.streamable_http_service_definition", "remote-search")]
    assert mcp_payload["transport"]["type"] == "streamable-http"
    assert mcp_payload["transport"]["url"] == "https://mcp.example.test/search"
    assert ("mcp.streamable_http_service_definition", "local-files") not in items

    lacp_payload = items[("lacp.registry_service_settings", "default")]
    assert lacp_payload["enabled"] is True
    assert lacp_payload["registry_url"] == "https://lacp.example.test/registry"


def test_runtime_bootstrap_configure_saves_credentials_without_migration_before_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _FakePostgresUserConfigStore.items = {}
    _FakePostgresUserConfigStore.last_instance = None
    monkeypatch.setattr(
        bootstrap_module,
        "PostgresUserConfigStore",
        _FakePostgresUserConfigStore,
    )
    monkeypatch.setattr(
        postgres_store_module,
        "PostgresUserConfigStore",
        _FakePostgresUserConfigStore,
    )

    data_root = tmp_path / "data"
    _write_runtime_config(tmp_path / "agent_runtime.yaml", data_root)
    _write_prompt_snippet(data_root)
    _write_agent_definition(data_root)
    _write_llm_registry(data_root, db_filename=None, item_prefix="shared")
    _write_llm_registry(
        data_root,
        db_filename="image_generation_llm_registry.sqlite3",
        item_prefix="image",
    )
    _write_image_generation_settings(data_root)
    _write_web_tools_settings(data_root)
    _write_mcp_services(data_root)
    _write_lacp_settings(data_root)

    app = FastAPI()
    app.include_router(
        bootstrap_module.create_runtime_bootstrap_router(
            cloud_user_config_database_active_at_start=False,
        ),
        prefix="/api/runtime-bootstrap",
    )

    try:
        ConfigRegistry.load(str(tmp_path / "agent_runtime.yaml"), watch=False)
        response = TestClient(app).post(
            "/api/runtime-bootstrap/configure",
            json={
                "cloud_user_config_database": {
                    "enabled": True,
                    "backend": "postgresql",
                    "host": "db.mindloop.chat",
                    "port": 25432,
                    "database": "agent_runtime",
                    "username": "agent_runtime_client",
                    "password": "postgres-secret",
                    "sslmode": "require",
                    "connect_timeout_seconds": 10,
                }
            },
        )
    finally:
        ConfigRegistry.reset()

    assert response.status_code == 200
    body = response.json()
    assert body["restart_required"] is False
    assert body["ready"] is False
    assert body["bootstrap_required"] is True
    assert body["cloud_user_config_database"]["configured"] is True
    assert body["cloud_user_config_database"]["active_at_start"] is False
    assert body["cloud_user_config_database"]["shared_config_snapshot_ready"] is True
    assert body["missing_required_items"] == ["object_storage_decision"]
    assert _FakePostgresUserConfigStore.items == {}

    try:
        ConfigRegistry.load(str(tmp_path / "agent_runtime.yaml"), watch=False)
        skip_response = TestClient(app).post(
            "/api/runtime-bootstrap/configure",
            json={
                "cloud_user_config_database": None,
                "object_storage_settings": None,
                "object_storage_skipped": True,
            },
        )
    finally:
        ConfigRegistry.reset()

    assert skip_response.status_code == 200
    skip_body = skip_response.json()
    assert skip_body["ready"] is True
    assert skip_body["bootstrap_required"] is False
    assert skip_body["object_storage"]["skipped"] is True


def test_cloud_enabled_shared_domain_repositories_use_postgresql_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _FakePostgresUserConfigStore.items = {}
    _FakePostgresUserConfigStore.last_instance = None
    monkeypatch.setattr(
        postgres_store_module,
        "PostgresUserConfigStore",
        _FakePostgresUserConfigStore,
    )
    data_root = tmp_path / "data"
    _write_runtime_config(tmp_path / "agent_runtime.yaml", data_root)
    durable_config = CloudUserConfigDatabaseConfig(
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
    CloudUserConfigBootstrapSettingsRepository(
        lambda: str(data_root / "cloud-user-config")
    ).save_config(durable_config)
    local_state_repository = CloudUserConfigLocalStateRepository(
        lambda: str(data_root / "cloud-user-config")
    )
    local_state_repository.save_snapshot(
        config_key=(
            durable_config.host,
            durable_config.port,
            durable_config.database,
            durable_config.username,
            durable_config.sslmode,
        ),
        revision=1,
        items={},
    )
    local_state_repository.mark_object_storage_skipped()

    try:
        ConfigRegistry.load(str(tmp_path / "agent_runtime.yaml"), watch=False)

        image_repository = create_image_generation_settings_repository(
            lambda: str(data_root / "llm-model-registry"),
        )
        image_settings = image_repository.save_settings(llm_policy_group_id="cloud-image-policy")
        assert str(image_repository.db_path()).startswith("postgresql:")
        assert image_repository.get_settings().llm_policy_group_id == "cloud-image-policy"
        assert image_settings.llm_policy_group_id == "cloud-image-policy"
        assert not (data_root / "llm-model-registry" / "image_generation_settings.db").exists()

        web_repository = create_web_tools_settings_repository(
            lambda: str(data_root / "llm-model-registry"),
        )
        web_settings = web_repository.save_settings(
            candidates=(
                WebToolPolicyCandidate(
                    id="cloud-web-candidate",
                    provider_id="shared-provider",
                    model_binding_id="shared-binding",
                ),
            ),
        )
        assert str(web_repository.db_path()).startswith("postgresql:")
        assert web_repository.get_settings().candidates[0].id == "cloud-web-candidate"
        assert web_settings.candidates[0].provider_id == "shared-provider"
        assert not (data_root / "llm-model-registry" / "web_tools_settings.db").exists()

        lacp_repository = create_lacp_settings_repository(lambda: str(data_root / "lacp"))
        lacp_settings = lacp_repository.save_settings(
            enabled=True,
            registry_url="https://lacp.example.test/registry",
            api_key="lacp-secret",
        )
        assert str(lacp_repository.db_path()).startswith("postgresql:")
        assert lacp_repository.get_settings().registry_url == "https://lacp.example.test/registry"
        assert lacp_settings.api_key == "lacp-secret"
        assert not (data_root / "lacp" / "lacp_settings.db").exists()

        context_settings_repository = create_context_compression_settings_repository(
            lambda: str(data_root / "context-compression"),
        )
        context_settings = context_settings_repository.set_settings(
            llm_policy_group_id="cloud-context-policy",
            input_mode="transcript_snapshot",
        )
        assert str(context_settings_repository.db_path()).startswith("postgresql:")
        assert context_settings_repository.get_settings().llm_policy_group_id == "cloud-context-policy"
        assert context_settings.input_mode == "transcript_snapshot"

        context_prompt_repository = create_context_compression_worker_prompt_repository(
            lambda: str(data_root / "context-compression"),
        )
        prompt = context_prompt_repository.upsert_default(
            input_mode="transcript_snapshot",
            worker_name="RequirementConstraintWorker",
            default_prompt_text="Default cloud prompt",
        )
        prompt = context_prompt_repository.update_prompt(
            input_mode=prompt.input_mode,
            worker_name=prompt.worker_name,
            prompt_text="Custom cloud prompt",
        )
        assert str(context_prompt_repository.db_path()).startswith("postgresql:")
        assert context_prompt_repository.get_required_prompt(
            input_mode="transcript_snapshot",
            worker_name="RequirementConstraintWorker",
        ).prompt_text == "Custom cloud prompt"
        assert prompt.is_custom is True
        assert not (
            data_root / "context-compression" / "context_compression_worker_prompts.db"
        ).exists()

        local_mcp_repository = YamlMcpServiceRepository(lambda: str(data_root / "mcp-servers"))
        shared_store = _FakePostgresUserConfigStore(
            lambda: ConfigRegistry.instance().cloud_user_config_database,
        )
        shared_mcp_repository = PostgresStreamableHttpMcpServiceRepository(lambda: shared_store)
        routing_repository = TransportRoutingMcpServiceRepository(
            local_repository=local_mcp_repository,
            shared_repository=shared_mcp_repository,
        )

        routing_repository.save(
            McpServiceDefinition(
                service_id="local-files",
                name="Local Files",
                enabled=True,
                transport=StdioTransportConfig(command="local-mcp", args=["serve"]),
            )
        )
        routing_repository.save(
            McpServiceDefinition(
                service_id="remote-search",
                name="Remote Search",
                enabled=True,
                transport=StreamableHttpTransportConfig(
                    url="https://mcp.example.test/search",
                    headers={"Authorization": "Bearer shared-token"},
                ),
            )
        )

        assert (data_root / "mcp-servers" / "local-files.yaml").exists()
        assert not (data_root / "mcp-servers" / "remote-search.yaml").exists()
        assert routing_repository.find_by_id("local-files").transport_type == "stdio"
        assert routing_repository.find_by_id("remote-search").transport_type == "streamable-http"
        assert _FakePostgresUserConfigStore.items[(
            "mcp.streamable_http_service_definition",
            "remote-search",
        )]["transport"]["url"] == "https://mcp.example.test/search"
        assert routing_repository.delete("remote-search") is True
        assert ("mcp.streamable_http_service_definition", "remote-search") not in _FakePostgresUserConfigStore.items
    finally:
        ConfigRegistry.reset()


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
                "mcp_client:",
                f"  config_dir: \"{(data_root / 'mcp-servers').as_posix()}\"",
                "lacp:",
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


def _write_image_generation_settings(data_root: Path) -> None:
    repository = ImageGenerationSettingsRepository(
        lambda: str(data_root / "llm-model-registry")
    )
    repository.save_settings(llm_policy_group_id="image-policy")


def _write_web_tools_settings(data_root: Path) -> None:
    repository = WebToolsSettingsRepository(lambda: str(data_root / "llm-model-registry"))
    repository.save_settings(
        candidates=(
            WebToolPolicyCandidate(
                id="shared-web-tool-candidate",
                provider_id="shared-provider",
                model_binding_id="shared-binding",
            ),
        )
    )


def _write_context_compression_settings(data_root: Path) -> None:
    repository = ContextCompressionSettingsRepository(
        lambda: str(data_root / "context-compression"),
    )
    repository.set_settings(
        llm_policy_group_id="context-policy",
        input_mode="native_multimodal_history",
    )


def _write_context_compression_worker_prompts(data_root: Path) -> None:
    repository = ContextCompressionWorkerPromptRepository(
        lambda: str(data_root / "context-compression"),
    )
    repository.upsert_default(
        input_mode="transcript_snapshot",
        worker_name="RequirementConstraintWorker",
        default_prompt_text="Default requirement prompt",
    )
    repository.upsert_default(
        input_mode="native_multimodal_history",
        worker_name="ProgressPlanWorker",
        default_prompt_text="Default progress prompt",
    )
    repository.update_prompt(
        input_mode="native_multimodal_history",
        worker_name="ProgressPlanWorker",
        prompt_text="Custom progress prompt",
    )


def _write_mcp_services(data_root: Path) -> None:
    repository = YamlMcpServiceRepository(lambda: str(data_root / "mcp-servers"))
    repository.save(
        McpServiceDefinition(
            service_id="local-files",
            name="Local Files",
            description="Machine-local stdio MCP service",
            enabled=True,
            transport=StdioTransportConfig(command="local-mcp", args=["serve"]),
        )
    )
    repository.save(
        McpServiceDefinition(
            service_id="remote-search",
            name="Remote Search",
            description="Shareable streamable-http MCP service",
            enabled=True,
            transport=StreamableHttpTransportConfig(
                url="https://mcp.example.test/search",
                headers={"Authorization": "Bearer shared-token"},
            ),
        )
    )


def _write_lacp_settings(data_root: Path) -> None:
    repository = LacpSettingsRepository(lambda: str(data_root / "lacp"))
    repository.save_settings(
        enabled=True,
        registry_url="https://lacp.example.test/registry",
        api_key="lacp-shared-secret",
    )

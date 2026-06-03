# -*- coding: utf-8 -*-
"""本机用户全局可复用配置到云端 PostgreSQL 的一次性迁移。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, TypeVar

from agent_runtime.agent_definition.agent_definition import AgentDefinition
from agent_runtime.agent_definition.infrastructure.postgres_agent_definition_repository import (
    PostgresAgentDefinitionRepository,
)
from agent_runtime.agent_definition.infrastructure.yaml_agent_definition_repository import (
    YamlAgentDefinitionRepository,
)
from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore
from agent_runtime.llm_model_registry.infrastructure.postgres_llm_model_registry_repository import (
    PostgresLlmModelRegistryRepository,
)
from agent_runtime.llm_model_registry.infrastructure.sqlite_llm_model_registry_repository import (
    SqliteLlmModelRegistryRepository,
)
from agent_runtime.llm_model_registry.llm_execution_policy import LlmExecutionPolicy
from agent_runtime.llm_model_registry.llm_model_metadata import LlmModelMetadata
from agent_runtime.llm_model_registry.llm_provider_definition import LlmProviderDefinition
from agent_runtime.prompt_template.infrastructure.postgres_prompt_snippet_repository import (
    PostgresPromptSnippetRepository,
)
from agent_runtime.prompt_template.infrastructure.yaml_snippet_repository import (
    YamlFileSnippetRepository,
)
from agent_runtime.prompt_template.prompt_snippet import PromptSnippet


_IMAGE_GENERATION_LLM_REGISTRY_DB_FILENAME = "image_generation_llm_registry.sqlite3"
_IMAGE_GENERATION_NAMESPACE_PREFIX = "image_generation_"

_T = TypeVar("_T")


@dataclass(slots=True)
class MigrationDomainStats:
    """单个用户全局配置集合的迁移统计。"""

    imported: int = 0
    skipped: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "imported": self.imported,
            "skipped": self.skipped,
            "failed": self.failed,
        }


@dataclass(slots=True)
class CloudUserConfigMigrationSummary:
    """用户全局可复用配置迁移摘要。"""

    prompt_snippets: MigrationDomainStats = field(default_factory=MigrationDomainStats)
    agent_definitions: MigrationDomainStats = field(default_factory=MigrationDomainStats)
    llm_model_metadata: MigrationDomainStats = field(default_factory=MigrationDomainStats)
    llm_provider_definitions: MigrationDomainStats = field(default_factory=MigrationDomainStats)
    llm_execution_policies: MigrationDomainStats = field(default_factory=MigrationDomainStats)
    image_llm_model_metadata: MigrationDomainStats = field(default_factory=MigrationDomainStats)
    image_llm_provider_definitions: MigrationDomainStats = field(default_factory=MigrationDomainStats)
    image_llm_execution_policies: MigrationDomainStats = field(default_factory=MigrationDomainStats)
    errors: list[str] = field(default_factory=list)

    @property
    def imported_total(self) -> int:
        return sum(stats.imported for stats in self._all_stats())

    @property
    def skipped_total(self) -> int:
        return sum(stats.skipped for stats in self._all_stats())

    @property
    def failed_total(self) -> int:
        return sum(stats.failed for stats in self._all_stats())

    def to_dict(self) -> dict[str, object]:
        return {
            "imported_total": self.imported_total,
            "skipped_total": self.skipped_total,
            "failed_total": self.failed_total,
            "prompt_snippets": self.prompt_snippets.to_dict(),
            "agent_definitions": self.agent_definitions.to_dict(),
            "llm_model_metadata": self.llm_model_metadata.to_dict(),
            "llm_provider_definitions": self.llm_provider_definitions.to_dict(),
            "llm_execution_policies": self.llm_execution_policies.to_dict(),
            "image_llm_model_metadata": self.image_llm_model_metadata.to_dict(),
            "image_llm_provider_definitions": self.image_llm_provider_definitions.to_dict(),
            "image_llm_execution_policies": self.image_llm_execution_policies.to_dict(),
            "errors": list(self.errors),
        }

    def _all_stats(self) -> tuple[MigrationDomainStats, ...]:
        return (
            self.prompt_snippets,
            self.agent_definitions,
            self.llm_model_metadata,
            self.llm_provider_definitions,
            self.llm_execution_policies,
            self.image_llm_model_metadata,
            self.image_llm_provider_definitions,
            self.image_llm_execution_policies,
        )


def migrate_local_user_global_config_to_postgresql(
    *,
    overwrite: bool = False,
) -> CloudUserConfigMigrationSummary:
    """把本机已批准的用户全局配置一次性导入云端 PostgreSQL。

    迁移范围严格限制为 prompt snippets、agent definitions、主 LLM Registry 和图像生成专用 LLM Registry。
    运行态、会话历史、项目数据、MCP 本机路径等不属于本次迁移范围。
    """
    from agent_runtime.config import ConfigRegistry

    cloud_config = ConfigRegistry.instance().cloud_user_config_database
    if not cloud_config.is_postgresql_enabled:
        raise ValueError(
            "cloud_user_config_database 未启用 PostgreSQL；请先在运行配置中设置 "
            "enabled=true 且 backend=postgresql。"
        )

    store = PostgresUserConfigStore(
        lambda: ConfigRegistry.instance().cloud_user_config_database,
    )
    store.ensure_schema()

    summary = CloudUserConfigMigrationSummary()

    _migrate_prompt_snippets(store, summary, overwrite=overwrite)
    _migrate_agent_definitions(store, summary, overwrite=overwrite)
    _migrate_llm_registry(
        store,
        summary,
        overwrite=overwrite,
        source_db_filename=None,
        namespace_prefix="",
        model_stats=summary.llm_model_metadata,
        provider_stats=summary.llm_provider_definitions,
        policy_stats=summary.llm_execution_policies,
        domain_prefix="llm_registry",
    )
    _migrate_llm_registry(
        store,
        summary,
        overwrite=overwrite,
        source_db_filename=_IMAGE_GENERATION_LLM_REGISTRY_DB_FILENAME,
        namespace_prefix=_IMAGE_GENERATION_NAMESPACE_PREFIX,
        model_stats=summary.image_llm_model_metadata,
        provider_stats=summary.image_llm_provider_definitions,
        policy_stats=summary.image_llm_execution_policies,
        domain_prefix="image_generation_llm_registry",
    )

    return summary


def _migrate_prompt_snippets(
    store: PostgresUserConfigStore,
    summary: CloudUserConfigMigrationSummary,
    *,
    overwrite: bool,
) -> None:
    from agent_runtime.config import ConfigRegistry

    source = YamlFileSnippetRepository(
        lambda: ConfigRegistry.instance().prompt_template.snippets_dir,
    )
    target = PostgresPromptSnippetRepository(lambda: store)
    _migrate_collection(
        domain="prompt_snippets",
        stats=summary.prompt_snippets,
        errors=summary.errors,
        items=lambda: source.find_all(),
        item_id=lambda item: item.name,
        exists=target.exists,
        save=target.save,
        overwrite=overwrite,
    )


def _migrate_agent_definitions(
    store: PostgresUserConfigStore,
    summary: CloudUserConfigMigrationSummary,
    *,
    overwrite: bool,
) -> None:
    from agent_runtime.config import ConfigRegistry

    source = YamlAgentDefinitionRepository(
        lambda: ConfigRegistry.instance().agent_definition.definitions_dir,
    )
    target = PostgresAgentDefinitionRepository(lambda: store)
    _migrate_collection(
        domain="agent_definitions",
        stats=summary.agent_definitions,
        errors=summary.errors,
        items=lambda: source.find_all(),
        item_id=lambda item: item.agent_id,
        exists=target.contains,
        save=target.save,
        overwrite=overwrite,
    )


def _migrate_llm_registry(
    store: PostgresUserConfigStore,
    summary: CloudUserConfigMigrationSummary,
    *,
    overwrite: bool,
    source_db_filename: str | None,
    namespace_prefix: str,
    model_stats: MigrationDomainStats,
    provider_stats: MigrationDomainStats,
    policy_stats: MigrationDomainStats,
    domain_prefix: str,
) -> None:
    from agent_runtime.config import ConfigRegistry

    source_kwargs = {}
    if source_db_filename is not None:
        source_kwargs["db_filename"] = source_db_filename
    source = SqliteLlmModelRegistryRepository(
        lambda: ConfigRegistry.instance().llm_model_registry.registry_dir,
        **source_kwargs,
    )
    target = PostgresLlmModelRegistryRepository(
        lambda: store,
        namespace_prefix=namespace_prefix,
    )
    try:
        _migrate_collection(
            domain=f"{domain_prefix}.models",
            stats=model_stats,
            errors=summary.errors,
            items=lambda: source.find_all_model_metadata(),
            item_id=lambda item: item.model_id,
            exists=target.contains_model_metadata,
            save=target.save_model_metadata,
            overwrite=overwrite,
        )
        _migrate_collection(
            domain=f"{domain_prefix}.providers",
            stats=provider_stats,
            errors=summary.errors,
            items=lambda: source.find_all_providers(),
            item_id=lambda item: item.provider_id,
            exists=target.contains_provider,
            save=target.save_provider,
            overwrite=overwrite,
        )
        _migrate_collection(
            domain=f"{domain_prefix}.policies",
            stats=policy_stats,
            errors=summary.errors,
            items=lambda: source.find_all_execution_policies(),
            item_id=lambda item: item.policy_group_id,
            exists=target.contains_execution_policy,
            save=target.save_execution_policy,
            overwrite=overwrite,
        )
    finally:
        source.close()


def _migrate_collection(
    *,
    domain: str,
    stats: MigrationDomainStats,
    errors: list[str],
    items: Callable[[], Iterable[_T]],
    item_id: Callable[[_T], str],
    exists: Callable[[str], bool],
    save: Callable[[_T], None],
    overwrite: bool,
) -> None:
    try:
        loaded_items = list(items())
    except Exception as exc:
        stats.failed += 1
        errors.append(f"{domain}:<list>: {exc}")
        return

    for item in loaded_items:
        current_item_id = "<unknown>"
        try:
            current_item_id = item_id(item)
            if not overwrite and exists(current_item_id):
                stats.skipped += 1
                continue
            save(item)
            stats.imported += 1
        except Exception as exc:
            stats.failed += 1
            errors.append(f"{domain}:{current_item_id}: {exc}")


__all__ = [
    "CloudUserConfigMigrationSummary",
    "MigrationDomainStats",
    "migrate_local_user_global_config_to_postgresql",
]
